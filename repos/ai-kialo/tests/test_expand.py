"""Tests for expand.py.

Pure-logic tests cover the prompt + schema builders. Real-LLM integration tests at
the bottom hit the actual endpoint at 8053; skipped if it's not reachable.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from expand import (
    LABEL_TO_SCORE,
    LABELS,
    _CLAIM_SCORE_SYSTEM_PROMPT,
    _CON_SCORE_SYSTEM_PROMPT,
    _EXPLAIN_ARG_SYSTEM_PROMPT,
    _EXPLAIN_CLAIM_SYSTEM_PROMPT,
    _EXPLAIN_SCHEMA,
    _PRO_SCORE_SYSTEM_PROMPT,
    _SCORE_SCHEMA,
    _CONTAINMENT_SCHEMA,
    _CONTAINMENT_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    CONTAINMENT_LABELS,
    ContainmentResult,
    GeneratedChild,
    LlamaCppExpander,
    _build_claim_score_user_message,
    _build_containment_message,
    _build_con_score_message,
    _build_explain_arg_message,
    _build_explain_claim_message,
    _build_pro_score_message,
    _build_user_message,
    _make_schema,
    _to_child,
    label_score,
)


# ---------- _make_schema ----------

def test_make_schema_pins_pro_array_length():
    s = _make_schema(3, 4)
    pros = s["schema"]["properties"]["pros"]
    assert pros["minItems"] == 3
    assert pros["maxItems"] == 3


def test_make_schema_pins_con_array_length():
    s = _make_schema(3, 4)
    cons = s["schema"]["properties"]["cons"]
    assert cons["minItems"] == 4
    assert cons["maxItems"] == 4


def test_make_schema_strict_and_no_extra_properties():
    s = _make_schema(2, 2)
    assert s["strict"] is True
    assert s["schema"]["additionalProperties"] is False
    assert set(s["schema"]["required"]) == {"pros", "cons"}


def test_make_schema_items_require_text_and_label_only():
    """Per-child schema is label-only — reasoning is on-demand via explain_*."""
    s = _make_schema(2, 2)
    pro_item = s["schema"]["properties"]["pros"]["items"]
    assert pro_item["type"] == "object"
    assert set(pro_item["required"]) == {"text", "label"}
    assert "reasoning" not in pro_item["properties"]
    assert pro_item["properties"]["label"]["enum"] == list(LABELS)


# ---------- _to_child ----------

def test_to_child_happy_path():
    c = _to_child({"text": "  hello  ", "label": "strong"}, "pro")
    assert c.text == "hello"
    assert c.stance == "pro"
    assert c.label == "strong"


def test_to_child_unknown_label_becomes_none():
    """Defensive: if the LLM ever slips past the schema, drop the label rather than crash."""
    c = _to_child({"text": "x", "label": "amazing"}, "con")
    assert c.label is None


def test_to_child_missing_label_becomes_none():
    c = _to_child({"text": "x"}, "pro")
    assert c.label is None


# ---------- label_score ----------

def test_label_score_very_strong_is_max():
    assert label_score("very strong") == 5


def test_label_score_very_weak_is_min():
    assert label_score("very weak") == 1


def test_label_score_strictly_descending():
    """Scores must agree with the LABELS ordering (strongest → weakest)."""
    scores = [label_score(l) for l in LABELS]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == len(LABELS)  # all distinct


def test_label_score_none_is_zero():
    assert label_score(None) == 0


def test_label_score_empty_string_is_zero():
    assert label_score("") == 0


def test_label_score_unknown_is_zero():
    assert label_score("amazing") == 0


def test_label_to_score_has_every_label():
    assert set(LABEL_TO_SCORE) == set(LABELS)


# ---------- score_argument schema + prompt ----------

def test_score_schema_strict_enum_and_required():
    """Score schema is label-only (reasoning lives in the separate explain schema)."""
    s = _SCORE_SCHEMA["schema"]
    assert _SCORE_SCHEMA["strict"] is True
    assert s["additionalProperties"] is False
    assert s["required"] == ["label"]
    assert "reasoning" not in s["properties"]
    assert s["properties"]["label"]["enum"] == list(LABELS)


# ---------- explain schema + prompts ----------

def test_explain_schema_has_only_reasoning():
    s = _EXPLAIN_SCHEMA["schema"]
    assert _EXPLAIN_SCHEMA["strict"] is True
    assert s["additionalProperties"] is False
    assert s["required"] == ["reasoning"]
    assert set(s["properties"]) == {"reasoning"}
    assert s["properties"]["reasoning"]["type"] == "string"


def test_explain_arg_prompt_mentions_label():
    p = _EXPLAIN_ARG_SYSTEM_PROMPT.lower()
    assert "rating" in p or "label" in p


def test_explain_arg_prompt_lists_all_labels():
    """The explain prompt should describe every label in the scale so the model can
    discriminate why the chosen one was picked over the others."""
    for label in LABELS:
        assert label in _EXPLAIN_ARG_SYSTEM_PROMPT, f"missing {label!r} from explain-arg prompt"


def test_explain_arg_prompt_asks_why_this_and_not_adjacent():
    """Prompt should explicitly request a contrast against neighbouring ratings."""
    p = _EXPLAIN_ARG_SYSTEM_PROMPT.lower()
    assert "not the adjacent" in p or "and not" in p or "rather than" in p


def test_explain_claim_prompt_mentions_label():
    p = _EXPLAIN_CLAIM_SYSTEM_PROMPT.lower()
    assert "rating" in p or "label" in p


def test_explain_claim_prompt_lists_all_labels():
    for label in LABELS:
        assert label in _EXPLAIN_CLAIM_SYSTEM_PROMPT, f"missing {label!r} from explain-claim prompt"


def test_explain_claim_prompt_asks_why_this_and_not_adjacent():
    p = _EXPLAIN_CLAIM_SYSTEM_PROMPT.lower()
    assert "not the adjacent" in p or "and not" in p or "rather than" in p


def test_build_explain_arg_message_includes_inputs():
    msg = _build_explain_arg_message("Parent claim text.", "Child claim text.", "pro", "weak")
    assert "Parent claim text." in msg
    assert "Child claim text." in msg
    assert "weak" in msg


def test_build_explain_claim_message_includes_inputs():
    msg = _build_explain_claim_message("the claim", "very strong")
    assert "the claim" in msg
    assert "very strong" in msg


def test_pro_score_system_prompt_lists_all_labels():
    for label in LABELS:
        assert label in _PRO_SCORE_SYSTEM_PROMPT


def test_con_score_system_prompt_lists_all_labels():
    for label in LABELS:
        assert label in _CON_SCORE_SYSTEM_PROMPT


def test_pro_score_prompt_focuses_on_supporting():
    """The pro-only prompt should talk about supporting the parent, not undermining it."""
    p = _PRO_SCORE_SYSTEM_PROMPT.lower()
    assert "support" in p
    assert "toward" in p


def test_con_score_prompt_focuses_on_undermining():
    p = _CON_SCORE_SYSTEM_PROMPT.lower()
    assert "undermin" in p or "against" in p
    assert "away" in p


def test_build_pro_score_message_phrasing():
    msg = _build_pro_score_message("Climate change is a problem.", "Sea levels are rising.")
    assert "Climate change is a problem." in msg
    assert "Sea levels are rising." in msg
    assert "Pro:" in msg
    assert "supports" in msg.lower()


def test_build_con_score_message_phrasing():
    msg = _build_con_score_message("X is good.", "X is harmful.")
    assert "X is good." in msg
    assert "X is harmful." in msg
    assert "Con:" in msg
    assert "undermin" in msg.lower()


# ---------- containment schema + prompt ----------

def test_containment_schema_required_fields():
    s = _CONTAINMENT_SCHEMA["schema"]
    assert _CONTAINMENT_SCHEMA["strict"] is True
    assert s["additionalProperties"] is False
    assert set(s["required"]) == {"reasoning", "containment"}
    assert s["properties"]["containment"]["type"] == "string"
    assert s["properties"]["containment"]["enum"] == list(CONTAINMENT_LABELS)


def test_containment_schema_field_order_reasoning_first():
    """CoT-first ordering: reasoning emits before the label commits."""
    keys = list(_CONTAINMENT_SCHEMA["schema"]["properties"].keys())
    assert keys.index("reasoning") < keys.index("containment")


def test_containment_labels_are_two():
    assert set(CONTAINMENT_LABELS) == {"self-contained", "references-parent"}


def test_containment_prompt_lists_both_labels():
    p = _CONTAINMENT_SYSTEM_PROMPT
    assert "self-contained" in p
    assert "references-parent" in p


def test_containment_prompt_has_examples():
    """Prompt should include concrete examples to anchor the rule."""
    p = _CONTAINMENT_SYSTEM_PROMPT.lower()
    assert "example" in p
    # the canonical bad case (a pronoun referring to the parent)
    assert "it" in p


def test_build_containment_message_includes_both_claims():
    msg = _build_containment_message("Renewable energy is the future.", "It reduces emissions.")
    assert "Renewable energy is the future." in msg
    assert "It reduces emissions." in msg
    assert "self-contained" in msg or "references-parent" in msg


# ---------- LlamaCppExpander.score_argument: argument validation ----------

def test_score_argument_invalid_stance_raises():
    e = LlamaCppExpander.__new__(LlamaCppExpander)
    e._client = None
    e._model = None
    e.max_tokens = 1024
    e.enable_thinking = False
    with pytest.raises(ValueError):
        e.score_argument("parent", "child", "neutral")  # type: ignore[arg-type]


def test_score_argument_empty_child_text_raises():
    e = LlamaCppExpander.__new__(LlamaCppExpander)
    e._client = None
    e._model = None
    e.max_tokens = 1024
    e.enable_thinking = False
    with pytest.raises(ValueError):
        e.score_argument("parent", "   ", "pro")


# ---------- explain_argument / explain_claim: argument validation ----------

def _make_stub_expander():
    e = LlamaCppExpander.__new__(LlamaCppExpander)
    e._client = None
    e._model = None
    e.max_tokens = 1024
    e.enable_thinking = False
    return e


def test_explain_argument_empty_text_raises():
    with pytest.raises(ValueError):
        _make_stub_expander().explain_argument("parent", "  ", "pro", "strong")


def test_explain_argument_invalid_stance_raises():
    with pytest.raises(ValueError):
        _make_stub_expander().explain_argument("parent", "child", "neutral", "strong")  # type: ignore[arg-type]


def test_explain_argument_unknown_label_raises():
    with pytest.raises(ValueError):
        _make_stub_expander().explain_argument("parent", "child", "pro", "compelling")


def test_explain_claim_empty_text_raises():
    with pytest.raises(ValueError):
        _make_stub_expander().explain_claim("  ", "strong")


def test_explain_claim_unknown_label_raises():
    with pytest.raises(ValueError):
        _make_stub_expander().explain_claim("the claim", "compelling")


# ---------- score_claim schema + prompt ----------

def test_claim_score_system_prompt_lists_all_labels():
    for label in LABELS:
        assert label in _CLAIM_SCORE_SYSTEM_PROMPT


def test_claim_score_prompt_emphasizes_standalone():
    """The claim-scoring prompt should NOT frame the rating as a pro/con of something."""
    p = _CLAIM_SCORE_SYSTEM_PROMPT.lower()
    assert "standalone" in p or "on its own" in p


def test_build_claim_score_user_message_contains_claim():
    msg = _build_claim_score_user_message("The earth is round.")
    assert "The earth is round." in msg


def test_score_claim_empty_text_raises():
    e = LlamaCppExpander.__new__(LlamaCppExpander)
    e._client = None
    e._model = None
    e.max_tokens = 1024
    e.enable_thinking = False
    with pytest.raises(ValueError):
        e.score_claim("   ")


def test_make_schema_zero_items_allowed():
    s = _make_schema(0, 0)
    assert s["schema"]["properties"]["pros"]["maxItems"] == 0
    assert s["schema"]["properties"]["cons"]["maxItems"] == 0


# ---------- _build_user_message ----------

def test_build_user_message_includes_claim():
    msg = _build_user_message("Vaccines are safe.", 2, 2, [], [])
    assert "Vaccines are safe." in msg


def test_build_user_message_mentions_counts():
    msg = _build_user_message("claim", 3, 5, [], [])
    assert "3 pros" in msg
    assert "5 cons" in msg


def test_build_user_message_omits_existing_blocks_when_empty():
    msg = _build_user_message("claim", 2, 2, [], [])
    assert "Existing pros" not in msg
    assert "Existing cons" not in msg


def test_build_user_message_includes_existing_pros():
    msg = _build_user_message("claim", 2, 2, ["it saves money", "it improves health"], [])
    assert "Existing pros" in msg
    assert "do NOT duplicate" in msg
    assert "it saves money" in msg
    assert "it improves health" in msg


def test_build_user_message_includes_existing_cons():
    msg = _build_user_message("claim", 2, 2, [], ["it costs too much"])
    assert "Existing cons" in msg
    assert "it costs too much" in msg


def test_build_user_message_includes_both():
    msg = _build_user_message("claim", 1, 1, ["pro1"], ["con1"])
    assert "Existing pros" in msg and "pro1" in msg
    assert "Existing cons" in msg and "con1" in msg


# ---------- self-containment instructions ----------

def test_system_prompt_demands_self_contained_arguments():
    assert "self-contained" in _SYSTEM_PROMPT.lower() or "self contained" in _SYSTEM_PROMPT.lower()


def test_system_prompt_warns_against_pronouns():
    """The prompt should explicitly call out pronouns that refer to the parent claim."""
    p = _SYSTEM_PROMPT.lower()
    assert "pronoun" in p
    # at least a couple of the named pronouns
    pronouns_mentioned = sum(p.count(f'"{w}"') for w in ["it", "this", "that", "these", "they"])
    assert pronouns_mentioned >= 2


def test_system_prompt_includes_a_concrete_example():
    """A few-shot BAD/GOOD example dramatically helps the model with this kind of rule."""
    p = _SYSTEM_PROMPT
    assert "BAD" in p and "GOOD" in p


def test_system_prompt_describes_each_label():
    """All five labels should appear with semantics in the prompt."""
    for label in LABELS:
        assert label in _SYSTEM_PROMPT, f"label {label!r} not described in system prompt"


def test_system_prompt_encourages_label_range():
    """Prompt should nudge the model away from rating everything 'very strong'."""
    p = _SYSTEM_PROMPT.lower()
    assert "be honest" in p or "full range" in p


def test_user_message_reiterates_self_containment():
    msg = _build_user_message("Renewable energy is the future.", 2, 2, [], [])
    assert "self-contained" in msg.lower()
    assert "pronoun" in msg.lower() or "concrete subject" in msg.lower()


# ---------- LlamaCppExpander: argument validation ----------

def test_expand_negative_counts_raises():
    """Validation runs before we try to instantiate the openai client."""
    # We can't easily construct LlamaCppExpander without openai; but the validation
    # logic is in expand() and runs after construction. So construct one and call.
    e = LlamaCppExpander.__new__(LlamaCppExpander)  # bypass __init__
    e._client = None
    e._model = None
    e.max_tokens = 1024
    e.enable_thinking = False
    with pytest.raises(ValueError):
        e.expand("x", n_pros=-1, n_cons=2)
    with pytest.raises(ValueError):
        e.expand("x", n_pros=2, n_cons=-1)


def test_expand_both_zero_returns_empty_without_llm_call():
    """If both counts are zero we should short-circuit — no client call."""
    e = LlamaCppExpander.__new__(LlamaCppExpander)
    e._client = None  # would crash if used
    e._model = None
    e.max_tokens = 1024
    e.enable_thinking = False
    assert e.expand("anything", 0, 0) == []


# ---------- Real LLM integration tests ----------

def _endpoint_up(host: str = "127.0.0.1", port: int = 8053, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


_skip_if_no_llm = pytest.mark.skipif(
    not _endpoint_up(),
    reason="local LLM endpoint not reachable at 127.0.0.1:8053",
)


@pytest.fixture(scope="module")
def expander() -> LlamaCppExpander:
    return LlamaCppExpander()


@_skip_if_no_llm
def test_expand_returns_correct_counts(expander: LlamaCppExpander):
    children = expander.expand("Vaccines are safe and effective.", n_pros=2, n_cons=2)
    assert len(children) == 4
    pros = [c for c in children if c.stance == "pro"]
    cons = [c for c in children if c.stance == "con"]
    assert len(pros) == 2
    assert len(cons) == 2


@_skip_if_no_llm
def test_expand_asymmetric_counts(expander: LlamaCppExpander):
    children = expander.expand("Cats are better pets than dogs.", n_pros=1, n_cons=3)
    pros = [c for c in children if c.stance == "pro"]
    cons = [c for c in children if c.stance == "con"]
    assert len(pros) == 1
    assert len(cons) == 3


@_skip_if_no_llm
def test_children_have_nonempty_text(expander: LlamaCppExpander):
    children = expander.expand("Renewable energy is the future.", 2, 2)
    for c in children:
        assert c.text.strip(), f"child has empty text: {c}"
        assert len(c.text) > 5, f"suspiciously short text: {c.text!r}"




@_skip_if_no_llm
def test_pros_distinct_from_cons(expander: LlamaCppExpander):
    """Sanity: a pro and a con shouldn't be the same string."""
    children = expander.expand("Universal basic income should be implemented.", 2, 2)
    pro_texts = {c.text for c in children if c.stance == "pro"}
    con_texts = {c.text for c in children if c.stance == "con"}
    assert pro_texts.isdisjoint(con_texts)


@_skip_if_no_llm
def test_expand_zero_pros_only_cons(expander: LlamaCppExpander):
    children = expander.expand("Self-driving cars are safe.", n_pros=0, n_cons=2)
    assert all(c.stance == "con" for c in children)
    assert len(children) == 2


@_skip_if_no_llm
def test_generated_arguments_are_self_contained(expander: LlamaCppExpander):
    """Generated arguments should not start with bare pronouns like "It " or "This ".

    Heuristic — the prompt asks for concrete subjects, so children that begin with
    a bare pronoun would be the canonical failure mode the user reported.
    """
    children = expander.expand("Renewable energy is the future of power generation.", 3, 3)
    bad_prefixes = ("it ", "this ", "that ", "these ", "they ")
    offenders = [
        c.text for c in children
        if c.text.lower().startswith(bad_prefixes)
    ]
    assert not offenders, (
        f"generated arguments started with bare pronouns: {offenders!r}"
    )


@_skip_if_no_llm
def test_generated_children_have_valid_labels(expander: LlamaCppExpander):
    """Every generated child should carry a label from the allowed set."""
    children = expander.expand("Universal basic income should be implemented.", 2, 2)
    for c in children:
        assert c.label in LABELS, f"unexpected label: {c.label!r}"


@_skip_if_no_llm
def test_labels_use_some_range(expander: LlamaCppExpander):
    """Sanity: across a non-trivial expansion, the model should not rate every argument identically.

    Soft check — we just want at least 2 distinct labels among 6 children, which would fail
    only if the model collapses everything to a single rating.
    """
    children = expander.expand(
        "Self-driving cars should be allowed on public roads.", 3, 3
    )
    labels = {c.label for c in children}
    assert len(labels) >= 2, f"all children share the same label: {labels!r}"


@_skip_if_no_llm
def test_score_argument_returns_valid_label(expander: LlamaCppExpander):
    label = expander.score_argument(
        parent_text="Renewable energy should replace fossil fuels.",
        child_text="Solar panels have dropped 90% in price over the last decade.",
        stance="pro",
    )
    assert label in LABELS


@_skip_if_no_llm
def test_score_argument_compelling_vs_weak(expander: LlamaCppExpander):
    """A clearly strong pro should outscore a clearly weak one for the same parent."""
    parent = "Cities should invest in public transit infrastructure."
    strong = expander.score_argument(
        parent, "Public transit reduces traffic congestion and lowers urban air pollution.", "pro"
    )
    weak = expander.score_argument(
        parent, "Some buses are painted blue.", "pro"
    )
    assert strong in LABELS and weak in LABELS
    assert label_score(strong) > label_score(weak), (
        f"expected strong-arg score > weak-arg score; got strong={strong!r} weak={weak!r}"
    )


@_skip_if_no_llm
def test_explain_argument_returns_nonempty_reasoning(expander: LlamaCppExpander):
    reasoning = expander.explain_argument(
        parent_text="Renewable energy should replace fossil fuels.",
        child_text="Solar panels have dropped 90% in price over the last decade.",
        stance="pro",
        label="strong",
    )
    assert isinstance(reasoning, str)
    assert len(reasoning) > 5


@_skip_if_no_llm
def test_explain_claim_returns_nonempty_reasoning(expander: LlamaCppExpander):
    reasoning = expander.explain_claim(
        "Climate change is occurring and is largely driven by human activity.",
        label="very strong",
    )
    assert isinstance(reasoning, str)
    assert len(reasoning) > 5


@_skip_if_no_llm
def test_score_claim_returns_valid_label(expander: LlamaCppExpander):
    label = expander.score_claim("Climate change is occurring and is largely driven by human activity.")
    assert label in LABELS


@_skip_if_no_llm
def test_score_claim_well_supported_outscores_fringe(expander: LlamaCppExpander):
    """Sanity: a well-supported claim should outscore an obviously fringe one."""
    strong = expander.score_claim(
        "The Earth orbits the Sun in approximately 365.25 days."
    )
    fringe = expander.score_claim(
        "The Earth is flat and has been hidden by a global conspiracy."
    )
    assert strong in LABELS and fringe in LABELS
    assert label_score(strong) > label_score(fringe), (
        f"expected well-supported > fringe; got strong={strong!r} fringe={fringe!r}"
    )


@_skip_if_no_llm
def test_existing_args_steer_diversity(expander: LlamaCppExpander):
    """When prior pros are provided, the new ones should not exactly repeat them.

    We only assert no exact-string duplication — paraphrases are accepted (the
    LLM can't always avoid those without semantic similarity checking, which is
    classify_claim's job).
    """
    existing_pros = [
        "It reduces greenhouse gas emissions.",
        "It creates jobs in new industries.",
    ]
    children = expander.expand(
        "Renewable energy is the future.",
        n_pros=2, n_cons=0,
        existing_pros=existing_pros,
    )
    new_texts = {c.text.strip() for c in children}
    for prior in existing_pros:
        assert prior not in new_texts, f"expander returned exact duplicate: {prior!r}"
