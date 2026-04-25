"""Append-only JSONL event log + fold-events-into-Node-dict replayer.

The log is the source of truth; the Node dict is derived state rebuilt on load.
Storing only events (not mutations) keeps history queryable and makes dedup
merges/negation links non-destructive.

Event types (v0):
    node_created   {id, parent, stance, text, embed_idx}
    node_edited    {id, text, embed_idx}
    node_deleted   {id}
    node_scored    {id, conv?, uncert?, src?}
    user_marked    {id, mark: "dig_here", value?}
    node_merged    {id, into, reason?}          # reads follow .merged_into to target
    node_negates   {a, b, reason?}              # bidirectional semantic-opposite link
    focus_set      {session, id}                # session UI state; not in node replay
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class Node:
    id: str
    parent_id: str | None
    stance: str            # "root" | "pro" | "con"
    text: str
    embed_idx: int
    created_at: float
    updated_at: float
    conv: float | None = None
    uncert: float | None = None
    label: str | None = None    # word-based compellingness (e.g. "compelling", "weak")
    reasoning: str | None = None  # LLM's explanation for the label, shown on demand
    dig_here: bool = False
    deleted: bool = False
    merged_into: str | None = None
    negates: set[str] = field(default_factory=set)
    children: list[str] = field(default_factory=list)


class EventLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, event: dict) -> dict:
        event.setdefault("ts", time.time())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
        return event

    def iter_events(self) -> Iterator[dict]:
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def replay(self) -> dict[str, Node]:
        nodes: dict[str, Node] = {}
        for ev in self.iter_events():
            _apply(nodes, ev)
        _rebuild_children(nodes)
        return nodes


def _apply(nodes: dict[str, Node], ev: dict) -> None:
    t = ev["t"]
    ts = ev.get("ts", 0.0)

    if t == "node_created":
        nodes[ev["id"]] = Node(
            id=ev["id"],
            parent_id=ev.get("parent"),
            stance=ev.get("stance", "root"),
            text=ev["text"],
            embed_idx=ev["embed_idx"],
            created_at=ts,
            updated_at=ts,
        )
    elif t == "node_edited":
        n = nodes[ev["id"]]
        n.text = ev["text"]
        n.embed_idx = ev["embed_idx"]
        n.updated_at = ts
    elif t == "node_deleted":
        n = nodes[ev["id"]]
        n.deleted = True
        n.updated_at = ts
    elif t == "node_scored":
        n = nodes[ev["id"]]
        if "conv" in ev:
            n.conv = ev["conv"]
        if "uncert" in ev:
            n.uncert = ev["uncert"]
        if "label" in ev:
            n.label = ev["label"]
        if "reasoning" in ev:
            n.reasoning = ev["reasoning"]
        n.updated_at = ts
    elif t == "user_marked":
        n = nodes[ev["id"]]
        if ev.get("mark") == "dig_here":
            n.dig_here = bool(ev.get("value", True))
        n.updated_at = ts
    elif t == "node_merged":
        n = nodes[ev["id"]]
        n.merged_into = ev["into"]
        n.updated_at = ts
    elif t == "node_negates":
        nodes[ev["a"]].negates.add(ev["b"])
        nodes[ev["b"]].negates.add(ev["a"])
    elif t == "focus_set":
        pass  # session-scoped; handled outside the node fold
    else:
        raise ValueError(f"unknown event type: {t!r}")


def _rebuild_children(nodes: dict[str, Node]) -> None:
    for n in nodes.values():
        n.children = []
    for n in nodes.values():
        if n.parent_id is None or n.deleted or n.merged_into is not None:
            continue
        parent = nodes.get(n.parent_id)
        if parent is not None:
            parent.children.append(n.id)


def resolve(nodes: dict[str, Node], nid: str) -> Node | None:
    """Follow merged_into pointers to the live target. Returns None if deleted or cyclic."""
    seen: set[str] = set()
    cur = nid
    while cur and cur not in seen:
        seen.add(cur)
        n = nodes.get(cur)
        if n is None:
            return None
        if n.merged_into is None:
            return None if n.deleted else n
        cur = n.merged_into
    return None
