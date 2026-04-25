"""Tree expansion: LLM generates pros + cons for a claim.

Symmetric in shape to classify.py: an `Expander` Protocol for testability and
`LlamaCppExpander` as the real OpenAI-compatible implementation pointing at the
local llama.cpp server.

The schema pins `minItems`/`maxItems` per side so the model can't return the
wrong count — important because the orchestrator (Flask layer) writes one
node_created event per generated child.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Literal, Protocol


Stance = Literal["pro", "con"]

# Compellingness scale, from strongest to weakest, GRADE-style — symmetric "very
# X / X / moderate / Y / very Y" so the ordering is unambiguous (vs the earlier
# "compelling/strong" pair which read as synonyms).  Word-based for the LLM call
# (LLMs calibrate categorical judgments more reliably than 0-1 floats); mapped
# to integers for sorting and display in the UI.
LABELS: tuple[str, ...] = ("very strong", "strong", "moderate", "weak", "very weak")
Label = Literal["very strong", "strong", "moderate", "weak", "very weak"]

LABEL_TO_SCORE: dict[str, int] = {
    "very strong": 5,
    "strong":      4,
    "moderate":    3,
    "weak":        2,
    "very weak":   1,
}


def label_score(label: str | None) -> int:
    """Map a compellingness label to its 1-5 score; unlabeled or unknown → 0."""
    if not label:
        return 0
    return LABEL_TO_SCORE.get(label, 0)


@dataclass
class GeneratedChild:
    text: str
    stance: Stance
    label: Label | None = None


class Expander(Protocol):
    def expand(
        self,
        claim_text: str,
        n_pros: int = 2,
        n_cons: int = 2,
        existing_pros: Iterable[str] = (),
        existing_cons: Iterable[str] = (),
    ) -> list[GeneratedChild]:
        """Return n_pros pro-children and n_cons con-children for `claim_text`.

        `existing_pros` / `existing_cons`, if given, are texts already present in the
        tree under this claim — the implementation should generate arguments that
        are substantively distinct from these (avoiding duplicates on re-expand).
        """
        ...

    def score_argument(
        self, parent_text: str, child_text: str, stance: Stance
    ) -> Label | None:
        """Rate how compelling `child_text` is as a `stance` argument for `parent_text`.

        Returns just the label — reasoning is generated on-demand via `explain_argument`.
        """
        ...

    def score_claim(self, claim_text: str) -> Label | None:
        """Rate how well-supported a standalone claim is on its own merits.

        Returns just the label — reasoning is generated on-demand via `explain_claim`.
        """
        ...

    def explain_argument(
        self, parent_text: str, child_text: str, stance: Stance, label: str
    ) -> str | None:
        """Generate a 1-2 sentence explanation for why an argument earned its label.

        Called only when the user clicks "Why?" — keeps initial scoring cheap by not
        emitting reasoning for arguments the user never inspects.
        """
        ...

    def explain_claim(self, claim_text: str, label: str) -> str | None:
        """Generate a 1-2 sentence explanation for why a standalone claim earned its label."""
        ...


_SYSTEM_PROMPT = """You generate arguments for and against a claim, for use in an argument tree.

Given a claim, produce:
- pros: short, distinct claims that, if true, would SUPPORT the original claim.
- cons: short, distinct claims that, if true, would ARGUE AGAINST the original claim.

CRITICAL — each argument must be SELF-CONTAINED: it must make complete sense to a reader who has NOT seen the original claim. Use the full subject and concrete nouns; do NOT use pronouns ("it", "this", "that", "these", "they") to refer back to the original claim or its subject. The reader of an argument should not need to read the parent to understand it.

Example — for the claim "Renewable energy is the future":
  BAD:  "It reduces greenhouse gas emissions."   (what is "it"?)
  GOOD: "Renewable energy reduces greenhouse gas emissions."
  BAD:  "This creates new jobs."                  (what is "this"?)
  GOOD: "Wind and solar industries create new jobs."

If existing pros or cons are listed, your generated arguments must be substantively DIFFERENT from them — different angle, different mechanism, different consideration. Do not restate or paraphrase existing arguments.

For each argument, also assign a label rating how compelling it is (for a pro, how strongly it supports the claim; for a con, how strongly it argues against). Use the GRADE-style scale:
  - "very strong":  rock-solid, hard to dispute, decisive
  - "strong":       well-supported with only minor caveats
  - "moderate":     reasonable but not definitive; meaningful contestation possible
  - "weak":         debatable, easily contested, has notable problems
  - "very weak":    flimsy, fallacious, or based on shaky reasoning

Be honest in your labeling: not every argument deserves "very strong". A good argument tree includes weak and very weak arguments too, because seeing them helps the reader understand the space of objections. Use the full range.

Each pro and con must be a standalone declarative claim — not a question, not hedged ("I think...", "maybe..."), just an assertion. One sentence each. Avoid restating the original claim, and make each argument substantively distinct from the others on its side.
"""


def _format_existing(label: str, items: list[str]) -> str:
    if not items:
        return ""
    lines = "\n".join(f"- {t}" for t in items)
    return f"\nExisting {label} (do NOT duplicate or paraphrase these):\n{lines}\n"


def _build_user_message(
    claim_text: str,
    n_pros: int,
    n_cons: int,
    existing_pros: list[str],
    existing_cons: list[str],
) -> str:
    parts = [f"Claim: {claim_text!r}"]
    parts.append(_format_existing("pros", existing_pros))
    parts.append(_format_existing("cons", existing_cons))
    parts.append(
        f"Generate exactly {n_pros} pros and {n_cons} cons. Each must be a self-contained "
        f"declarative claim with a concrete subject (no pronouns referring to the parent), "
        f"one sentence each, and substantively different from any existing arguments shown above."
    )
    return "\n".join(p for p in parts if p)


_ARG_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "label": {"type": "string", "enum": list(LABELS)},
    },
    "required": ["text", "label"],
    "additionalProperties": False,
}


def _make_schema(n_pros: int, n_cons: int) -> dict:
    return {
        "name": "tree_expansion",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "pros": {
                    "type": "array",
                    "items": _ARG_ITEM_SCHEMA,
                    "minItems": n_pros,
                    "maxItems": n_pros,
                },
                "cons": {
                    "type": "array",
                    "items": _ARG_ITEM_SCHEMA,
                    "minItems": n_cons,
                    "maxItems": n_cons,
                },
            },
            "required": ["pros", "cons"],
            "additionalProperties": False,
        },
    }


class LlamaCppExpander:
    """Expander backed by an OpenAI-compatible endpoint (default: local Qwen at 8053).

    Uses json_schema response_format with pinned array lengths so we never get a
    short list. `enable_thinking=False` by default — Qwen handles this task fine
    without the thinking phase.
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

    def expand(
        self,
        claim_text: str,
        n_pros: int = 2,
        n_cons: int = 2,
        existing_pros: Iterable[str] = (),
        existing_cons: Iterable[str] = (),
    ) -> list[GeneratedChild]:
        if n_pros < 0 or n_cons < 0:
            raise ValueError(f"counts must be non-negative; got n_pros={n_pros}, n_cons={n_cons}")
        if n_pros == 0 and n_cons == 0:
            return []

        existing_pros = list(existing_pros)
        existing_cons = list(existing_cons)

        response = self._client.chat.completions.create(
            model=self._resolve_model(),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(
                    claim_text, n_pros, n_cons, existing_pros, existing_cons
                )},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": _make_schema(n_pros, n_cons),
            },
            max_tokens=self.max_tokens,
            extra_body={"chat_template_kwargs": {"enable_thinking": self.enable_thinking}},
        )
        choice = response.choices[0]
        raw = choice.message.content or ""
        if not raw:
            raise ValueError(
                f"expander returned empty content (finish_reason={choice.finish_reason!r}); "
                f"max_tokens={self.max_tokens} may be too low"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"expander returned non-JSON: {raw!r}") from e

        children: list[GeneratedChild] = []
        for item in data.get("pros", [])[:n_pros]:
            children.append(_to_child(item, "pro"))
        for item in data.get("cons", [])[:n_cons]:
            children.append(_to_child(item, "con"))
        return children

    def score_argument(
        self, parent_text: str, child_text: str, stance: Stance
    ) -> Label | None:
        if stance not in ("pro", "con"):
            raise ValueError(f"stance must be 'pro' or 'con', got {stance!r}")
        if not child_text.strip():
            raise ValueError("child_text is empty")

        return self._score(
            system_prompt=_SCORE_SYSTEM_PROMPT,
            user_message=_build_score_user_message(parent_text, child_text, stance),
        )

    def score_claim(self, claim_text: str) -> Label | None:
        if not claim_text.strip():
            raise ValueError("claim_text is empty")
        return self._score(
            system_prompt=_CLAIM_SCORE_SYSTEM_PROMPT,
            user_message=_build_claim_score_user_message(claim_text),
        )

    def explain_argument(
        self, parent_text: str, child_text: str, stance: Stance, label: str
    ) -> str | None:
        if stance not in ("pro", "con"):
            raise ValueError(f"stance must be 'pro' or 'con', got {stance!r}")
        if not child_text.strip():
            raise ValueError("child_text is empty")
        if label not in LABELS:
            raise ValueError(f"unknown label: {label!r}")
        return self._explain(
            system_prompt=_EXPLAIN_ARG_SYSTEM_PROMPT,
            user_message=_build_explain_arg_message(parent_text, child_text, stance, label),
        )

    def explain_claim(self, claim_text: str, label: str) -> str | None:
        if not claim_text.strip():
            raise ValueError("claim_text is empty")
        if label not in LABELS:
            raise ValueError(f"unknown label: {label!r}")
        return self._explain(
            system_prompt=_EXPLAIN_CLAIM_SYSTEM_PROMPT,
            user_message=_build_explain_claim_message(claim_text, label),
        )

    def _score(self, system_prompt: str, user_message: str) -> Label | None:
        """Shared core for score_argument / score_claim — only differs in prompt."""
        response = self._client.chat.completions.create(
            model=self._resolve_model(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_schema", "json_schema": _SCORE_SCHEMA},
            max_tokens=128,
            extra_body={"chat_template_kwargs": {"enable_thinking": self.enable_thinking}},
        )
        choice = response.choices[0]
        raw = choice.message.content or ""
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        label = data.get("label")
        return label if label in LABELS else None

    def _explain(self, system_prompt: str, user_message: str) -> str | None:
        """Shared core for explain_argument / explain_claim."""
        response = self._client.chat.completions.create(
            model=self._resolve_model(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_schema", "json_schema": _EXPLAIN_SCHEMA},
            max_tokens=512,
            extra_body={"chat_template_kwargs": {"enable_thinking": self.enable_thinking}},
        )
        choice = response.choices[0]
        raw = choice.message.content or ""
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        reasoning = str(data.get("reasoning", "")).strip()
        return reasoning or None


def _to_child(item: dict, stance: Stance) -> GeneratedChild:
    text = str(item.get("text", "")).strip()
    label = item.get("label")
    if label not in LABELS:
        label = None  # tolerate missing/unknown; orchestrator can skip scoring
    return GeneratedChild(text=text, stance=stance, label=label)


_SCORE_SYSTEM_PROMPT = """You rate how compelling a single argument is, using a GRADE-style scale.

Given a parent claim and a candidate argument that either supports (pro) or argues against (con) the parent, assign one of these labels:
  - "very strong":  rock-solid, hard to dispute, decisive
  - "strong":       well-supported with only minor caveats
  - "moderate":     reasonable but not definitive; meaningful contestation possible
  - "weak":         debatable, easily contested, has notable problems
  - "very weak":    flimsy, fallacious, or based on shaky reasoning

Be honest — not every argument deserves "very strong". Use the full range when warranted.
"""


_SCORE_SCHEMA = {
    "name": "argument_score",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": list(LABELS)},
        },
        "required": ["label"],
        "additionalProperties": False,
    },
}


def _build_score_user_message(parent_text: str, child_text: str, stance: Stance) -> str:
    role = "supports (pro of)" if stance == "pro" else "argues against (con of)"
    return (
        f"Parent claim: {parent_text!r}\n\n"
        f"Argument that {role} the parent: {child_text!r}\n\n"
        f"Rate this argument."
    )


_CLAIM_SCORE_SYSTEM_PROMPT = """You rate how well-supported a single claim is, on its own merits, using a GRADE-style scale.

The claim has no context here — judge it as a standalone proposition. Pick one of:
  - "very strong": strongly supported by evidence, widely accepted, hard to dispute
  - "strong":      well-supported with minor caveats
  - "moderate":    reasonable but contested; has both supporting and opposing evidence
  - "weak":        questionable, mostly speculative, or weakly supported
  - "very weak":   contradicted by evidence, fringe, or based on shaky reasoning

Be honest and use the full range. Don't over-rate everything as "very strong" — many real claims are merely "moderate" or "weak" once you scrutinize the evidence.
"""


_EXPLAIN_SCHEMA = {
    "name": "explanation",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
        },
        "required": ["reasoning"],
        "additionalProperties": False,
    },
}


_EXPLAIN_ARG_SYSTEM_PROMPT = """You explain why a single argument received a specific rating from this GRADE-style scale:
  - "very strong":  rock-solid, hard to dispute, decisive
  - "strong":       well-supported with only minor caveats
  - "moderate":     reasonable but not definitive; meaningful contestation possible
  - "weak":         debatable, easily contested, has notable problems
  - "very weak":    flimsy, fallacious, or based on shaky reasoning

Given a parent claim, a candidate argument (pro or con), and the chosen rating label, write a brief 1-2 sentence explanation that addresses both:
  1. What specifically makes the argument earn this rating, AND
  2. Why this rating and NOT the adjacent ones — e.g. "why 'strong' and not 'very strong'?" (what minor caveat keeps it from rock-solid?), or "why 'weak' and not 'very weak'?" (what does it have going for it that keeps it from being flimsy?).

Be concrete — name the specific strength or weakness, don't just restate the label.
"""


_EXPLAIN_CLAIM_SYSTEM_PROMPT = """You explain why a single standalone claim received a specific rating from this GRADE-style scale:
  - "very strong": strongly supported by evidence, widely accepted, hard to dispute
  - "strong":      well-supported with minor caveats
  - "moderate":    reasonable but contested; has both supporting and opposing evidence
  - "weak":        questionable, mostly speculative, or weakly supported
  - "very weak":   contradicted by evidence, fringe, or based on shaky reasoning

Given a claim and the chosen rating label, write a brief 1-2 sentence explanation that addresses both:
  1. What evidence or reasoning supports this rating, AND
  2. Why this rating and NOT the adjacent ones — e.g. "why 'moderate' and not 'strong'?" (what's the contestation?), or "why 'weak' and not 'very weak'?" (what kernel of truth keeps it from being clearly wrong?).

Be concrete — point at what makes the claim earn this label specifically, don't just restate it.
"""


def _build_explain_arg_message(
    parent_text: str, child_text: str, stance: Stance, label: str
) -> str:
    role = "supports (pro of)" if stance == "pro" else "argues against (con of)"
    return (
        f"Parent claim: {parent_text!r}\n"
        f"Argument that {role} the parent: {child_text!r}\n"
        f"Rating: {label}\n\n"
        f"Explain why."
    )


def _build_explain_claim_message(claim_text: str, label: str) -> str:
    return (
        f"Claim: {claim_text!r}\n"
        f"Rating: {label}\n\n"
        f"Explain why."
    )


def _build_claim_score_user_message(claim_text: str) -> str:
    return f"Claim: {claim_text!r}\n\nRate this claim."
