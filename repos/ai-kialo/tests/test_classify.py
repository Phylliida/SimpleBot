"""Tests for classify.py: orchestrator logic exercised with a FakeClassifier.

No LLM calls — all pipeline logic is tested with a fake. Real-LLM integration tests
live in test_llamacpp_classifier.py (skipped if the endpoint is down).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from classify import (
    ClaimClassification,
    ClassifierResult,
    _build_user_message,
    _format_candidates,
    _validate_result,
    classify_claim,
)
from embeddings import sentence_embed
from event_log import EventLog
from node_embeddings import NodeEmbeddings


# ---------- FakeClassifier fixture ----------

@dataclass
class FakeClassifier:
    """Records calls; returns a canned result, or delegates to a custom responder."""
    default: ClassifierResult = field(
        default_factory=lambda: ClassifierResult("new", None, 1.0, "fake-default")
    )
    responder: Callable | None = None
    calls: list = field(default_factory=list)

    def classify(self, text, candidates):
        self.calls.append((text, list(candidates)))
        if self.responder is not None:
            return self.responder(text, candidates)
        return self.default


def _seed(tmp_path, entries: list[tuple[str, str]]):
    """Build (nodes, store, log) with real sentence embeddings for each text."""
    log = EventLog(tmp_path / "events.jsonl")
    store = NodeEmbeddings(tmp_path / "node.bin")
    for nid, text in entries:
        idx = store.append(sentence_embed(text))
        log.append({
            "t": "node_created", "id": nid, "parent": None, "stance": "root",
            "text": text, "embed_idx": idx,
        })
    return log.replay(), store, log


# ---------- _format_candidates ----------

def test_format_candidates_empty_mentions_no_candidates():
    s = _format_candidates([])
    assert "no candidates" in s.lower()


def test_format_candidates_includes_ids_and_texts():
    s = _format_candidates([("n1", "hello world"), ("n2", "goodbye")])
    assert "n1" in s and "n2" in s
    assert "hello world" in s and "goodbye" in s


def test_build_user_message_contains_everything():
    msg = _build_user_message("my new claim", [("n1", "old claim")])
    assert "my new claim" in msg
    assert "n1" in msg
    assert "old claim" in msg
    assert "Classify" in msg


# ---------- _validate_result ----------

def test_validate_result_happy_path_new():
    r = _validate_result(
        {"verdict": "new", "related_to": None, "confidence": 0.9, "reasoning": "ok"}, set()
    )
    assert r.verdict == "new"
    assert r.related_to is None
    assert r.confidence == 0.9


def test_validate_result_duplicate_with_valid_id():
    r = _validate_result(
        {"verdict": "duplicate", "related_to": "n1", "confidence": 0.9, "reasoning": "same"},
        {"n1", "n2"},
    )
    assert r.verdict == "duplicate"
    assert r.related_to == "n1"


def test_validate_result_negation_with_valid_id():
    r = _validate_result(
        {"verdict": "negation", "related_to": "n2", "confidence": 0.8, "reasoning": "opposite"},
        {"n1", "n2"},
    )
    assert r.verdict == "negation"
    assert r.related_to == "n2"


def test_validate_result_new_coerces_related_to_none():
    """Even if LLM supplies related_to with verdict=new, we drop it."""
    r = _validate_result(
        {"verdict": "new", "related_to": "n1", "confidence": 0.9, "reasoning": "ok"}, {"n1"}
    )
    assert r.related_to is None


def test_validate_result_invalid_related_id_coerces_to_new(capsys):
    r = _validate_result(
        {"verdict": "duplicate", "related_to": "ghost", "confidence": 0.9, "reasoning": "ok"},
        {"n1"},
    )
    assert r.verdict == "new"
    assert r.related_to is None
    assert "coercing to verdict=new" in capsys.readouterr().err


def test_validate_result_invalid_verdict_raises():
    with pytest.raises(ValueError):
        _validate_result(
            {"verdict": "maybe", "related_to": None, "confidence": 0.9, "reasoning": "ok"}, set()
        )


def test_validate_result_missing_confidence_defaults():
    r = _validate_result(
        {"verdict": "new", "related_to": None, "reasoning": "ok"}, set()
    )
    assert 0 <= r.confidence <= 1


# ---------- classify_claim orchestrator ----------

def test_classify_claim_empty_graph_short_circuits_without_llm(tmp_path):
    store = NodeEmbeddings(tmp_path / "node.bin")
    fake = FakeClassifier()
    result = classify_claim("hello", {}, store, fake)
    assert isinstance(result, ClaimClassification)
    assert result.verdict == "new"
    assert result.candidates_seen == []
    # empty graph means no candidates, so classifier never runs
    assert fake.calls == []


def test_classify_claim_no_candidates_in_scope_short_circuits(tmp_path):
    nodes, store, _ = _seed(tmp_path, [("n1", "hello world")])
    fake = FakeClassifier()
    # scope to a nonexistent id -> no candidates
    result = classify_claim("hello", nodes, store, fake, scope=["nonexistent"])
    assert result.verdict == "new"
    assert fake.calls == []


def test_classify_claim_passes_new_text_and_candidates_to_classifier(tmp_path):
    nodes, store, _ = _seed(tmp_path, [
        ("cats", "cats are wonderful animals"),
        ("dogs", "dogs are loyal animals"),
        ("market", "the stock market crashed this morning"),
    ])
    fake = FakeClassifier()
    result = classify_claim("puppies are cute pets", nodes, store, fake, k=2)
    assert 1 <= len(result.candidates_seen) <= 2
    assert len(fake.calls) == 1
    call_text, call_candidates = fake.calls[0]
    assert call_text == "puppies are cute pets"
    assert len(call_candidates) == len(result.candidates_seen)
    # classifier receives (id, text) — text must come from the node, not the query
    for cid, ctext in call_candidates:
        assert ctext == nodes[cid].text


def test_classify_claim_returns_classifier_verdict(tmp_path):
    nodes, store, _ = _seed(tmp_path, [("n1", "hello")])
    fake = FakeClassifier(
        default=ClassifierResult("duplicate", "n1", 0.95, "same meaning")
    )
    result = classify_claim("hello", nodes, store, fake)
    assert result.verdict == "duplicate"
    assert result.related_to == "n1"
    assert result.confidence == 0.95
    assert result.reasoning == "same meaning"


def test_classify_claim_custom_responder(tmp_path):
    nodes, store, _ = _seed(tmp_path, [("n1", "x"), ("n2", "y")])

    def responder(text, candidates):
        if "negate" in text and candidates:
            return ClassifierResult("negation", candidates[0][0], 0.8, "opposite")
        return ClassifierResult("new", None, 0.9, "novel")

    fake = FakeClassifier(responder=responder)
    r1 = classify_claim("hello", nodes, store, fake)
    r2 = classify_claim("negate this", nodes, store, fake)
    assert r1.verdict == "new"
    assert r2.verdict == "negation"
    assert r2.related_to in {"n1", "n2"}


def test_classify_claim_scope_restricts_candidates(tmp_path):
    nodes, store, _ = _seed(tmp_path, [("a", "alpha"), ("b", "beta"), ("c", "gamma")])
    fake = FakeClassifier()
    classify_claim("test", nodes, store, fake, k=5, scope=["a", "b"])
    _, call_candidates = fake.calls[0]
    ids = {cid for cid, _ in call_candidates}
    assert ids.issubset({"a", "b"})


def test_classify_claim_threshold_drops_candidates(tmp_path):
    nodes, store, _ = _seed(tmp_path, [
        ("exact",     "my specific unique input about quantum entanglement"),
        ("unrelated", "completely different topic about finance markets"),
    ])
    fake = FakeClassifier()
    # threshold very high — only the exact-same-ish candidate passes
    classify_claim(
        "my specific unique input about quantum entanglement",
        nodes, store, fake, k=5, threshold=0.95,
    )
    _, call_candidates = fake.calls[0]
    ids = {cid for cid, _ in call_candidates}
    assert "unrelated" not in ids


def test_classify_claim_filters_deleted_and_merged(tmp_path):
    nodes, store, log = _seed(tmp_path, [
        ("keep", "x"), ("gone", "y"), ("mergedaway", "z"),
    ])
    log.append({"t": "node_deleted", "id": "gone"})
    log.append({"t": "node_merged", "id": "mergedaway", "into": "keep"})
    nodes = log.replay()
    fake = FakeClassifier()
    classify_claim("test", nodes, store, fake)
    _, call_candidates = fake.calls[0]
    ids = {cid for cid, _ in call_candidates}
    assert "gone" not in ids
    assert "mergedaway" not in ids
    assert "keep" in ids


def test_classify_claim_candidates_seen_includes_cosine_scores(tmp_path):
    nodes, store, _ = _seed(tmp_path, [("n1", "hello")])
    fake = FakeClassifier()
    result = classify_claim("hello world", nodes, store, fake)
    assert len(result.candidates_seen) == 1
    cid, cos = result.candidates_seen[0]
    assert cid == "n1"
    assert isinstance(cos, float)
    assert 0 <= cos <= 1


def test_classify_claim_k_limits_candidates(tmp_path):
    """k=2 must send at most 2 candidates to the classifier even if more are live."""
    nodes, store, _ = _seed(tmp_path, [
        ("a", "one"), ("b", "two"), ("c", "three"), ("d", "four"), ("e", "five"),
    ])
    fake = FakeClassifier()
    result = classify_claim("hello", nodes, store, fake, k=2)
    _, call_candidates = fake.calls[0]
    assert len(call_candidates) == 2
    assert len(result.candidates_seen) == 2


def test_classify_claim_preserves_candidate_order(tmp_path):
    """candidates_seen is sorted descending by cosine; classifier input must match that order."""
    nodes, store, _ = _seed(tmp_path, [
        ("a", "alpha animal"), ("b", "beta banana"), ("c", "gamma galaxy"),
    ])
    fake = FakeClassifier()
    result = classify_claim("animal world", nodes, store, fake, k=3)
    _, call_candidates = fake.calls[0]
    seen_ids = [cid for cid, _ in result.candidates_seen]
    sent_ids = [cid for cid, _ in call_candidates]
    assert seen_ids == sent_ids
    cosines = [c for _, c in result.candidates_seen]
    assert cosines == sorted(cosines, reverse=True)
