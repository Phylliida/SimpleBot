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
from expand import GeneratedChild, ContainmentResult
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
    containment_to_return: ContainmentResult | None = field(
        default_factory=lambda: ContainmentResult(containment="self-contained", reasoning="fake-sc")
    )
    containment_should_raise: bool = False
    calls: list = field(default_factory=list)
    score_calls: list = field(default_factory=list)
    score_claim_calls: list = field(default_factory=list)
    explain_argument_calls: list = field(default_factory=list)
    explain_claim_calls: list = field(default_factory=list)
    containment_calls: list = field(default_factory=list)

    def expand(self, claim_text, n_pros=2, n_cons=2, existing_pros=(), existing_cons=(), ancestors=()):
        existing_pros = list(existing_pros)
        existing_cons = list(existing_cons)
        ancestors = list(ancestors)
        self.calls.append((claim_text, n_pros, n_cons, existing_pros, existing_cons, ancestors))
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

    def score_argument(self, parent_text, child_text, stance, ancestors=()):
        self.score_calls.append((parent_text, child_text, stance, list(ancestors)))
        if self.score_should_raise:
            raise RuntimeError("simulated scoring failure")
        return self.score_to_return

    def score_claim(self, claim_text, ancestors=()):
        self.score_claim_calls.append((claim_text, list(ancestors)))
        if self.score_claim_should_raise:
            raise RuntimeError("simulated claim-scoring failure")
        return self.score_claim_to_return

    def explain_argument(self, parent_text, child_text, stance, label, ancestors=()):
        self.explain_argument_calls.append((parent_text, child_text, stance, label, list(ancestors)))
        if self.explain_argument_should_raise:
            raise RuntimeError("simulated explain-arg failure")
        return self.explain_argument_to_return

    def explain_claim(self, claim_text, label, ancestors=()):
        self.explain_claim_calls.append((claim_text, label, list(ancestors)))
        if self.explain_claim_should_raise:
            raise RuntimeError("simulated explain-claim failure")
        return self.explain_claim_to_return

    def check_containment(self, parent_text, child_text):
        self.containment_calls.append((parent_text, child_text))
        if self.containment_should_raise:
            raise RuntimeError("simulated containment failure")
        return self.containment_to_return


# ---------- fixtures ----------

@pytest.fixture
def state(tmp_path):
    return AppState(
        data_dir=tmp_path,
        event_log=EventLog(tmp_path / "events.jsonl"),
        node_embeddings=NodeEmbeddings(tmp_path / "node_embeddings.bin"),
        classifier=FakeClassifier(),
        expander=FakeExpander(),
        # tests run /expand inline so assertions on the resulting state
        # don't race a background worker
        expand_synchronously=True,
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


def test_expand_calls_expander_per_child_with_parent_text(client, state):
    """Streaming: one LLM call per child. With n_pros=1, n_cons=1 we expect 2 calls."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "specific parent text"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "1"})
    assert len(state.expander.calls) == 2
    # All calls reference the same parent text and ask for exactly 1 child each
    for text, n_pros, n_cons, _, _, _ in state.expander.calls:
        assert text == "specific parent text"
        assert (n_pros, n_cons) in {(1, 0), (0, 1)}
    pro_calls = [c for c in state.expander.calls if c[1] == 1]
    con_calls = [c for c in state.expander.calls if c[2] == 1]
    assert len(pro_calls) == 1 and len(con_calls) == 1


def test_expand_makes_n_calls_per_n_children(client, state):
    """n_pros=2 + n_cons=3 should fire 5 single-child calls."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "3"})
    assert len(state.expander.calls) == 5


def test_reexpand_grows_existing_children_each_call(client, state):
    """Each per-child call should see the children written by all previous calls.

    With n_pros=2, n_cons=2 followed by another 2+2:
      - Call 1 (pro): existing_pros=[]
      - Call 2 (pro): existing_pros=[pro0]
      - Call 3 (con): existing_cons=[]
      - Call 4 (con): existing_cons=[con0]
      - Call 5 (pro): existing_pros=[pro0, pro1]  ← second /expand starts here
      ...
    """
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "2"})
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "2"})
    assert len(state.expander.calls) == 8
    fifth_call = state.expander.calls[4]
    _, n_pros5, _, existing_pros5, _, _ = fifth_call
    assert n_pros5 == 1
    assert "parent pro 0" in existing_pros5
    assert "parent pro 1" in existing_pros5


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
    state.expander.calls.clear()  # drop calls from the first expand round
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    # one new pro call; existing should have 1 (the surviving pro), not 2
    assert len(state.expander.calls) == 1
    _, _, _, existing_pros, _, _ = state.expander.calls[0]
    assert len(existing_pros) == 1
    assert nodes[pros[0]].text not in existing_pros
    assert nodes[pros[1]].text in existing_pros


def test_expand_records_potential_dupes_when_match_found(client, state):
    """When a generated child is similar enough to an existing same-stance node,
    /expand still creates the new node but emits node_potential_dupe events
    linking back to the candidates."""
    from app import create_node, create_root

    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    create_root(state, "Should we adopt renewable energy?")
    nodes = state.replay()
    root_a = next(iter(nodes))
    create_root(state, "Should we phase out fossil fuels?")
    nodes = state.replay()
    root_b = next(rid for rid in nodes if rid != root_a)

    existing_pro_text = "Renewable energy reduces greenhouse gas emissions"
    existing_pro = create_node(state, existing_pro_text, root_a, "pro")

    state.expander.children_to_return = [
        GeneratedChild(text=existing_pro_text, stance="pro", label="strong"),
    ]
    client.post(f"/node/{root_b}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()

    # A NEW node was created under root_b (the user-generated claim is preserved)
    new_pros = [n for n in nodes.values() if n.parent_id == root_b and n.stance == "pro"]
    assert len(new_pros) == 1
    new_pro = new_pros[0]
    assert new_pro.text == existing_pro_text
    # And it has the existing one recorded as a potential dupe
    assert existing_pro in new_pro.potential_dupes


def test_expand_no_potential_dupes_when_text_dissimilar(client, state):
    """Unrelated text → no potential_dupe events emitted."""
    from app import create_node, create_root

    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    create_root(state, "Cars should be electric")
    nodes = state.replay()
    root = next(iter(nodes))
    create_node(state, "Electric motors are efficient", root, "pro")

    state.expander.children_to_return = [
        GeneratedChild(text="Cookies taste good with milk", stance="pro", label="weak"),
    ]
    client.post(f"/node/{root}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    new_pro = next(n for n in nodes.values() if n.text == "Cookies taste good with milk")
    assert new_pro.potential_dupes == []


def test_potential_dupes_render_as_superscript_links(client, state):
    """The pros/cons list shows ¹²³ superscript links pointing at each candidate."""
    from app import create_node, create_root

    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    create_root(state, "First root")
    nodes = state.replay()
    root_a = next(iter(nodes))
    create_root(state, "Second root")
    nodes = state.replay()
    root_b = next(rid for rid in nodes if rid != root_a)
    shared_text = "shared argument across both roots"
    existing_pro = create_node(state, shared_text, root_a, "pro")

    state.expander.children_to_return = [
        GeneratedChild(text=shared_text, stance="pro", label="strong"),
    ]
    client.post(f"/node/{root_b}/expand", data={"n_pros": "1", "n_cons": "0"})

    body = client.get(f"/node/{root_b}").data.decode()
    # The new node still appears under root_b (not aliased away)
    nodes = state.replay()
    new_pro = next(n for n in nodes.values() if n.parent_id == root_b and n.stance == "pro")
    assert f'href="/node/{new_pro.id}"' in body
    # And there's a superscript dupe-link pointing at the existing one
    assert "dupe-links" in body
    assert "dupe-link" in body
    assert f'href="/node/{existing_pro}"' in body


def test_article_shows_potential_dupes_section_when_present(client, state):
    """A node with potential_dupes recorded surfaces them in a dedicated section
    on its own page so the user can review/compare."""
    from app import create_node, create_root

    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    create_root(state, "Root A")
    nodes = state.replay()
    root_a = next(iter(nodes))
    existing_pro = create_node(state, "the canonical claim", root_a, "pro")

    create_root(state, "Root B")
    nodes = state.replay()
    root_b = next(rid for rid in nodes if rid != root_a)
    state.expander.children_to_return = [
        GeneratedChild(text="the canonical claim", stance="pro", label="strong"),
    ]
    client.post(f"/node/{root_b}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    new_pro = next(n for n in nodes.values() if n.parent_id == root_b and n.stance == "pro")

    body = client.get(f"/node/{new_pro.id}").data.decode()
    assert "Potential duplicates" in body
    # The existing canonical claim is listed in the section
    assert f'href="/node/{existing_pro}"' in body


def test_expand_404_for_missing_node(client):
    r = client.post("/node/nonesuch/expand", data={"n_pros": "1", "n_cons": "1"})
    assert r.status_code == 404


def test_expand_redirects_with_expanding_query_param(client, state):
    """/expand returns a 302 with ?expanding=N where N = current_live_count + n_pros + n_cons."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    r = client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "3"})
    assert r.status_code == 302
    assert "expanding=5" in r.headers["Location"]


def test_expand_redirect_preserves_sort_when_provided(client, state):
    """If the form carries sort=newest, the redirect URL keeps it alongside expanding=N."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    r = client.post(
        f"/node/{parent_id}/expand",
        data={"n_pros": "1", "n_cons": "1", "sort": "newest"},
    )
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert "sort=newest" in loc
    assert "expanding=" in loc


def test_expand_redirect_drops_unknown_sort(client, state):
    """A garbage sort value should silently be dropped from the redirect."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    r = client.post(
        f"/node/{parent_id}/expand",
        data={"n_pros": "1", "n_cons": "1", "sort": "garbage"},
    )
    assert r.status_code == 302
    assert "sort=" not in r.headers["Location"]


def test_expand_form_includes_current_sort_as_hidden(client, state):
    """The expand form rendered when viewing with ?sort=newest carries the sort
    forward as a hidden field so the redirect can preserve it."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    body = client.get(f"/node/{parent_id}?sort=newest").data.decode()
    assert '<input type="hidden" name="sort" value="newest">' in body


def test_expanding_banner_renders_while_count_below_target(client, state):
    """When the URL says expanding=10 but only 4 children exist, banner + reload script appear."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "2"})
    # Now there are 4 children; ask the view for ?expanding=10 (more than current)
    body = client.get(f"/node/{parent_id}?expanding=10").data.decode()
    assert "expanding-banner" in body
    assert "generating arguments" in body
    assert "4 / 10" in body
    assert "location.reload" in body  # the auto-refresh script


def test_no_banner_once_target_reached(client, state):
    """Once live children count >= expanding total, no visible banner, no polling."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "2"})
    # 4 children present; ask for ?expanding=4 — already done
    body = client.get(f"/node/{parent_id}?expanding=4").data.decode()
    # The VISIBLE banner has the parenthesized count "generating arguments… (X /"
    # The HIDDEN placeholder has "generating arguments…" without the count parens
    assert "generating arguments… (" not in body
    # No 2-second polling reload script
    assert ", 2000);" not in body
    # The hidden placeholder IS still rendered in the DOM (for instant visual feedback
    # on the next expand click) — this is fine.


def test_no_banner_without_expanding_param(client, state):
    """Plain view URL: no visible banner, no polling. The hidden JS-target
    placeholder is allowed (used by base.html's expand-form handler)."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    body = client.get(f"/node/{parent_id}").data.decode()
    # No visible banner with count
    assert "generating arguments… (" not in body
    # No 2-second polling reload (the JS handler in base.html uses 1500, fine to ignore)
    assert ", 2000);" not in body
    # Hidden placeholder banner is pre-rendered for the JS handler to unhide.
    # The `hidden` attribute must be on the same element as the class so CSS
    # `.expanding-banner[hidden]` can hide it (otherwise `display: flex` wins).
    assert 'id="expand-pending-banner"' in body
    assert 'class="expanding-banner" id="expand-pending-banner" hidden' in body


def test_expand_form_has_class_for_js_intercept(client, state):
    """The expand button form needs `expand-form` class so the JS handler in
    base.html can intercept its submit and avoid the immediate page refresh."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    body = client.get(f"/node/{parent_id}").data.decode()
    assert 'expand-form' in body


def test_multiple_expand_clicks_chain_more_children(client, state):
    """Clicking expand twice doubles the children. Second click adds another batch."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "2"})
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "2", "n_cons": "2"})
    nodes = state.replay()
    live = [c for c in nodes[parent_id].children
            if not nodes[c].deleted and nodes[c].merged_into is None]
    assert len(live) == 8


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
    parent_text, child_text, stance, _ancestors = state.expander.score_calls[0]
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
    assert [c[0] for c in state.expander.score_claim_calls] == ["vaccines are safe"]


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
    assert [c[0] for c in state.expander.score_claim_calls] == ["X is false"]


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


# ---------- context_chain helper + ancestor propagation ----------

def test_context_chain_empty_for_root(state):
    from app import context_chain, create_root
    state.expander.score_claim_to_return = "moderate"
    create_root(state, "the root")
    nodes = state.replay()
    root = next(iter(nodes.values()))
    assert context_chain(root, nodes) == []


def test_context_chain_single_step_when_parent_is_self_contained(state):
    """If the immediate parent is self-contained, chain stops there."""
    from app import context_chain, create_node, create_root
    create_root(state, "root claim")
    nodes = state.replay()
    root_id = next(iter(nodes))
    nodes[root_id].containment = "self-contained"  # synthetic — but parent is root anyway

    child_id = create_node(state, "child claim", root_id, "pro")
    state.event_log.append({"t": "node_scored", "id": child_id,
                             "containment": "references-parent"})
    nodes = state.replay()
    chain = context_chain(nodes[child_id], nodes)
    # only the root in the chain
    assert [n.id for n in chain] == [root_id]


def test_context_chain_walks_up_through_references_parent(state):
    """Walks up through ancestors that reference their parent, stops at self-contained."""
    from app import context_chain, create_node, create_root
    create_root(state, "root claim")
    nodes = state.replay()
    root_id = next(iter(nodes))

    # Build a chain: root -> a (refs-parent) -> b (refs-parent) -> c (refs-parent)
    a_id = create_node(state, "a", root_id, "pro")
    state.event_log.append({"t": "node_scored", "id": a_id, "containment": "references-parent"})
    b_id = create_node(state, "b", a_id, "pro")
    state.event_log.append({"t": "node_scored", "id": b_id, "containment": "references-parent"})
    c_id = create_node(state, "c", b_id, "pro")
    state.event_log.append({"t": "node_scored", "id": c_id, "containment": "references-parent"})
    nodes = state.replay()

    chain = context_chain(nodes[c_id], nodes)
    # closest-first: b, a, root (walks up since each is references-parent)
    assert [n.id for n in chain] == [b_id, a_id, root_id]


def test_context_chain_stops_at_first_self_contained_ancestor(state):
    """Chain terminates as soon as we find a self-contained ancestor."""
    from app import context_chain, create_node, create_root
    create_root(state, "root")
    nodes = state.replay()
    root_id = next(iter(nodes))

    # root -> a (self-contained) -> b (refs-parent) -> c (refs-parent)
    a_id = create_node(state, "a", root_id, "pro")
    state.event_log.append({"t": "node_scored", "id": a_id, "containment": "self-contained"})
    b_id = create_node(state, "b", a_id, "pro")
    state.event_log.append({"t": "node_scored", "id": b_id, "containment": "references-parent"})
    c_id = create_node(state, "c", b_id, "pro")
    state.event_log.append({"t": "node_scored", "id": c_id, "containment": "references-parent"})
    nodes = state.replay()

    chain = context_chain(nodes[c_id], nodes)
    # b's parent a is self-contained, so chain stops at a (root not included)
    assert [n.id for n in chain] == [b_id, a_id]


def test_ancestors_payload_returns_text_stance_tuples(state):
    from app import ancestors_payload, create_node, create_root
    create_root(state, "root text")
    nodes = state.replay()
    root_id = next(iter(nodes))
    a_id = create_node(state, "a text", root_id, "pro")
    state.event_log.append({"t": "node_scored", "id": a_id, "containment": "references-parent"})
    b_id = create_node(state, "b text", a_id, "con")
    nodes = state.replay()

    payload = ancestors_payload(nodes[b_id], nodes)
    assert payload == [("a text", "pro"), ("root text", "root")]


def test_score_argument_receives_ancestors_when_parent_references_grandparent(client, state):
    """add_child should pass the parent's chain to score_argument when the parent
    isn't self-contained."""
    from app import create_node, create_root
    create_root(state, "ROOT")
    nodes = state.replay()
    root_id = next(iter(nodes))
    a_id = create_node(state, "A (mid)", root_id, "pro")
    state.event_log.append({"t": "node_scored", "id": a_id, "containment": "references-parent"})

    state.expander.score_calls.clear()
    client.post(f"/node/{a_id}/add_child", data={"text": "child of A", "stance": "pro"})

    # score_argument was called with ancestors = chain of A
    assert state.expander.score_calls
    parent_text, child_text, stance, ancestors = state.expander.score_calls[0]
    assert parent_text == "A (mid)"
    assert child_text == "child of A"
    # Chain should include the root
    assert ("ROOT", "root") in ancestors


def test_expand_receives_ancestors_when_expanding_non_root(client, state):
    """expand_node should pass the parent's chain to Expander.expand."""
    from app import create_node, create_root
    create_root(state, "ROOT")
    nodes = state.replay()
    root_id = next(iter(nodes))
    a_id = create_node(state, "A", root_id, "pro")
    state.event_log.append({"t": "node_scored", "id": a_id, "containment": "references-parent"})

    state.expander.calls.clear()
    client.post(f"/node/{a_id}/expand", data={"n_pros": "1", "n_cons": "0"})

    assert state.expander.calls
    _, _, _, _, _, ancestors = state.expander.calls[0]
    assert ("ROOT", "root") in ancestors


def test_score_claim_receives_ancestors_on_first_view_of_nonroot(client, state):
    """ensure_standalone_score should pass the chain to score_claim."""
    from app import create_node, create_root
    create_root(state, "ROOT")
    nodes = state.replay()
    root_id = next(iter(nodes))
    a_id = create_node(state, "A", root_id, "pro")
    state.event_log.append({"t": "node_scored", "id": a_id, "containment": "references-parent"})
    # b is a child of a, which references its parent — viewing b should give it
    # the full chain (a + ROOT) when scoring standalone.
    b_id = create_node(state, "B", a_id, "pro")
    state.event_log.append({"t": "node_scored", "id": b_id, "containment": "references-parent"})

    state.expander.score_claim_calls.clear()
    client.get(f"/node/{b_id}")

    assert state.expander.score_claim_calls
    claim_text, ancestors = state.expander.score_claim_calls[0]
    assert claim_text == "B"
    # chain walks up: A first, then ROOT
    assert ancestors == [("A", "pro"), ("ROOT", "root")]


def test_explain_argument_receives_ancestors(client, state):
    from app import create_node, create_root
    create_root(state, "ROOT")
    nodes = state.replay()
    root_id = next(iter(nodes))
    a_id = create_node(state, "A", root_id, "pro")
    state.event_log.append({"t": "node_scored", "id": a_id, "containment": "references-parent"})
    b_id = create_node(state, "B", a_id, "pro")
    state.event_log.append({"t": "node_scored", "id": b_id, "label": "strong"})

    state.expander.explain_argument_calls.clear()
    client.post(f"/node/{b_id}/explain", data={"axis": "relational"})

    assert state.expander.explain_argument_calls
    _, _, _, _, ancestors = state.expander.explain_argument_calls[0]
    assert ("ROOT", "root") in ancestors


# ---------- dual-axis scoring: standalone vs relational ----------

def test_view_nonroot_first_time_computes_standalone_score(client, state):
    """First visit to a non-root's own page should compute its standalone score
    via score_claim and persist it as standalone_label."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "moderate"  # what FakeExpander returns for standalone
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))

    # before the visit, the child has only a relational label (not standalone)
    assert nodes[child_id].label == "strong"  # FakeExpander expand sets pro=strong
    assert nodes[child_id].standalone_label is None

    # visit the child's own page
    state.expander.score_claim_calls.clear()
    client.get(f"/node/{child_id}")
    nodes = state.replay()

    # standalone score is now populated; relational label is unchanged
    assert nodes[child_id].standalone_label == "moderate"
    assert nodes[child_id].label == "strong"
    # score_claim was called with the child's text
    assert state.expander.score_claim_calls[-1][0] == nodes[child_id].text


def test_view_nonroot_first_time_runs_self_containment_check(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.containment_to_return = ContainmentResult(
        containment="references-parent", reasoning="uses 'it' to refer to the parent"
    )
    client.post("/submit", data={"text": "parent claim"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))

    state.expander.containment_calls.clear()
    client.get(f"/node/{child_id}")
    nodes = state.replay()

    assert nodes[child_id].containment == "references-parent"
    assert nodes[child_id].containment_reasoning == "uses 'it' to refer to the parent"
    assert state.expander.containment_calls
    parent_text, child_text = state.expander.containment_calls[0]
    assert parent_text == "parent claim"
    assert child_text == nodes[child_id].text


def test_view_nonroot_caches_standalone_score_across_visits(client, state):
    """Second visit shouldn't re-fire the LLM calls — the result is cached."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))

    client.get(f"/node/{child_id}")  # first visit: computes
    score_calls_after_first = len(state.expander.score_claim_calls)
    sc_calls_after_first = len(state.expander.containment_calls)

    client.get(f"/node/{child_id}")  # second visit: should NOT recompute
    assert len(state.expander.score_claim_calls) == score_calls_after_first
    assert len(state.expander.containment_calls) == sc_calls_after_first


def test_view_root_does_not_trigger_standalone_score(client, state):
    """Roots already have a standalone score (their `label`) — no extra call needed."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "the root"})
    nodes = state.replay()
    nid = next(iter(nodes))
    score_calls_before = len(state.expander.score_claim_calls)
    sc_calls_before = len(state.expander.containment_calls)
    client.get(f"/node/{nid}")
    # the root's standalone was already computed at submit; viewing shouldn't add calls
    assert len(state.expander.score_claim_calls) == score_calls_before
    assert len(state.expander.containment_calls) == sc_calls_before


def test_nonroot_article_shows_standalone_score_badge(client, state):
    """The article on a non-root's page renders the STANDALONE score, not relational."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "very weak"  # standalone says: very weak (1)
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))

    body = client.get(f"/node/{child_id}").data.decode()
    # FakeExpander.expand gave the child label="strong" (relational, score 4).
    # Standalone is "very weak" (score 1). The ARTICLE must show 1, not 4.
    article_section = body.split("<h1>")[0]  # everything before the title
    assert "score-1" in article_section
    assert "score-4" not in article_section


def test_self_containment_notice_when_not_self_contained(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.containment_to_return = ContainmentResult(
        containment="references-parent", reasoning="needs the parent for context"
    )
    client.post("/submit", data={"text": "the parent claim text"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))

    body = client.get(f"/node/{child_id}").data.decode()
    assert "context-notice" in body
    assert "In context of" in body
    assert "the parent claim text" in body
    # the disclosure with the LLM's reasoning for the self-containment verdict
    assert "needs the parent for context" in body


def test_no_self_containment_notice_when_self_contained(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.containment_to_return = ContainmentResult(
        containment="self-contained", reasoning="claim names its own subject"
    )
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))

    body = client.get(f"/node/{child_id}").data.decode()
    assert "context-notice" not in body


def test_self_contained_pill_visible_when_yes(client, state):
    """Both states get a visible status pill on the article — the positive case too."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.containment_to_return = ContainmentResult(
        containment="self-contained", reasoning="names its subject explicitly"
    )
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))

    body = client.get(f"/node/{child_id}").data.decode()
    assert 'class="sc-tag sc-yes"' in body
    assert ">self-contained<" in body
    # the LLM's reasoning is exposed via the hover tooltip; suffix tells the user about the click
    assert 'names its subject explicitly' in body
    assert 'click to flip' in body


def test_self_contained_pill_visible_when_no(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.containment_to_return = ContainmentResult(
        containment="references-parent", reasoning="uses pronouns"
    )
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))

    body = client.get(f"/node/{child_id}").data.decode()
    assert 'class="sc-tag sc-no"' in body
    assert ">references-parent<" in body
    # AND the fuller context-notice block also renders for the references-parent case
    assert "context-notice" in body


def test_self_contained_pill_absent_on_root(client, state):
    """Roots don't have a parent, so self-containment doesn't apply — no pill."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "the root"})
    nodes = state.replay()
    nid = next(iter(nodes))
    body = client.get(f"/node/{nid}").data.decode()
    assert "sc-tag" not in body


# ---------- user-override containment toggle ----------

def test_containment_pill_renders_as_form_button(client, state):
    """The pill is a submit button posting to /toggle_containment — clicking flips it."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.containment_to_return = ContainmentResult(
        containment="self-contained", reasoning="ok"
    )
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))

    body = client.get(f"/node/{child_id}").data.decode()
    assert f'/node/{child_id}/toggle_containment' in body
    # pill is now a submit button styled as the badge
    assert '<button type="submit" class="sc-tag' in body


def test_toggle_containment_flips_self_contained_to_references_parent(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.containment_to_return = ContainmentResult(
        containment="self-contained", reasoning="LLM says yes"
    )
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))
    # populate containment via first view
    client.get(f"/node/{child_id}")
    nodes = state.replay()
    assert nodes[child_id].containment == "self-contained"

    r = client.post(f"/node/{child_id}/toggle_containment")
    assert r.status_code == 302
    nodes = state.replay()
    assert nodes[child_id].containment == "references-parent"
    # the override carries a marker so future readers can see it was a manual flip
    assert nodes[child_id].containment_reasoning == "manually set by user"


def test_toggle_containment_flips_references_parent_to_self_contained(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.containment_to_return = ContainmentResult(
        containment="references-parent", reasoning="LLM says no"
    )
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))
    client.get(f"/node/{child_id}")
    nodes = state.replay()
    assert nodes[child_id].containment == "references-parent"

    client.post(f"/node/{child_id}/toggle_containment")
    nodes = state.replay()
    assert nodes[child_id].containment == "self-contained"


def test_toggle_containment_redirects_to_referrer(client, state):
    """The flip should bring the user back to where they clicked from."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))
    client.get(f"/node/{child_id}")  # populate containment

    r = client.post(
        f"/node/{child_id}/toggle_containment",
        headers={"Referer": f"http://localhost/node/{child_id}"},
    )
    assert r.status_code == 302
    assert f"/node/{child_id}" in r.headers["Location"]


def test_toggle_containment_is_400_for_root(client, state):
    """Roots have no parent → no containment axis → 400 (or some error)."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "the root"})
    nodes = state.replay()
    nid = next(iter(nodes))
    r = client.post(f"/node/{nid}/toggle_containment")
    assert r.status_code == 400


def test_toggle_containment_404_for_missing(client):
    r = client.post("/node/nonesuch/toggle_containment")
    assert r.status_code == 404


def test_toggle_containment_does_not_re_trigger_llm(client, state):
    """Manual override should NOT call check_containment again."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))
    client.get(f"/node/{child_id}")  # this DOES call check_containment
    state.expander.containment_calls.clear()

    client.post(f"/node/{child_id}/toggle_containment")
    # a manual flip is purely user-driven — no LLM call
    assert state.expander.containment_calls == []


# ---------- /explain with axis param ----------

def test_explain_axis_standalone_persists_standalone_reasoning(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.explain_claim_to_return = "standalone explanation here"
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))
    client.get(f"/node/{child_id}")  # populate standalone_label

    state.expander.explain_claim_calls.clear()
    state.expander.explain_argument_calls.clear()
    r = client.post(f"/node/{child_id}/explain", data={"axis": "standalone"})
    assert r.status_code == 302
    nodes = state.replay()
    assert nodes[child_id].standalone_reasoning == "standalone explanation here"
    # should NOT have written into relational reasoning
    assert nodes[child_id].reasoning is None
    # used explain_claim (standalone), not explain_argument
    assert state.expander.explain_claim_calls
    assert state.expander.explain_argument_calls == []


def test_explain_axis_relational_persists_relational_reasoning(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.explain_argument_to_return = "relational explanation here"
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))

    state.expander.explain_argument_calls.clear()
    r = client.post(f"/node/{child_id}/explain", data={"axis": "relational"})
    assert r.status_code == 302
    nodes = state.replay()
    assert nodes[child_id].reasoning == "relational explanation here"
    # should NOT have written into standalone
    assert nodes[child_id].standalone_reasoning is None
    assert state.expander.explain_argument_calls


def test_pros_cons_list_shows_relational_score_not_standalone(client, state):
    """In the parent's view, child badges show their RELATIONAL score (label),
    not the standalone one even if both have been computed."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "very weak"  # standalone label for any non-root
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    client.post(f"/node/{parent_id}/expand", data={"n_pros": "1", "n_cons": "0"})
    nodes = state.replay()
    child_id = next(iter(nodes[parent_id].children))
    client.get(f"/node/{child_id}")  # populate standalone fields

    # Now view the PARENT — child's badge in the pros list should show relational (4=strong)
    body = client.get(f"/node/{parent_id}").data.decode()
    # The child appears in the parent's pros list with relational label (strong=4)
    pros_section = body.split("<h3>Pros")[1].split("<h3>")[0] if "<h3>Pros" in body else ""
    assert "score-4" in pros_section  # relational
    assert "score-1" not in pros_section  # standalone NOT shown here


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
    # No cached reasoning yet — no reasoning panel
    assert 'class="reasoning-box"' not in body


def test_no_reasoning_renders_hidden_spinner_placeholder(client, state):
    """A hidden &lt;div class="why-spinner-pending"&gt; lives below the claim while reasoning
    is uncached. JS in base.html unhides it on form submit so the user sees a spinner
    while the LLM generates the explanation."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.score_claim_to_return = "moderate"
    client.post("/submit", data={"text": "claim"})
    nodes = state.replay()
    nid = next(iter(nodes))
    body = client.get(f"/node/{nid}").data.decode()
    assert 'class="why-spinner-pending"' in body
    # element starts hidden via the `hidden` attribute (unhidden by JS on submit)
    assert 'class="why-spinner-pending" hidden' in body
    assert "generating reasoning" in body


def test_cached_reasoning_replaces_spinner_placeholder(client, state):
    """Once reasoning is cached, the spinner placeholder is replaced by the real reasoning panel."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.explain_claim_to_return = "the reason"
    client.post("/submit", data={"text": "claim"})
    nodes = state.replay()
    nid = next(iter(nodes))
    client.post(f"/node/{nid}/explain")  # populate reasoning
    body = client.get(f"/node/{nid}").data.decode()
    # No spinner element rendered — reasoning panel is in its place. (The string
    # "why-spinner-pending" still appears in the inline JS that handles forms,
    # so we check for the actual class-attribute substring instead of bare name.)
    assert 'class="why-spinner-pending"' not in body
    assert 'class="reasoning-box"' in body


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
    assert state.expander.explain_claim_calls == [("vaccines work", "strong", [])]
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
    parent_text, child_text, stance, label, _ancestors = state.expander.explain_argument_calls[0]
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
    """Once /explain runs, the badge above becomes a checkbox+label toggle, and the
    reasoning panel renders below the claim. Click the badge to collapse/expand."""
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    state.expander.explain_claim_to_return = "the cached explanation text"
    client.post("/submit", data={"text": "claim"})
    nodes = state.replay()
    nid = next(iter(nodes))
    client.post(f"/node/{nid}/explain")
    body = client.get(f"/node/{nid}").data.decode()
    assert "the cached explanation text" in body
    # Toggle pair: hidden checkbox (default checked, so panel starts visible) + label
    assert 'class="reason-toggle"' in body
    assert 'type="checkbox"' in body
    assert "checked" in body
    assert 'class="label score-' in body  # the visible badge label
    # No form action to /explain (reasoning is cached) and no submit button-as-badge
    assert f'/node/{nid}/explain' not in body
    assert '<button type="submit" class="label' not in body
    # Reasoning panel below the claim is a div, no longer a <details>
    assert 'class="reasoning-box"' in body
    assert 'class="reasoning-title"' in body
    assert 'class="reasoning-text"' in body


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


# ---------- sort options ----------

def test_sort_controls_present_with_score_active_by_default(client, state):
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    body = client.get(f"/node/{parent_id}").data.decode()
    assert 'class="sort-controls"' in body
    # all three options present, with "score" marked active by default
    for opt in ("score", "newest", "oldest"):
        assert f'sort={opt}' in body
    assert '?sort=score" class="active"' in body or "score</a>" in body  # score is the active link


def test_sort_by_newest_orders_recent_first(client, state):
    """?sort=newest → most-recently-created child first."""
    from app import create_node
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    # Create three pros in order; later-created ones have higher created_at
    a = create_node(state, "first pro", parent_id, "pro")
    b = create_node(state, "second pro", parent_id, "pro")
    c = create_node(state, "third pro", parent_id, "pro")
    body = client.get(f"/node/{parent_id}?sort=newest").data.decode()
    # Newest first: third, second, first
    assert -1 < body.find("third pro") < body.find("second pro") < body.find("first pro")


def test_sort_by_oldest_orders_oldest_first(client, state):
    from app import create_node
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    a = create_node(state, "first pro", parent_id, "pro")
    b = create_node(state, "second pro", parent_id, "pro")
    c = create_node(state, "third pro", parent_id, "pro")
    body = client.get(f"/node/{parent_id}?sort=oldest").data.decode()
    assert -1 < body.find("first pro") < body.find("second pro") < body.find("third pro")


def test_sort_by_score_overrides_creation_order(client, state):
    """Score sort: highest-rated first regardless of creation time."""
    from app import create_node
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    weak = create_node(state, "older weak pro", parent_id, "pro")
    state.event_log.append({"t": "node_scored", "id": weak, "label": "weak"})
    strong = create_node(state, "newer strong pro", parent_id, "pro")
    state.event_log.append({"t": "node_scored", "id": strong, "label": "very strong"})
    # default sort = score: very strong appears first despite being newer
    body = client.get(f"/node/{parent_id}").data.decode()
    assert -1 < body.find("newer strong pro") < body.find("older weak pro")


def test_invalid_sort_falls_back_to_score(client, state):
    """A garbage sort value shouldn't error or change the order — falls back to score."""
    from app import create_node
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    weak = create_node(state, "weak pro", parent_id, "pro")
    state.event_log.append({"t": "node_scored", "id": weak, "label": "weak"})
    strong = create_node(state, "strong pro", parent_id, "pro")
    state.event_log.append({"t": "node_scored", "id": strong, "label": "very strong"})
    r = client.get(f"/node/{parent_id}?sort=garbage")
    assert r.status_code == 200
    body = r.data.decode()
    # falls back to score: strong before weak
    assert body.find("strong pro") < body.find("weak pro")


def test_sort_applies_to_cons_too(client, state):
    """Both pros and cons honor the requested sort independently."""
    from app import create_node
    state.classifier.default = ClassifierResult("new", None, 1.0, "")
    client.post("/submit", data={"text": "parent"})
    nodes = state.replay()
    parent_id = next(iter(nodes))
    create_node(state, "old con", parent_id, "con")
    create_node(state, "new con", parent_id, "con")
    body = client.get(f"/node/{parent_id}?sort=newest").data.decode()
    assert body.find("new con") < body.find("old con")


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
