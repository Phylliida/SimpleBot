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
        ancestors: Iterable[tuple[str, str]] = (),
    ) -> list[GeneratedChild]:
        """Return n_pros pro-children and n_cons con-children for `claim_text`.

        `existing_pros` / `existing_cons`, if given, are texts already present in the
        tree under this claim — the implementation should generate arguments that
        are substantively distinct from these (avoiding duplicates on re-expand).

        `ancestors` is the chain of context up to the first self-contained ancestor
        (closest-first, each `(text, stance)`). Passed in only when `claim_text`
        itself references its parent — gives the LLM the broader tree-position so
        it can interpret an ungrounded claim.
        """
        ...

    def score_argument(
        self, parent_text: str, child_text: str, stance: Stance,
        ancestors: Iterable[tuple[str, str]] = (),
    ) -> Label | None:
        """Rate how strongly `child_text` works AS A PRO/CON of `parent_text`.

        `ancestors` provides upstream context if the parent itself isn't self-contained.
        """
        ...

    def score_claim(
        self, claim_text: str, ancestors: Iterable[tuple[str, str]] = (),
    ) -> Label | None:
        """Rate how well-supported a standalone claim is on its own merits.

        `ancestors` is provided when the claim references its parent — gives the LLM
        enough context to interpret the claim before rating its standalone strength.
        """
        ...

    def explain_argument(
        self, parent_text: str, child_text: str, stance: Stance, label: str,
        ancestors: Iterable[tuple[str, str]] = (),
    ) -> str | None:
        ...

    def explain_claim(
        self, claim_text: str, label: str,
        ancestors: Iterable[tuple[str, str]] = (),
    ) -> str | None:
        ...

    def check_containment(
        self, parent_text: str, child_text: str
    ) -> "ContainmentResult | None":
        """Classify whether `child_text` stands on its own without parent context.

        Always single-level (immediate parent only) — the question is specifically
        whether the child references its IMMEDIATE parent, not the whole chain.
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

For each argument, also assign a label rating how strongly it functions AS A PRO OR CON of the parent claim — i.e. how much believing the argument should shift a reasonable person's view of the parent. This is about the pro/con RELATIONSHIP, not the argument's standalone truth. A true statement that's only tangentially related to the parent should still rate "weak" or "very weak" because it doesn't actually move the needle on the parent claim.

Use the GRADE-style scale:
  - "very strong": decisively supports/undermines the parent — a reasonable person would substantially update their view of the parent based on this argument
  - "strong":      meaningfully supports/undermines the parent, with only minor caveats about how much confidence shifts
  - "moderate":    somewhat supports/undermines the parent; relevant but not decisive
  - "weak":        tangentially relevant; provides only limited support/objection to the parent
  - "very weak":   barely connects to the parent or fails to actually move confidence either way

Be honest in your labeling: not every argument deserves "very strong". A good argument tree includes weak and very weak arguments too, because seeing them helps the reader understand the space of objections. Use the full range.

Each pro and con must be a standalone declarative claim — not a question, not hedged ("I think...", "maybe..."), just an assertion. One sentence each. Avoid restating the original claim, and make each argument substantively distinct from the others on its side.
"""


def _format_existing(label: str, items: list[str]) -> str:
    if not items:
        return ""
    lines = "\n".join(f"- {t}" for t in items)
    return f"\nExisting {label} (do NOT duplicate or paraphrase these):\n{lines}\n"


def _format_ancestors(ancestors: Iterable[tuple[str, str]]) -> str:
    """Render an ancestor chain as a short context preamble. Empty if no ancestors.

    `ancestors` is closest-first: the first tuple is the immediate parent (or claim),
    each later tuple is the parent of the previous. Each tuple is `(text, stance)`
    with stance ∈ {"pro", "con", "root"}. The preamble is purely interpretive context
    so the LLM can ground what the relevant claim means; it doesn't shift the rating
    axis (a pro is still rated against its IMMEDIATE parent, not the whole chain).
    """
    items = list(ancestors)
    if not items:
        return ""
    lines = ["Context (where the relevant claim sits in the broader argument tree):"]
    for i, (text, stance) in enumerate(items):
        if i == 0:
            prefix = f"  Parent claim ({stance})"
        elif stance == "root":
            prefix = f"  ... which is the root claim"
        else:
            prefix = f"  ... which is a {stance} of"
        lines.append(f"{prefix}: {text!r}")
    return "\n".join(lines) + "\n\n"


def _build_user_message(
    claim_text: str,
    n_pros: int,
    n_cons: int,
    existing_pros: list[str],
    existing_cons: list[str],
    ancestors: Iterable[tuple[str, str]] = (),
) -> str:
    parts = []
    ancestors_block = _format_ancestors(ancestors)
    if ancestors_block:
        parts.append(ancestors_block.rstrip())
    parts.append(f"Claim to expand: {claim_text!r}")
    parts.append(_format_existing("pros", existing_pros))
    parts.append(_format_existing("cons", existing_cons))
    parts.append(
        f"Generate exactly {n_pros} pros and {n_cons} cons of the claim above. Each must be a self-contained "
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
        ancestors: Iterable[tuple[str, str]] = (),
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
                    claim_text, n_pros, n_cons, existing_pros, existing_cons, ancestors,
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
        self, parent_text: str, child_text: str, stance: Stance,
        ancestors: Iterable[tuple[str, str]] = (),
    ) -> Label | None:
        if stance not in ("pro", "con"):
            raise ValueError(f"stance must be 'pro' or 'con', got {stance!r}")
        if not child_text.strip():
            raise ValueError("child_text is empty")

        if stance == "pro":
            system_prompt = _PRO_SCORE_SYSTEM_PROMPT
            user_message = _build_pro_score_message(parent_text, child_text, ancestors)
        else:
            system_prompt = _CON_SCORE_SYSTEM_PROMPT
            user_message = _build_con_score_message(parent_text, child_text, ancestors)
        return self._score(system_prompt=system_prompt, user_message=user_message)

    def check_containment(
        self, parent_text: str, child_text: str
    ) -> "ContainmentResult | None":
        if not child_text.strip():
            raise ValueError("child_text is empty")
        response = self._client.chat.completions.create(
            model=self._resolve_model(),
            messages=[
                {"role": "system", "content": _CONTAINMENT_SYSTEM_PROMPT},
                {"role": "user", "content": _build_containment_message(parent_text, child_text)},
            ],
            response_format={"type": "json_schema", "json_schema": _CONTAINMENT_SCHEMA},
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
        containment = data.get("containment")
        if containment not in CONTAINMENT_LABELS:
            return None
        reasoning = str(data.get("reasoning", "")).strip()
        return ContainmentResult(containment=containment, reasoning=reasoning)

    def score_claim(
        self, claim_text: str, ancestors: Iterable[tuple[str, str]] = (),
    ) -> Label | None:
        if not claim_text.strip():
            raise ValueError("claim_text is empty")
        return self._score(
            system_prompt=_CLAIM_SCORE_SYSTEM_PROMPT,
            user_message=_build_claim_score_user_message(claim_text, ancestors),
        )

    def explain_argument(
        self, parent_text: str, child_text: str, stance: Stance, label: str,
        ancestors: Iterable[tuple[str, str]] = (),
    ) -> str | None:
        if stance not in ("pro", "con"):
            raise ValueError(f"stance must be 'pro' or 'con', got {stance!r}")
        if not child_text.strip():
            raise ValueError("child_text is empty")
        if label not in LABELS:
            raise ValueError(f"unknown label: {label!r}")
        return self._explain(
            system_prompt=_EXPLAIN_ARG_SYSTEM_PROMPT,
            user_message=_build_explain_arg_message(parent_text, child_text, stance, label, ancestors),
        )

    def explain_claim(
        self, claim_text: str, label: str, ancestors: Iterable[tuple[str, str]] = (),
    ) -> str | None:
        if not claim_text.strip():
            raise ValueError("claim_text is empty")
        if label not in LABELS:
            raise ValueError(f"unknown label: {label!r}")
        return self._explain(
            system_prompt=_EXPLAIN_CLAIM_SYSTEM_PROMPT,
            user_message=_build_explain_claim_message(claim_text, label, ancestors),
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


_PRO_SCORE_SYSTEM_PROMPT = """You rate how strongly a PRO supports its parent claim, using a GRADE-style scale.

The rating is about how much believing the pro should shift a reasonable person TOWARD accepting the parent claim — not the pro's standalone truth. A pro that is true but only tangentially related to the parent should still rate "weak" or "very weak" because it doesn't actually move the needle on the parent.

Labels:
  - "very strong": a powerful pro — would substantially shift a reasonable person toward accepting the parent
  - "strong":      a meaningful pro, with only minor caveats about how much it shifts confidence
  - "moderate":    somewhat supports the parent; relevant but not decisive
  - "weak":        tangentially relevant; provides only limited support for the parent
  - "very weak":   barely connects to the parent; fails to actually shift confidence toward it

Be honest — use the full range. Many pros that look convincing on the surface turn out to be "moderate" or "weak" once you ask whether they actually shift the parent's plausibility.
"""


_CON_SCORE_SYSTEM_PROMPT = """You rate how strongly a CON undermines its parent claim, using a GRADE-style scale.

The rating is about how much believing the con should shift a reasonable person AWAY from accepting the parent claim — not the con's standalone truth. A con that is true but only tangentially related to the parent should still rate "weak" or "very weak" because it doesn't actually move the needle on the parent.

Labels:
  - "very strong": a powerful objection — would substantially shift a reasonable person away from the parent
  - "strong":      a meaningful objection, with only minor caveats about how much it shifts confidence
  - "moderate":    somewhat undermines the parent; relevant but not decisive
  - "weak":        tangentially relevant; provides only limited objection to the parent
  - "very weak":   barely connects to the parent; fails to actually shift confidence away from it

Be honest — use the full range. Many cons that look convincing on the surface turn out to be "moderate" or "weak" once you ask whether they actually undermine the parent's plausibility.
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


def _build_pro_score_message(
    parent_text: str, child_text: str, ancestors: Iterable[tuple[str, str]] = ()
) -> str:
    return (
        _format_ancestors(ancestors)
        + f"Parent claim: {parent_text!r}\n"
        + f"Pro: {child_text!r}\n\n"
        + f"Rate how strongly this pro supports the parent."
    )


def _build_con_score_message(
    parent_text: str, child_text: str, ancestors: Iterable[tuple[str, str]] = ()
) -> str:
    return (
        _format_ancestors(ancestors)
        + f"Parent claim: {parent_text!r}\n"
        + f"Con: {child_text!r}\n\n"
        + f"Rate how strongly this con undermines the parent."
    )


# Self-containment: does the child claim make sense without parent context?
# Two-label enum (matching the project's preference for word labels over booleans).
CONTAINMENT_LABELS: tuple[str, ...] = ("self-contained", "references-parent")
Containment = Literal["self-contained", "references-parent"]


_CONTAINMENT_SCHEMA = {
    "name": "containment_check",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            # reasoning first so CoT runs before the label commits
            "reasoning": {"type": "string"},
            "containment": {"type": "string", "enum": list(CONTAINMENT_LABELS)},
        },
        "required": ["reasoning", "containment"],
        "additionalProperties": False,
    },
}


_CONTAINMENT_SYSTEM_PROMPT = """You assess whether a claim is fully self-contained — i.e. whether it makes complete sense to a reader who has NEVER seen the parent claim it came from.

You're given the parent claim purely as context for the assessment. The question is: if a reader saw ONLY the child claim with no other information, could they understand exactly what is being asserted?

Pick "references-parent" if the child:
  - Uses pronouns ("it", "this", "they", "these") to refer back to the parent's subject
  - Has implicit subjects that only make sense given the parent
  - Is a partial assertion that completes a thought from the parent
  - References the parent's claim by deixis without naming the actual subject

Pick "self-contained" if the child:
  - Names its own concrete subjects explicitly
  - Makes a complete assertion that stands on its own
  - Could be quoted out of context without confusion

First write a brief 1-2 sentence reasoning, then commit to one label.

Examples:
  Parent: "Renewable energy is the future"; Child: "It reduces greenhouse gas emissions" → references-parent ("it" requires parent context)
  Parent: "Renewable energy is the future"; Child: "Solar power is now cheaper than coal" → self-contained (concrete subjects, complete assertion)
  Parent: "X should be banned"; Child: "Banning X violates personal freedom" → self-contained (the subject "X" is named explicitly)
"""


def _build_containment_message(parent_text: str, child_text: str) -> str:
    return (
        f"Parent claim (context only): {parent_text!r}\n"
        f"Child claim to assess: {child_text!r}\n\n"
        f"Pick one: \"self-contained\" or \"references-parent\"."
    )


@dataclass
class ContainmentResult:
    containment: Containment
    reasoning: str


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


_EXPLAIN_ARG_SYSTEM_PROMPT = """You explain why a single argument received a specific rating. The rating is about the argument's strength AS A PRO OR CON of the parent claim — how much it should shift a reasonable person's view of the parent — not the argument's standalone truth. The GRADE-style scale:
  - "very strong": decisively supports/undermines the parent
  - "strong":      meaningfully supports/undermines, with minor caveats
  - "moderate":    somewhat supports/undermines; relevant but not decisive
  - "weak":        tangentially relevant; limited support/objection
  - "very weak":   barely connects, or fails to actually move confidence

Given a parent claim, a candidate argument (pro or con), and the chosen rating label, write a brief 1-2 sentence explanation that addresses both:
  1. What specifically about the argument's RELATIONSHIP to the parent makes it earn this rating, AND
  2. Why this rating and NOT the adjacent ones — e.g. "why 'strong' and not 'very strong'?" (what caveat keeps it from being decisive?), or "why 'weak' and not 'very weak'?" (what kernel of relevance keeps it from being totally disconnected?).

Be concrete — focus on whether/how the argument actually shifts confidence in the parent, not on whether the argument is true in isolation.
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
    parent_text: str, child_text: str, stance: Stance, label: str,
    ancestors: Iterable[tuple[str, str]] = (),
) -> str:
    role = "supports (pro of)" if stance == "pro" else "argues against (con of)"
    return (
        _format_ancestors(ancestors)
        + f"Parent claim: {parent_text!r}\n"
        + f"Argument that {role} the parent: {child_text!r}\n"
        + f"Rating: {label}\n\n"
        + f"Explain why."
    )


def _build_explain_claim_message(
    claim_text: str, label: str, ancestors: Iterable[tuple[str, str]] = (),
) -> str:
    return (
        _format_ancestors(ancestors)
        + f"Claim: {claim_text!r}\n"
        + f"Rating: {label}\n\n"
        + f"Explain why."
    )


def _build_claim_score_user_message(
    claim_text: str, ancestors: Iterable[tuple[str, str]] = ()
) -> str:
    return _format_ancestors(ancestors) + f"Claim: {claim_text!r}\n\nRate this claim."
