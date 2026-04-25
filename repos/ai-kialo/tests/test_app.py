"""Flask integration tests using the test_client + fake classifier/expander.

No LLM calls. Pipeline logic (submit/dedup/expand/delete/view) exercised via fakes.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import AppState, create_app
from classify import ClassifierResult
from event_log import EventLog
from expand import GeneratedChild
from node_embeddings import NodeEmbeddings


# ---------- fakes ----------

@dataclass
class FakeClassifier:
    """Returns a canned ClassifierResult; optionally pluggable via responder."""
    default: ClassifierResult = field(
        default_factory=lambda: ClassifierResult("new", None, 1.0, "fake")
    )
    responder: Callable | None = None
    calls: list = field(default_factory=list)

    def classify(self, text, candidates):
        self.calls.append((text, list(candidates)))
        if self.responder is not None:
            return self.responder(text, candidates)
        return self.default


@dataclass
class FakeExpander:
    """Records each expand/score_argument call with all kwargs; returns canned data."""
    children_to_return: list[GeneratedChild] = field(default_factory=list)
    score_to_return: str | None = "moderate"
    score_should_raise: bool = False
    score_claim_to_return: str | None = "moderate"
    score_claim_should_raise: bool = False
    explain_argument_to_return: str | None = "fake-arg-reasoning"
    explain_argument_should_raise: bool = False
    explain_claim_to_return: str | None = "fake-claim-reasoning"
    explain_claim_should_raise: bool = False
    calls: list = field(default_factory=list)
    score_calls: list = field(default_factory=list)
    score_claim_calls: list = field(default_factory=list)
    explain_argument_calls: list = field(default_factory=list)
    explain_claim_calls: list = field(default_factory=list)

    def expand(self, claim_text, n_pros=2, n_cons=2, existing_pros=(), existing_cons=()):
        existing_pros = list(existing_pros)
        existing_cons = list(existing_cons)
        self.calls.append((claim_text, n_pros, n_cons, existing_pros, existing_cons))
        if self.children_to_return:
            return list(self.children_to_return)
        po = len(existing_pros)
        co = len(existing_cons)
        return [
            GeneratedChild(
                text=f"{claim_text} pro {po + i}", stance="pro", label="strong",
            ) for i in range(n_pros)
        ] + [
            GeneratedChild(
                text=f"{claim_text} con {co + i}", stance="con", label="weak",
            ) for i in range(n_cons)
        ]

    def score_argument(self, parent_text, child_text, stance):
        self.score_calls.append((parent_text, child_text, stance))
        if self.score_should_raise:
            raise RuntimeError("simulated scoring failure")
        return self.score_to_return

    def score_claim(self, claim_text):
        self.score_claim_calls.append(claim_text)
        if self.score_claim_should_raise:
            raise RuntimeError("simulated claim-scoring failure")
        return self.score_claim_to_return

    def explain_argument(self, parent_text, child_text, stance, label):
        self.explain_argument_calls.append((parent_text, child_text, stance, label))
        if self.explain_argument_should_raise:
            raise RuntimeError("simulated explain-arg failure")
        return self.explain_argument_to_return

    def explain_claim(self, claim_text, label):
        self.explain_claim_calls.append((claim_text, label))
        if self.explain_claim_should_raise:
            raise RuntimeError("simulated explain-claim failure")
        return self.explain_claim_to_return


# ---------- fixtures ----------

@pytest.fixture
def state(tmp_path):
    return AppState(
        data_dir=tmp_path,
        event_log=EventLog(tmp_path / "events.jsonl"),
        node_embeddings=NodeEmbeddings(tmp_path / "node_embeddings.bin"),
        classifier=FakeClassifier(),
        expander=FakeExpander(),
    )


@pytest.fixture
def client(state):
    app = create_app(state)
    app.config["TESTING"] = True
    return app.test_client()


# ---------- index ----------

def test_index_renders_when_empty(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Submit a claim" in r.data
    assert b"No claims yet" in r.data


def test_inline_legend_in_expanded_explanation(client, state):
    """The score-scale ? disclosure should appear at the end of each open reasoning panel
    (not in the header anymore). Triggered by viewing a node that has cached reasoning."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "very strong"
    state.expander.explain_claim_to_return = "explanation text"
    client.post("/submit", data={"text": "the claim"})
    nodes = state.replay()
    nid = next(iter(nodes))
    client.post(f"/node/{nid}/explain")  # populate reasoning so the panel opens

    body = client.get(f"/node/{nid}").data.decode()
    assert 'class="legend-inline"' in body
    # all five GRADE labels appear inside the legend
    for word in ("very strong", "strong", "moderate", "weak", "very weak"):
        assert word in body, f"legend missing label {word!r}"
    for n in (1, 2, 3, 4, 5):
        assert f"score-{n}" in body


def test_header_does_not_have_score_legend(client):
    """The score legend was moved out of the header to the bottom of each explanation."""
    body = client.get("/").data.decode()
    assert 'class="legend"' not in body  # the old header legend is gone
    assert "Score scale" not in body


def test_index_lists_root_claims(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "fake")
    client.post("/submit", data={"text": "first claim"})
    client.post("/submit", data={"text": "second claim"})
    r = client.get("/")
    assert r.status_code == 200
    assert b"first claim" in r.data
    assert b"second claim" in r.data


# ---------- submit: new ----------

def test_submit_new_claim_creates_and_redirects(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "fake")
    r = client.post("/submit", data={"text": "vaccines are safe"})
    assert r.status_code == 302
    assert "/node/" in r.headers["Location"]


def test_submit_empty_text_redirects_to_index(client):
    r = client.post("/submit", data={"text": "   "})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/")


def test_submit_low_confidence_treated_as_new(client, state):
    """Even when classifier says duplicate, if confidence < floor we just create."""
    state.classifier.default = ClassifierResult("duplicate", "n_phantom", 0.2, "uncertain")
    r = client.post("/submit", data={"text": "fresh claim"})
    assert r.status_code == 302
    assert "/node/" in r.headers["Location"]


# ---------- submit: dedup review page ----------

def test_submit_duplicate_shows_review_page(client, state):
    """Seed an existing claim; classifier flags submission as duplicate."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "vaccines are safe"})

    nodes = state.replay()
    existing_id = next(iter(nodes))
    state.classifier.default = ClassifierResult("duplicate", existing_id, 0.95, "same claim")

    r = client.post("/submit", data={"text": "vaccination is safe"})
    assert r.status_code == 200
    assert b"This looks like an existing claim" in r.data
    assert b"vaccination is safe" in r.data
    assert b"vaccines are safe" in r.data
    assert b"Use the existing claim" in r.data
    assert b"Create as new claim anyway" in r.data


def test_submit_negation_shows_review_page(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "vaccines are safe"})

    nodes = state.replay()
    existing_id = next(iter(nodes))
    state.classifier.default = ClassifierResult("negation", existing_id, 0.9, "opposite stance")

    r = client.post("/submit", data={"text": "vaccines are dangerous"})
    assert r.status_code == 200
    assert b"link as negation" in r.data.lower() or b"Create new + link as negation" in r.data


# ---------- submit confirm ----------

def test_submit_confirm_use_existing_redirects_to_existing(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "original"})
    nodes = state.replay()
    existing_id = next(iter(nodes))

    r = client.post("/submit/confirm", data={
        "text": "anything",
        "action": "use_existing",
        "related_to": existing_id,
    })
    assert r.status_code == 302
    assert f"/node/{existing_id}" in r.headers["Location"]


def test_submit_confirm_force_new_creates_node(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    r = client.post("/submit/confirm", data={
        "text": "even though it's a dup",
        "action": "force_new",
    })
    assert r.status_code == 302
    nodes = state.replay()
    texts = [n.text for n in nodes.values()]
    assert "even though it's a dup" in texts


def test_submit_confirm_link_negation_creates_node_and_links(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "X is true"})
    nodes = state.replay()
    existing_id = next(iter(nodes))

    r = client.post("/submit/confirm", data={
        "text": "X is false",
        "action": "link_negation",
        "related_to": existing_id,
    })
    assert r.status_code == 302
    nodes = state.replay()
    new_node = next(n for n in nodes.values() if n.text == "X is false")
    assert existing_id in new_node.negates
    assert new_node.id in nodes[existing_id].negates


# ---------- view node ----------

def test_view_node_404_for_missing(client):
    assert client.get("/node/abc123").status_code == 404


def test_view_node_renders_text_and_actions(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "my claim"})
    nodes = state.replay()
    nid = next(iter(nodes))
    r = client.get(f"/node/{nid}")
    assert r.status_code == 200
    assert b"my claim" in r.data
    assert b"Expand" in r.data
    assert b"Delete" in r.data


def test_view_merged_node_redirects_to_target(client, state):
    """A node with merged_into set should redirect viewers to the canonical."""
    from app import create_node, merge_node
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    canonical = create_node(state, "canonical", parent_id=None, stance="root")
    dupe = create_node(state, "dupe", parent_id=None, stance="root")
    merge_node(state, dupe, canonical, reason="test")
    r = client.get(f"/node/{dupe}")
    assert r.status_code == 302
    assert f"/node/{canonical}" in r.headers["Location"]


# ---------- expand ----------

def test_expand_creates_pros_and_cons(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "claim X"})
    nodes = state.replay()
    parent_id = next(iter(nodes))

    r = client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "2"})
    assert r.status_code == 302

    nodes = state.replay()
    parent = nodes[parent_id]
    assert len(parent.children) == 4
    pros = [nodes[c] for c in parent.children if nodes[c].stance == "pro"]
    cons = [nodes[c] for c in parent.children if nodes[c].stance == "con"]
    assert len(pros) == 2
    assert len(cons) == 2


def test_expand_calls_expander_with_parent_text(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "specific parent text"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "1"})
    assert state.expander.calls
    text, n_pros, n_cons, existing_pros, existing_cons = state.expander.calls[0]
    assert text == "specific parent text"
    assert n_pros == 1
    assert n_cons == 1
    # No prior children — empty existing lists
    assert existing_pros == []
    assert existing_cons == []


def test_reexpand_passes_existing_children_to_expander(client, state):
    """Second expand should include the first call's outputs in existing_pros/cons."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    # First expand: creates 2 pros + 2 cons
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "2"})
    # Second expand: should pass the 4 created children as existing_pros/cons
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "2"})
    assert len(state.expander.calls) == 2
    _, _, _, existing_pros, existing_cons = state.expander.calls[1]
    assert len(existing_pros) == 2
    assert len(existing_cons) == 2
    # texts from first round
    assert "parent pro 0" in existing_pros
    assert "parent con 0" in existing_cons


def test_reexpand_excludes_deleted_children_from_existing(client, state):
    """Deleted children must not appear in the existing_pros/cons passed on re-expand."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "0"})
    nodes = state.replay()
    pros = [c for c in nodes[parent_id].children if nodes[c].stance == "pro"]
    client.post(f"/node/{pros[0]}/delete")
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    _, _, _, existing_pros, _ = state.expander.calls[1]
    assert len(existing_pros) == 1  # the deleted one is gone
    assert nodes[pros[0]].text not in existing_pros
    assert nodes[pros[1]].text in existing_pros


def test_expand_404_for_missing_node(client):
    r = client.post("/node/nonesuch/expand", data={"n_pros": "1", "n_cons": "1"})
    assert r.status_code == 404


# ---------- delete ----------

def test_delete_marks_node_deleted_and_redirects(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "to be deleted"})
    nodes = state.replay()
    nid = next(iter(nodes))

    r = client.post(f"/node/{nid}/delete")
    assert r.status_code == 302
    nodes = state.replay()
    assert nodes[nid].deleted


def test_delete_child_redirects_to_parent(client, state):
    """Deleting a child should bring the user back to the parent's view."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(c for c in nodes[parent_id].children)
    r = client.post(f"/node/{child_id}/delete")
    assert r.status_code == 302
    assert f"/node/{parent_id}" in r.headers["Location"]


def test_expand_persists_label_via_node_scored(client, state):
    """Each generated child with a label should produce a node_scored event setting it."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "claim"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "2"})
    nodes = state.replay()
    parent = nodes[parent_id]
    pros = [nodes[c] for c in parent.children if nodes[c].stance == "pro"]
    cons = [nodes[c] for c in parent.children if nodes[c].stance == "con"]
    # FakeExpander assigns "strong" to pros, "weak" to cons
    assert all(p.label == "strong" for p in pros)
    assert all(c.label == "weak" for c in cons)


def test_node_view_renders_numeric_score_badges(client, state):
    """Score badge shows the integer; the underlying word label remains as a tooltip."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "claim"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "1"})
    r = client.get(f"/node/{parent_id}")
    assert r.status_code == 200
    # FakeExpander labels pros "strong" (4) and cons "weak" (2)
    assert b"score-4" in r.data
    assert b"score-2" in r.data
    # the word labels must NOT appear as visible text — they're tooltips only
    assert b">strong<" not in r.data
    assert b">weak<" not in r.data
    # but the title attribute (tooltip) should carry the word; it now also includes
    # the "click for an explanation" affordance text since reasoning is uncached
    assert b'title="strong' in r.data
    assert b'title="weak' in r.data


def test_node_view_sorts_pros_descending_by_score(client, state):
    """Pros list should be ordered by score descending (very strong → very weak)."""
    from app import create_node
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))

    # Add three pros in mixed-up order with different label scores
    weak_id = create_node(state, "weak pro text", parent_id, "pro")
    state.event_log.append({"t": "node_scored", "id": weak_id, "label": "weak"})
    very_strong_id = create_node(state, "very strong pro text", parent_id, "pro")
    state.event_log.append({"t": "node_scored", "id": very_strong_id, "label": "very strong"})
    moderate_id = create_node(state, "moderate pro text", parent_id, "pro")
    state.event_log.append({"t": "node_scored", "id": moderate_id, "label": "moderate"})

    body = client.get(f"/node/{parent_id}").data.decode()
    pos_very_strong = body.find("very strong pro text")
    pos_moderate = body.find("moderate pro text")
    pos_weak = body.find("weak pro text")
    assert -1 < pos_very_strong < pos_moderate < pos_weak


def test_node_view_sorts_cons_descending_by_score(client, state):
    from app import create_node
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))

    unconv_id = create_node(state, "unconv con text", parent_id, "con")
    state.event_log.append({"t": "node_scored", "id": unconv_id, "label": "very weak"})
    strong_id = create_node(state, "strong con text", parent_id, "con")
    state.event_log.append({"t": "node_scored", "id": strong_id, "label": "strong"})

    body = client.get(f"/node/{parent_id}").data.decode()
    assert -1 < body.find("strong con text") < body.find("unconv con text")


# ---------- add_child ----------

def test_add_child_creates_node_with_stance(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))

    r = client.post(
        f"/node/{parent_id}/add_child",
        data={"text": "my custom pro", "stance": "pro"},
    )
    assert r.status_code == 302
    assert f"/node/{parent_id}" in r.headers["Location"]
    nodes = state.replay()
    child = next(n for n in nodes.values() if n.text == "my custom pro")
    assert child.parent_id == parent_id
    assert child.stance == "pro"


def test_add_child_calls_scorer_and_persists_label(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_to_return = "very strong"
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))

    client.post(
        f"/node/{parent_id}/add_child",
        data={"text": "user-written con", "stance": "con"},
    )
    nodes = state.replay()
    child = next(n for n in nodes.values() if n.text == "user-written con")
    assert child.label == "very strong"

    # scorer was called with parent text + child text + stance
    assert state.expander.score_calls
    parent_text, child_text, stance = state.expander.score_calls[0]
    assert parent_text == "parent"
    assert child_text == "user-written con"
    assert stance == "con"


def test_add_child_creates_node_even_if_scoring_fails(client, state):
    """Scoring is best-effort — a scoring exception must not block node creation."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_should_raise = True
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))

    r = client.post(
        f"/node/{parent_id}/add_child",
        data={"text": "argument that scoring will fail on", "stance": "pro"},
    )
    assert r.status_code == 302
    nodes = state.replay()
    child = next(n for n in nodes.values() if n.text.startswith("argument that scoring"))
    assert child.label is None  # no node_scored event written


def test_add_child_empty_text_redirects_without_creating(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    before = sum(1 for _ in state.event_log.iter_events())
    r = client.post(f"/node/{parent_id}/add_child", data={"text": "   ", "stance": "pro"})
    assert r.status_code == 302
    after = sum(1 for _ in state.event_log.iter_events())
    assert before == after
    assert state.expander.score_calls == []


def test_add_child_invalid_stance_rejected(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    r = client.post(
        f"/node/{parent_id}/add_child",
        data={"text": "x", "stance": "neutral"},
    )
    assert r.status_code == 302
    nodes = state.replay()
    assert all(n.text != "x" for n in nodes.values())


def test_add_child_unknown_parent_404(client):
    r = client.post(
        "/node/nonesuch/add_child",
        data={"text": "anything", "stance": "pro"},
    )
    assert r.status_code == 404


def test_add_child_appears_in_parent_view(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_to_return = "strong"
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(
        f"/node/{parent_id}/add_child",
        data={"text": "manually added pro", "stance": "pro"},
    )
    r = client.get(f"/node/{parent_id}")
    assert b"manually added pro" in r.data
    assert b"score-4" in r.data  # "strong" maps to 4


def test_submit_scores_root_claim(client, state):
    """A new root claim should be scored via score_claim and have its label persisted."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "very strong"
    client.post("/submit", data={"text": "vaccines are safe"})
    nodes = state.replay()
    root = next(iter(nodes.values()))
    assert root.label == "very strong"
    assert state.expander.score_claim_calls == ["vaccines are safe"]


def test_submit_force_new_scores_root(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "weak"
    r = client.post("/submit/confirm", data={
        "text": "fringe claim",
        "action": "force_new",
    })
    assert r.status_code == 302
    nodes = state.replay()
    root = next(n for n in nodes.values() if n.text == "fringe claim")
    assert root.label == "weak"


def test_submit_link_negation_scores_root(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "moderate"
    # First create something to negate
    client.post("/submit", data={"text": "X is true"})
    nodes = state.replay()
    existing_id = next(iter(nodes))
    state.expander.score_claim_calls.clear()

    r = client.post("/submit/confirm", data={
        "text": "X is false",
        "action": "link_negation",
        "related_to": existing_id,
    })
    assert r.status_code == 302
    nodes = state.replay()
    new_root = next(n for n in nodes.values() if n.text == "X is false")
    assert new_root.label == "moderate"
    assert state.expander.score_claim_calls == ["X is false"]


def test_submit_continues_if_score_claim_raises(client, state):
    """Score failures must not block root creation."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_should_raise = True
    r = client.post("/submit", data={"text": "claim that scoring will fail on"})
    assert r.status_code == 302
    nodes = state.replay()
    root = next(iter(nodes.values()))
    assert root.label is None  # no label persisted


def test_index_shows_root_score_badge(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "strong"
    client.post("/submit", data={"text": "well supported claim"})
    r = client.get("/")
    assert r.status_code == 200
    assert b"score-4" in r.data  # "strong" = 4
    assert b'title="strong' in r.data  # title may include explanation affordance suffix
    assert b"well supported claim" in r.data


def test_node_view_shows_root_score_badge(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "very strong"
    client.post("/submit", data={"text": "claim text here"})
    nodes = state.replay()
    root_id = next(iter(nodes))
    r = client.get(f"/node/{root_id}")
    assert r.status_code == 200
    assert b"score-5" in r.data
    assert b'title="very strong' in r.data


def test_index_sorts_roots_by_score_desc(client, state):
    """Higher-scored roots come first on the index page."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")

    state.expander.score_claim_to_return = "weak"
    client.post("/submit", data={"text": "weak root"})

    state.expander.score_claim_to_return = "very strong"
    client.post("/submit", data={"text": "very strong root"})

    state.expander.score_claim_to_return = "moderate"
    client.post("/submit", data={"text": "moderate root"})

    body = client.get("/").data.decode()
    assert -1 < body.find("very strong root") < body.find("moderate root") < body.find("weak root")


# ---------- lazy reasoning: not generated at score time, only on /explain ----------

def test_submit_does_NOT_persist_reasoning(client, state):
    """Initial scoring stores only the label — reasoning is on-demand only."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "very strong"
    client.post("/submit", data={"text": "the claim"})
    nodes = state.replay()
    root = next(iter(nodes.values()))
    assert root.label == "very strong"
    assert root.reasoning is None
    # FakeExpander.explain_claim must NOT have been called during submit
    assert state.expander.explain_claim_calls == []


def test_add_child_does_NOT_persist_reasoning(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_to_return = "weak"
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(
        f"/node/{parent_id}/add_child",
        data={"text": "user-supplied con", "stance": "con"},
    )
    nodes = state.replay()
    child = next(n for n in nodes.values() if n.text == "user-supplied con")
    assert child.label == "weak"
    assert child.reasoning is None
    assert state.expander.explain_argument_calls == []


def test_expand_does_NOT_persist_reasoning_per_child(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "1"})
    nodes = state.replay()
    parent = nodes[parent_id]
    for cid in parent.children:
        c = nodes[cid]
        assert c.label  # children DO have labels
        assert c.reasoning is None  # but no reasoning


def test_node_view_score_badge_is_clickable_when_no_reasoning(client, state):
    """When there's a label but no cached reasoning, the badge renders as a submit button
    inside a form pointing at /explain — clicking the score itself triggers the LLM."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "very strong"
    client.post("/submit", data={"text": "the claim"})
    nodes = state.replay()
    nid = next(iter(nodes))
    body = client.get(f"/node/{nid}").data.decode()
    # Form points at /explain
    assert f'/node/{nid}/explain' in body
    # Badge is a <button> styled with the score class
    assert '<button type="submit"' in body
    assert 'class="label score-5"' in body
    # The score badge itself should NOT be wrapped in a reasoning disclosure yet
    # (the header score-legend <details> is a different element with class="legend")
    assert 'class="reasoning-box"' not in body


def test_index_score_badge_is_clickable_when_no_reasoning(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "strong"
    client.post("/submit", data={"text": "first claim"})
    nodes = state.replay()
    nid = next(iter(nodes))
    body = client.get("/").data.decode()
    assert f'/node/{nid}/explain' in body
    assert '<button type="submit"' in body
    assert 'class="label score-4"' in body


def test_explain_route_calls_explain_claim_for_root(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "strong"
    state.expander.explain_claim_to_return = "Backed by multiple peer-reviewed studies."
    client.post("/submit", data={"text": "vaccines work"})
    nodes = state.replay()
    nid = next(iter(nodes))

    r = client.post(f"/node/{nid}/explain")
    assert r.status_code == 302
    nodes = state.replay()
    assert nodes[nid].reasoning == "Backed by multiple peer-reviewed studies."
    # must have used explain_claim (not explain_argument) since this is a root
    assert state.expander.explain_claim_calls == [("vaccines work", "strong")]
    assert state.expander.explain_argument_calls == []


def test_explain_route_calls_explain_argument_for_child(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.explain_argument_to_return = "Concrete data point with cited source."
    client.post("/submit", data={"text": "the parent claim"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(c for c in nodes[parent_id].children)
    child = nodes[child_id]

    r = client.post(f"/node/{child_id}/explain")
    assert r.status_code == 302
    nodes = state.replay()
    assert nodes[child_id].reasoning == "Concrete data point with cited source."
    # must have used explain_argument
    assert len(state.expander.explain_argument_calls) == 1
    parent_text, child_text, stance, label = state.expander.explain_argument_calls[0]
    assert parent_text == "the parent claim"
    assert child_text == child.text
    assert stance == child.stance
    assert label == child.label


def test_explain_route_404_for_missing_node(client):
    r = client.post("/node/nonesuch/explain")
    assert r.status_code == 404


def test_explain_route_does_nothing_when_node_has_no_label(client, state):
    """If a node never got a label, explain has nothing to explain — no LLM call, no event."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = None  # initial scoring fails → no label
    client.post("/submit", data={"text": "unscored"})
    nodes = state.replay()
    nid = next(iter(nodes))
    assert nodes[nid].label is None

    r = client.post(f"/node/{nid}/explain")
    assert r.status_code == 302
    nodes = state.replay()
    assert nodes[nid].reasoning is None
    assert state.expander.explain_claim_calls == []
    assert state.expander.explain_argument_calls == []


def test_explain_failure_does_not_overwrite_state(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.explain_claim_should_raise = True
    client.post("/submit", data={"text": "claim"})
    nodes = state.replay()
    nid = next(iter(nodes))
    r = client.post(f"/node/{nid}/explain")
    assert r.status_code == 302
    nodes = state.replay()
    assert nodes[nid].reasoning is None  # nothing persisted on failure


def test_view_after_explain_renders_disclosure(client, state):
    """Once /explain runs, the badge becomes the &lt;summary&gt; of a &lt;details&gt; with cached text."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.explain_claim_to_return = "the cached explanation text"
    client.post("/submit", data={"text": "claim"})
    nodes = state.replay()
    nid = next(iter(nodes))
    client.post(f"/node/{nid}/explain")
    body = client.get(f"/node/{nid}").data.decode()
    assert "the cached explanation text" in body
    # The score badge is now rendered as <summary class="label score-X">, not as a form button
    assert '<summary class="label score-' in body
    assert f'/node/{nid}/explain' not in body  # no form posting to /explain for this root anymore
    assert '<button type="submit" class="label' not in body  # no badge-as-button


def test_explain_redirects_back_to_referrer(client, state):
    """User clicks Why? on a child from the parent's view → redirects back to parent."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(c for c in nodes[parent_id].children)

    r = client.post(
        f"/node/{child_id}/explain",
        headers={"Referer": f"http://localhost/node/{parent_id}"},
    )
    assert r.status_code == 302
    assert f"/node/{parent_id}" in r.headers["Location"]


def test_node_view_renders_add_child_form(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    r = client.get(f"/node/{parent_id}")
    body = r.data.decode()
    assert "Add as Pro" in body
    assert "Add as Con" in body
    assert "/add_child" in body


def test_unlabeled_children_sort_after_labeled(client, state):
    """Children with no score (label=None) drop to the end of the list."""
    from app import create_node
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))

    unlabeled_id = create_node(state, "unlabeled text", parent_id, "pro")
    moderate_id = create_node(state, "moderate text", parent_id, "pro")
    state.event_log.append({"t": "node_scored", "id": moderate_id, "label": "moderate"})

    body = client.get(f"/node/{parent_id}").data.decode()
    assert body.find("moderate text") < body.find("unlabeled text")


def test_expand_with_labelless_children_does_not_emit_node_scored(client, state):
    """If a generated child has label=None, no node_scored event is appended."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.children_to_return = [
        GeneratedChild(text="unlabeled pro", stance="pro", label=None),
    ]
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    # Wipe so we get exactly one event from the expand
    starting_events = sum(1 for _ in state.event_log.iter_events())
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    new_events = list(state.event_log.iter_events())[starting_events:]
    types = [e["t"] for e in new_events]
    assert "node_created" in types
    assert "node_scored" not in types


def test_deleted_node_excluded_from_parent_children_view(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "0"})
    nodes = state.replay()
    pros = [c for c in nodes[parent_id].children if nodes[c].stance == "pro"]
    client.post(f"/node/{pros[0]}/delete")
    r = client.get(f"/node/{parent_id}")
    assert r.status_code == 200
    # only one pro should be visible after deletion
    assert nodes[pros[1]].text.encode() in r.data
    assert nodes[pros[0]].text.encode() not in r.data
