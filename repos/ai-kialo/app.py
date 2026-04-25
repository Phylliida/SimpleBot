"""Flask webapp for ai-kialo: submit claims, view their argument tree, expand via LLM.

Architecture:
- `AppState` holds the singletons (event log, node embeddings, classifier, expander).
- `create_app(state)` is a factory so tests can inject fakes.
- Each request replays the event log into a fresh `nodes` dict — cheap at v0 scale,
  removes any cache-coherence concerns.

v0 scope: list root claims, submit (with dedup → review page), view node + children,
expand a node via the LLM. Deferred: best-first auto-expansion, dig-here marking,
convincingness scoring, search.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from flask import Flask, abort, redirect, render_template, request, url_for

from classify import Classifier, ClaimClassification, LlamaCppClassifier, classify_claim
from embeddings import sentence_embed
from event_log import EventLog, Node
from expand import Expander, LlamaCppExpander, label_score
from node_embeddings import NodeEmbeddings


# Below this confidence we ignore the classifier's verdict and treat as new
# (duplicate/negation needs to be reasonably certain to be worth showing the user)
_DEDUP_CONFIDENCE_FLOOR = 0.5


@dataclass
class AppState:
    data_dir: Path
    event_log: EventLog
    node_embeddings: NodeEmbeddings
    classifier: Classifier
    expander: Expander

    @classmethod
    def from_dir(
        cls,
        data_dir: str | Path,
        classifier: Classifier | None = None,
        expander: Expander | None = None,
    ) -> "AppState":
        data_dir = Path(data_dir)
        return cls(
            data_dir=data_dir,
            event_log=EventLog(data_dir / "events.jsonl"),
            node_embeddings=NodeEmbeddings(data_dir / "node_embeddings.bin"),
            classifier=classifier or LlamaCppClassifier(),
            expander=expander or LlamaCppExpander(),
        )

    def replay(self) -> dict[str, Node]:
        return self.event_log.replay()


# ---------- write helpers ----------

def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def create_node(
    state: AppState,
    text: str,
    parent_id: str | None,
    stance: str,
) -> str:
    """Append a new node — embed → write to vectors store → emit node_created."""
    text = text.strip()
    if not text:
        raise ValueError("empty claim text")
    embed = sentence_embed(text)
    embed_idx = state.node_embeddings.append(embed)
    nid = _new_id()
    state.event_log.append({
        "t": "node_created",
        "id": nid,
        "parent": parent_id,
        "stance": stance,
        "text": text,
        "embed_idx": embed_idx,
    })
    return nid


def link_negation(state: AppState, a: str, b: str, reason: str = "user-confirmed") -> None:
    state.event_log.append({"t": "node_negates", "a": a, "b": b, "reason": reason})


def merge_node(state: AppState, dupe_id: str, into: str, reason: str) -> None:
    state.event_log.append({"t": "node_merged", "id": dupe_id, "into": into, "reason": reason})


def soft_delete(state: AppState, nid: str) -> None:
    state.event_log.append({"t": "node_deleted", "id": nid})


def _try_call(call):
    """Best-effort wrapper — swallow exceptions and return None on failure."""
    try:
        return call()
    except Exception:
        return None


def _persist_label(state: AppState, nid: str, label: str | None) -> None:
    if label is None:
        return
    state.event_log.append({
        "t": "node_scored", "id": nid, "label": label, "src": "scorer",
    })


def _persist_reasoning(state: AppState, nid: str, reasoning: str | None) -> None:
    if not reasoning:
        return
    state.event_log.append({
        "t": "node_scored", "id": nid, "reasoning": reasoning, "src": "explainer",
    })


def create_root(state: AppState, text: str) -> str:
    """Create a root node and best-effort score it via score_claim (label only — no reasoning)."""
    nid = create_node(state, text, parent_id=None, stance="root")
    _persist_label(state, nid, _try_call(lambda: state.expander.score_claim(text)))
    return nid


def add_child(state: AppState, parent_id: str, text: str, stance: str) -> str:
    """Create a user-supplied child under `parent_id`, then score it via the expander.

    The node is created unconditionally; if scoring fails (returns None / raises),
    the node still exists — scoring is a nice-to-have, not a precondition.
    """
    if stance not in ("pro", "con"):
        raise ValueError(f"stance must be 'pro' or 'con', got {stance!r}")
    text = text.strip()
    if not text:
        raise ValueError("empty argument text")
    nodes = state.replay()
    parent = nodes.get(parent_id)
    if parent is None or parent.deleted or parent.merged_into is not None:
        raise ValueError(f"parent {parent_id!r} is missing, deleted, or merged")
    nid = create_node(state, text, parent_id=parent.id, stance=stance)
    _persist_label(state, nid, _try_call(lambda: state.expander.score_argument(parent.text, text, stance)))
    return nid


def expand_node(state: AppState, parent_id: str, n_pros: int = 2, n_cons: int = 2) -> list[str]:
    """LLM-generate children for `parent_id` and write them as new nodes.

    Passes the parent's existing live pros/cons to the expander so re-expanding
    yields arguments that don't duplicate ones already present.
    """
    nodes = state.replay()
    parent = nodes.get(parent_id)
    if parent is None or parent.deleted or parent.merged_into is not None:
        raise ValueError(f"parent {parent_id!r} is missing, deleted, or merged")
    existing_pros = [nodes[c].text for c in parent.children if nodes[c].stance == "pro"]
    existing_cons = [nodes[c].text for c in parent.children if nodes[c].stance == "con"]
    specs = state.expander.expand(
        parent.text,
        n_pros=n_pros,
        n_cons=n_cons,
        existing_pros=existing_pros,
        existing_cons=existing_cons,
    )
    new_ids: list[str] = []
    for spec in specs:
        nid = create_node(state, spec.text, parent_id=parent.id, stance=spec.stance)
        if spec.label is not None:
            state.event_log.append({
                "t": "node_scored", "id": nid, "label": spec.label, "src": "expander",
            })
        new_ids.append(nid)
    return new_ids


def explain_node(state: AppState, nid: str) -> str | None:
    """Generate (and persist) an explanation for `nid`'s current label.

    Idempotent-ish: re-runs every time it's called and overwrites with the latest
    reasoning. Caller should check `node.reasoning` before calling to avoid wasted
    LLM calls.
    """
    nodes = state.replay()
    n = nodes.get(nid)
    if n is None or n.deleted or n.merged_into is not None:
        raise ValueError(f"node {nid!r} is missing, deleted, or merged")
    if not n.label:
        return None
    if n.parent_id is None:
        reasoning = _try_call(lambda: state.expander.explain_claim(n.text, n.label))
    else:
        parent = nodes.get(n.parent_id)
        if parent is None:
            return None
        reasoning = _try_call(
            lambda: state.expander.explain_argument(parent.text, n.text, n.stance, n.label)
        )
    _persist_reasoning(state, nid, reasoning)
    return reasoning


# ---------- read helpers ----------

def _live(nodes: dict[str, Node], ids: Iterable[str]) -> list[Node]:
    """Filter ids to live nodes (not deleted, not merged), preserving order."""
    out: list[Node] = []
    for nid in ids:
        n = nodes.get(nid)
        if n is None or n.deleted or n.merged_into is not None:
            continue
        out.append(n)
    return out


def _by_score_desc(nodes_list: list[Node]) -> list[Node]:
    """Sort by label score descending; tiebreak by created_at ascending."""
    return sorted(nodes_list, key=lambda n: (-label_score(n.label), n.created_at))


def _roots(nodes: dict[str, Node]) -> list[Node]:
    return [
        n for n in nodes.values()
        if n.parent_id is None and not n.deleted and n.merged_into is None
    ]


# ---------- app factory ----------

def create_app(state: AppState) -> Flask:
    app = Flask(__name__)
    app.config["STATE"] = state
    # Make the score helper available in templates so they can render badges.
    app.jinja_env.globals["label_score"] = label_score

    @app.route("/")
    def index():
        nodes = state.replay()
        # sort roots by score desc, tiebreak newest-first
        roots = sorted(
            _roots(nodes),
            key=lambda n: (-label_score(n.label), -n.created_at),
        )
        return render_template("index.html", roots=roots)

    @app.route("/node/<nid>")
    def view_node(nid: str):
        nodes = state.replay()
        n = nodes.get(nid)
        if n is None or n.deleted:
            abort(404)
        if n.merged_into is not None:
            return redirect(url_for("view_node", nid=n.merged_into))
        pros = _by_score_desc(_live(nodes, [c for c in n.children if nodes[c].stance == "pro"]))
        cons = _by_score_desc(_live(nodes, [c for c in n.children if nodes[c].stance == "con"]))
        parent = nodes.get(n.parent_id) if n.parent_id else None
        negations = _live(nodes, n.negates)
        return render_template(
            "node.html",
            node=n, parent=parent, pros=pros, cons=cons, negations=negations,
        )

    @app.route("/submit", methods=["POST"])
    def submit():
        text = (request.form.get("text") or "").strip()
        if not text:
            return redirect(url_for("index"))
        nodes = state.replay()
        result = classify_claim(text, nodes, state.node_embeddings, state.classifier)
        # Below the confidence floor we treat as new even if the classifier flagged
        if (
            result.verdict == "new"
            or result.confidence < _DEDUP_CONFIDENCE_FLOOR
            or result.related_to is None
        ):
            new_id = create_root(state, text)
            return redirect(url_for("view_node", nid=new_id))
        # duplicate or negation with high confidence — show review page
        match = nodes.get(result.related_to)
        return render_template(
            "submit_review.html",
            text=text,
            classification=result,
            match=match,
        )

    @app.route("/submit/confirm", methods=["POST"])
    def submit_confirm():
        text = (request.form.get("text") or "").strip()
        action = request.form.get("action")
        related_to = request.form.get("related_to")
        if not text or not action:
            return redirect(url_for("index"))
        nodes = state.replay()

        if action == "use_existing":
            if related_to and related_to in nodes:
                return redirect(url_for("view_node", nid=related_to))
            return redirect(url_for("index"))

        if action == "force_new":
            new_id = create_root(state, text)
            return redirect(url_for("view_node", nid=new_id))

        if action == "link_negation":
            new_id = create_root(state, text)
            if related_to and related_to in nodes:
                link_negation(state, new_id, related_to, reason="user-confirmed at submit")
            return redirect(url_for("view_node", nid=new_id))

        return redirect(url_for("index"))

    @app.route("/node/<nid>/explain", methods=["POST"])
    def explain_route(nid: str):
        try:
            explain_node(state, nid)
        except ValueError:
            abort(404)
        # Bring the user back to where they came from so the now-cached reasoning
        # appears in context (e.g., a child's reasoning shows up in the parent's view).
        return redirect(request.referrer or url_for("view_node", nid=nid))

    @app.route("/node/<nid>/add_child", methods=["POST"])
    def add_child_route(nid: str):
        text = (request.form.get("text") or "").strip()
        stance = request.form.get("stance", "pro")
        if not text or stance not in ("pro", "con"):
            return redirect(url_for("view_node", nid=nid))
        try:
            add_child(state, nid, text, stance)
        except ValueError:
            abort(404)
        return redirect(url_for("view_node", nid=nid))

    @app.route("/node/<nid>/expand", methods=["POST"])
    def expand(nid: str):
        n_pros = int(request.form.get("n_pros", 2))
        n_cons = int(request.form.get("n_cons", 2))
        try:
            expand_node(state, nid, n_pros=n_pros, n_cons=n_cons)
        except ValueError:
            abort(404)
        return redirect(url_for("view_node", nid=nid))

    @app.route("/node/<nid>/delete", methods=["POST"])
    def delete(nid: str):
        nodes = state.replay()
        if nid not in nodes:
            abort(404)
        soft_delete(state, nid)
        parent = nodes[nid].parent_id
        if parent:
            return redirect(url_for("view_node", nid=parent))
        return redirect(url_for("index"))

    return app


def main():
    state = AppState.from_dir(Path(__file__).resolve().parent / "data")
    app = create_app(state)
    app.run(host="127.0.0.1", port=8295, debug=True)


if __name__ == "__main__":
    main()
