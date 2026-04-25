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

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from flask import Flask, abort, redirect, render_template, request, url_for

from classify import Classifier, ClaimClassification, LlamaCppClassifier, classify_claim
from embeddings import sentence_embed
from event_log import EventLog, Node
from expand import ContainmentResult, Expander, LlamaCppExpander, label_score
from node_embeddings import NodeEmbeddings
from similarity import find_similar_in_nodes


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
    # When True, /expand runs the streaming generation synchronously rather than
    # spawning a thread — used by the test suite so assertions on event-log state
    # don't race the background worker.
    expand_synchronously: bool = False

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


def _persist_standalone_reasoning(state: AppState, nid: str, reasoning: str | None) -> None:
    if not reasoning:
        return
    state.event_log.append({
        "t": "node_scored", "id": nid,
        "standalone_reasoning": reasoning, "src": "explainer",
    })


def ensure_standalone_score(state: AppState, nid: str) -> None:
    """For non-root nodes only: compute standalone label + self-containment if absent.

    Called synchronously on first view of a non-root's own page. After the first
    visit the result is cached in the event log so subsequent views are fast.
    """
    nodes = state.replay()
    n = nodes.get(nid)
    if n is None or n.parent_id is None:
        return
    if n.deleted or n.merged_into is not None:
        return

    if n.standalone_label is None:
        ancestors = ancestors_payload(n, nodes)
        label = _try_call(lambda: state.expander.score_claim(n.text, ancestors=ancestors))
        if label is not None:
            state.event_log.append({
                "t": "node_scored", "id": nid,
                "standalone_label": label, "src": "scorer",
            })

    if n.containment is None:
        parent = nodes.get(n.parent_id)
        if parent is not None:
            sc = _try_call(lambda: state.expander.check_containment(parent.text, n.text))
            if isinstance(sc, ContainmentResult):
                state.event_log.append({
                    "t": "node_scored", "id": nid,
                    "containment": sc.containment,
                    "containment_reasoning": sc.reasoning,
                    "src": "scorer",
                })


def create_root(state: AppState, text: str) -> str:
    """Create a root node and best-effort score it via score_claim (label only — no reasoning).

    Roots have no ancestors so we pass none — the standalone scorer rates them
    purely on their own merits.
    """
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
    ancestors = ancestors_payload(parent, nodes)
    _persist_label(state, nid, _try_call(
        lambda: state.expander.score_argument(parent.text, text, stance, ancestors=ancestors)
    ))
    return nid


_DEDUP_COSINE_THRESHOLD = 0.85


def _find_dedup_match(state: AppState, text: str, stance: str, parent_id: str) -> str | None:
    """Find an existing live same-stance node whose text is similar enough to be
    treated as a duplicate. Excludes the parent itself and nodes already under
    this parent (so we never alias something that's already a real child).
    Returns the existing node id, or None if no match.
    """
    nodes = state.replay()
    parent = nodes.get(parent_id)
    parent_children = set(parent.children) if parent is not None else set()
    candidates = [
        n.id for n in nodes.values()
        if n.stance == stance
        and not n.deleted
        and n.merged_into is None
        and n.id != parent_id
        and n.id not in parent_children
    ]
    if not candidates:
        return None
    results = find_similar_in_nodes(
        text, nodes, state.node_embeddings,
        k=1, threshold=_DEDUP_COSINE_THRESHOLD, scope=candidates,
    )
    return results[0][0] if results else None


def _expand_one_child(state: AppState, parent_id: str, stance: str) -> str | None:
    """Generate ONE child of the given stance and write it immediately.

    Uses a fresh replay each call so existing-pros/cons (and the ancestor chain)
    reflect anything written by previous calls or by other concurrent threads.
    Errors swallowed: a failed individual child shouldn't kill the rest of the run.

    Before persisting a generated child, runs an embedding-based dedup check.
    If the generated text is sufficiently similar (cosine ≥ 0.85) to an existing
    same-stance node elsewhere in the tree, we emit a `node_aliased` event
    linking that node as an additional child of this parent — instead of
    creating a new (likely-duplicate) node.
    """
    nodes = state.replay()
    parent = nodes.get(parent_id)
    if parent is None or parent.deleted or parent.merged_into is not None:
        return None
    existing_pros = [nodes[c].text for c in parent.children if nodes[c].stance == "pro"]
    existing_cons = [nodes[c].text for c in parent.children if nodes[c].stance == "con"]
    ancestors = ancestors_payload(parent, nodes)
    try:
        specs = state.expander.expand(
            parent.text,
            n_pros=1 if stance == "pro" else 0,
            n_cons=1 if stance == "con" else 0,
            existing_pros=existing_pros,
            existing_cons=existing_cons,
            ancestors=ancestors,
        )
    except Exception:
        return None
    new_id: str | None = None
    for spec in specs:
        # Dedup before creating: if we find a same-stance node with very similar
        # text already in the tree, link to it instead of duplicating.
        match_id = _find_dedup_match(state, spec.text, spec.stance, parent_id)
        if match_id is not None:
            state.event_log.append({
                "t": "node_aliased",
                "parent": parent_id,
                "child": match_id,
                "reason": "embedding-cosine match during expansion",
                "src": "expander",
            })
            new_id = match_id
            continue
        nid = create_node(state, spec.text, parent_id=parent.id, stance=spec.stance)
        new_id = nid
        if spec.label is not None:
            state.event_log.append({
                "t": "node_scored", "id": nid, "label": spec.label, "src": "expander",
            })
    return new_id


def expand_node(state: AppState, parent_id: str, n_pros: int = 2, n_cons: int = 2) -> None:
    """Stream pros/cons under `parent_id`, one at a time.

    Each child is written to the event log as soon as its LLM call returns, so
    a UI polling the page sees them appear incrementally rather than all at once.
    Designed to be runnable either synchronously (tests) or in a background thread
    (the /expand route in production); thread-safe because each call re-replays
    the event log to compute fresh existing-pros/cons.
    """
    nodes = state.replay()
    parent = nodes.get(parent_id)
    if parent is None or parent.deleted or parent.merged_into is not None:
        raise ValueError(f"parent {parent_id!r} is missing, deleted, or merged")
    for _ in range(max(0, n_pros)):
        _expand_one_child(state, parent_id, "pro")
    for _ in range(max(0, n_cons)):
        _expand_one_child(state, parent_id, "con")


def explain_node(state: AppState, nid: str, axis: str = "relational") -> str | None:
    """Generate (and persist) an explanation for `nid`'s score along the chosen axis.

    Axes:
      - "relational" (default): explain why this node has its relational rating as a
        pro/con of its parent. For non-roots writes `reasoning`; for roots there's
        no relational axis so this falls through to standalone.
      - "standalone": explain why this node has its standalone rating. For non-roots
        writes `standalone_reasoning`. For roots writes `reasoning` (since for roots
        the existing `label`/`reasoning` IS standalone).
    """
    nodes = state.replay()
    n = nodes.get(nid)
    if n is None or n.deleted or n.merged_into is not None:
        raise ValueError(f"node {nid!r} is missing, deleted, or merged")

    is_root = n.parent_id is None

    if is_root:
        # Roots only have a standalone axis — both axis values map there
        if not n.label:
            return None
        reasoning = _try_call(lambda: state.expander.explain_claim(n.text, n.label))
        _persist_reasoning(state, nid, reasoning)
        return reasoning

    if axis == "standalone":
        if not n.standalone_label:
            return None
        ancestors = ancestors_payload(n, nodes)
        reasoning = _try_call(
            lambda: state.expander.explain_claim(n.text, n.standalone_label, ancestors=ancestors)
        )
        _persist_standalone_reasoning(state, nid, reasoning)
        return reasoning

    # axis == "relational" (default for non-roots)
    if not n.label:
        return None
    parent = nodes.get(n.parent_id)
    if parent is None:
        return None
    ancestors = ancestors_payload(parent, nodes)
    reasoning = _try_call(
        lambda: state.expander.explain_argument(parent.text, n.text, n.stance, n.label, ancestors=ancestors)
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


SORT_OPTIONS = ("score", "newest", "oldest")


def _sort_children(nodes_list: list[Node], sort_by: str) -> list[Node]:
    """Apply the requested sort to a list of children. Falls back to score on
    unknown values so URL-injected garbage can't break the page."""
    if sort_by == "newest":
        return sorted(nodes_list, key=lambda n: -n.created_at)
    if sort_by == "oldest":
        return sorted(nodes_list, key=lambda n: n.created_at)
    return _by_score_desc(nodes_list)


def context_chain(node: Node, nodes: dict[str, Node]) -> list[Node]:
    """Walk up the parent chain from `node`'s parent (inclusive), stopping at the
    first self-contained ancestor or at the root. Returns Nodes closest-first.

    Used when feeding a claim to the LLM that itself references its parent — the
    chain gives enough context for the LLM to interpret the claim before rating.
    """
    chain: list[Node] = []
    cur = node
    while cur.parent_id is not None:
        parent = nodes.get(cur.parent_id)
        if parent is None or parent.deleted or parent.merged_into is not None:
            break
        chain.append(parent)
        if parent.containment == "self-contained":
            break
        if parent.parent_id is None:  # hit root, stop
            break
        cur = parent
    return chain


def ancestors_payload(node: Node, nodes: dict[str, Node]) -> list[tuple[str, str]]:
    """Convert `context_chain` to the (text, stance) tuples Expander methods consume."""
    return [(p.text, p.stance) for p in context_chain(node, nodes)]


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

        # First view of a non-root's own page: synchronously compute the standalone
        # score + self-containment check. Cached afterwards via node_scored events.
        if n.parent_id is not None:
            ensure_standalone_score(state, nid)
            nodes = state.replay()
            n = nodes[nid]

        sort_by = request.args.get("sort", "score")
        if sort_by not in SORT_OPTIONS:
            sort_by = "score"
        pros = _sort_children(_live(nodes, [c for c in n.children if nodes[c].stance == "pro"]), sort_by)
        cons = _sort_children(_live(nodes, [c for c in n.children if nodes[c].stance == "con"]), sort_by)
        parent = nodes.get(n.parent_id) if n.parent_id else None
        negations = _live(nodes, n.negates)

        # Streaming expansion: when /expand redirected with ?expanding=N, show a
        # banner + auto-refresh until the live child count catches up. After that
        # the param is harmless — the banner just stops rendering.
        expanding_total = request.args.get("expanding", type=int)
        live_children_count = len(pros) + len(cons)
        is_expanding = (
            expanding_total is not None
            and live_children_count < expanding_total
        )

        return render_template(
            "node.html",
            node=n, parent=parent, pros=pros, cons=cons, negations=negations,
            is_expanding=is_expanding,
            expanding_total=expanding_total,
            live_children_count=live_children_count,
            sort_by=sort_by,
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

    @app.route("/node/<nid>/toggle_containment", methods=["POST"])
    def toggle_containment_route(nid: str):
        nodes = state.replay()
        n = nodes.get(nid)
        if n is None or n.deleted or n.merged_into is not None:
            abort(404)
        if n.parent_id is None:
            # roots don't have a containment axis (no parent to reference)
            abort(400)
        # Flip whatever's currently set; if it was None for some reason, default
        # to flipping from self-contained (i.e. set to references-parent).
        new_value = (
            "references-parent" if n.containment == "self-contained" else "self-contained"
        )
        state.event_log.append({
            "t": "node_scored", "id": nid,
            "containment": new_value,
            "containment_reasoning": "manually set by user",
            "src": "user",
        })
        return redirect(request.referrer or url_for("view_node", nid=nid))

    @app.route("/node/<nid>/explain", methods=["POST"])
    def explain_route(nid: str):
        axis = request.form.get("axis", "relational")
        if axis not in ("relational", "standalone"):
            axis = "relational"
        try:
            explain_node(state, nid, axis=axis)
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
        nodes = state.replay()
        parent = nodes.get(nid)
        if parent is None or parent.deleted or parent.merged_into is not None:
            abort(404)
        live_count = sum(
            1 for c in parent.children
            if not nodes[c].deleted and nodes[c].merged_into is None
        )
        expected_total = live_count + n_pros + n_cons

        if state.expand_synchronously:
            expand_node(state, nid, n_pros=n_pros, n_cons=n_cons)
        else:
            threading.Thread(
                target=expand_node,
                args=(state, nid, n_pros, n_cons),
                daemon=True,
            ).start()

        # Preserve sort if the form carried it through, so the user stays on
        # their chosen ordering after the polling reload finishes.
        redirect_args = {"nid": nid, "expanding": expected_total}
        sort_by = request.form.get("sort")
        if sort_by in SORT_OPTIONS:
            redirect_args["sort"] = sort_by
        return redirect(url_for("view_node", **redirect_args))

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
