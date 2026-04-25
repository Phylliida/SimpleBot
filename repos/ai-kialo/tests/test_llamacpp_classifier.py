"""Integration tests against the real llama.cpp endpoint at 127.0.0.1:8055.

Skipped if the endpoint is unreachable. These are slow (several seconds per test on
Gemma-27B @ Q4) but they prove the real path works end-to-end: model resolution,
OpenAI-compat transport, json_schema response format, and reasoning + JSON output.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from classify import ClassifierResult, LlamaCppClassifier


def _endpoint_up(host: str = "127.0.0.1", port: int = 8055, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _endpoint_up(),
    reason="local LLM endpoint not reachable at 127.0.0.1:8055",
)


@pytest.fixture(scope="module")
def classifier() -> LlamaCppClassifier:
    return LlamaCppClassifier()


def test_resolves_model_from_endpoint(classifier: LlamaCppClassifier):
    name = classifier._resolve_model()
    assert isinstance(name, str) and name


def test_empty_candidates_short_circuits(classifier: LlamaCppClassifier):
    """No candidates → no LLM call → trivial 'new' verdict."""
    result = classifier.classify("anything at all", [])
    assert isinstance(result, ClassifierResult)
    assert result.verdict == "new"
    assert result.related_to is None


def test_classifies_negation(classifier: LlamaCppClassifier):
    result = classifier.classify(
        "Vaccines are safe and effective.",
        [
            ("n1", "Vaccines are not safe."),
            ("n2", "Dogs are mammals."),
        ],
    )
    assert result.verdict == "negation", f"expected negation, got {result}"
    assert result.related_to == "n1"


def test_classifies_new_when_unrelated(classifier: LlamaCppClassifier):
    result = classifier.classify(
        "The sky appears blue because of Rayleigh scattering.",
        [
            ("n1", "Quarterly earnings beat expectations."),
            ("n2", "Cats purr when content."),
        ],
    )
    assert result.verdict == "new", f"expected new, got {result}"
    assert result.related_to is None


def test_classifies_duplicate(classifier: LlamaCppClassifier):
    result = classifier.classify(
        "Vaccination is a safe medical intervention.",
        [
            ("n1", "Vaccines are safe."),
            ("n2", "Interest rates rose this quarter."),
        ],
    )
    # A reasonable classifier can land on duplicate (same claim, rephrased) or new
    # (nuance-based). We just require it doesn't call it a negation and doesn't
    # hallucinate a related_to outside the candidates.
    assert result.verdict in {"duplicate", "new"}, f"unexpected verdict: {result}"
    if result.verdict == "duplicate":
        assert result.related_to == "n1"


def test_returns_confidence_and_reasoning(classifier: LlamaCppClassifier):
    """Any successful call produces a 0-1 confidence and a non-empty reasoning."""
    result = classifier.classify(
        "Water boils at 100 degrees Celsius at sea level.",
        [("n1", "Water freezes at 0 degrees Celsius.")],
    )
    assert 0.0 <= result.confidence <= 1.0
    assert isinstance(result.reasoning, str)
    assert len(result.reasoning) > 0
