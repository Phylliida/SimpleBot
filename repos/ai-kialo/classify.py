"""Claim classification: new / duplicate / negation against existing nodes.

Two-stage pipeline:
- Cheap recall via embedding similarity (`find_similar_in_nodes`) → top-k candidates.
- Precise verdict via an LLM `Classifier` — a Protocol, so the orchestration is fully
  testable with a FakeClassifier and the real `LlamaCppClassifier` is just one impl.

Entry point: `classify_claim(text, nodes, node_embeddings, classifier)` ties it all
together and returns a `ClaimClassification` with verdict, related_to, confidence,
reasoning, and the candidate list the LLM actually saw.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Iterable, Literal, Protocol

from event_log import Node
from node_embeddings import NodeEmbeddings
from similarity import find_similar_in_nodes


Verdict = Literal["new", "duplicate", "negation"]


@dataclass
class ClassifierResult:
    verdict: Verdict
    related_to: str | None
    confidence: float
    reasoning: str


@dataclass
class ClaimClassification:
    verdict: Verdict
    related_to: str | None
    confidence: float
    reasoning: str
    candidates_seen: list[tuple[str, float]]  # (id, cosine) from find_similar


class Classifier(Protocol):
    def classify(self, text: str, candidates: list[tuple[str, str]]) -> ClassifierResult:
        """Return a 3-way verdict for `text` against `(id, text)` candidate pairs."""
        ...


# ---------- prompt + schema ----------

_SYSTEM_PROMPT = """You classify new claims against existing claims in an argument tree, for deduplication.

Given a new claim and a list of candidate claims that are semantically similar, decide one of three verdicts:
- "new": the new claim is substantively different from all candidates (different topic, OR same topic but a meaningfully distinct position).
- "duplicate": the new claim says essentially the same thing as one candidate, even if worded differently (same stance on same topic).
- "negation": the new claim is the logical opposite of one candidate (e.g. "X is good" vs "X is bad", or "X is true" vs "X is false").

If duplicate or negation, set related_to to that candidate's id. If new, set related_to to null.
Confidence should be your subjective 0.0-1.0 certainty in the verdict.
"""

_JSON_SCHEMA = {
    "name": "claim_classification",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["new", "duplicate", "negation"]},
            "related_to": {"type": ["string", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reasoning": {"type": "string"},
        },
        "required": ["verdict", "related_to", "confidence", "reasoning"],
        "additionalProperties": False,
    },
}


def _format_candidates(candidates: list[tuple[str, str]]) -> str:
    if not candidates:
        return "(no candidates — return verdict=new)"
    return "\n".join(f"- {cid}: {ctext!r}" for cid, ctext in candidates)


def _build_user_message(text: str, candidates: list[tuple[str, str]]) -> str:
    return (
        f"New claim: {text!r}\n\n"
        f"Candidate claims:\n{_format_candidates(candidates)}\n\n"
        f"Classify the new claim."
    )


def _validate_result(data: dict, candidate_ids: set[str]) -> ClassifierResult:
    """Validate + coerce a classifier response dict into a ClassifierResult.

    Defensive against malformed output even though json_schema guarantees shape:
    - unknown verdict → raises
    - verdict=new with related_to set → coerce related_to=None
    - verdict=duplicate|negation with related_to not in candidates → coerce to verdict=new (warns)
    """
    verdict = data.get("verdict")
    if verdict not in ("new", "duplicate", "negation"):
        raise ValueError(f"invalid verdict: {verdict!r}")
    related_to = data.get("related_to")
    confidence = float(data.get("confidence", 0.5))
    reasoning = str(data.get("reasoning", ""))

    if verdict == "new":
        related_to = None
    else:
        if related_to not in candidate_ids:
            print(
                f"warn: classifier returned related_to={related_to!r} not in "
                f"candidates {sorted(candidate_ids)!r}; coercing to verdict=new",
                file=sys.stderr,
            )
            verdict = "new"
            related_to = None

    return ClassifierResult(
        verdict=verdict, related_to=related_to,
        confidence=confidence, reasoning=reasoning,
    )


# ---------- real LLM classifier ----------

class LlamaCppClassifier:
    """Classifier backed by an OpenAI-compatible endpoint (local llama.cpp server by default).

    Uses `response_format={"type": "json_schema", ...}` so the response is grammar-constrained
    to valid JSON matching our schema — no parsing fragility.

    Lazy-resolves the model name from `/v1/models` unless one is passed explicitly, so a
    model-swap on the server doesn't require a code change here.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8053/v1",
        api_key: str = "sk-no-key",
        model: str | None = None,
        max_tokens: int = 1024,
        timeout: float = 120.0,
        enable_thinking: bool = False,
    ):
        # Lazy import keeps pure-logic tests independent of the openai package
        from openai import OpenAI
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._model = model
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking

    def _resolve_model(self) -> str:
        if self._model is None:
            models = self._client.models.list()
            self._model = models.data[0].id
        return self._model

    def classify(self, text: str, candidates: list[tuple[str, str]]) -> ClassifierResult:
        if not candidates:
            return ClassifierResult("new", None, 1.0, "no candidates")

        response = self._client.chat.completions.create(
            model=self._resolve_model(),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(text, candidates)},
            ],
            response_format={"type": "json_schema", "json_schema": _JSON_SCHEMA},
            max_tokens=self.max_tokens,
            extra_body={"chat_template_kwargs": {"enable_thinking": self.enable_thinking}},
        )
        choice = response.choices[0]
        raw = choice.message.content or ""
        if not raw:
            # With reasoning-capable models (e.g. Gemma thinking), content can be empty
            # if the reasoning phase consumed the whole max_tokens budget before the
            # schema-constrained JSON got emitted. Surface that clearly.
            raise ValueError(
                f"classifier returned empty content (finish_reason={choice.finish_reason!r}); "
                f"max_tokens={self.max_tokens} may be too low for this model's reasoning phase"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"classifier returned non-JSON: {raw!r}") from e
        return _validate_result(data, {cid for cid, _ in candidates})


# ---------- orchestrator ----------

def classify_claim(
    text: str,
    nodes: dict[str, Node],
    node_embeddings: NodeEmbeddings,
    classifier: Classifier,
    k: int = 5,
    scope: Iterable[str] | None = None,
    threshold: float | None = None,
) -> ClaimClassification:
    """Classify `text` against existing nodes using embedding recall + LLM precision.

    - `k`: how many top-similar candidates to present to the classifier.
    - `scope`: restrict candidate pool to these node ids (default: all live nodes).
    - `threshold`: drop candidates with cosine below this before the LLM sees them.

    Short-circuits to verdict="new" without calling the classifier when there are
    no candidates in scope.
    """
    candidates = find_similar_in_nodes(
        text, nodes, node_embeddings,
        k=k, threshold=threshold, scope=scope,
    )
    candidate_pairs = [(cid, nodes[cid].text) for cid, _ in candidates]

    if not candidate_pairs:
        return ClaimClassification(
            verdict="new", related_to=None, confidence=1.0,
            reasoning="no candidates in scope", candidates_seen=[],
        )

    result = classifier.classify(text, candidate_pairs)
    return ClaimClassification(
        verdict=result.verdict,
        related_to=result.related_to,
        confidence=result.confidence,
        reasoning=result.reasoning,
        candidates_seen=candidates,
    )
