"""Round-trip tests for the event log: append events, replay, verify derived state."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_log import EventLog, Node, resolve


def _root(log: EventLog, nid: str, text: str, embed_idx: int = 0) -> None:
    log.append({"t": "node_created", "id": nid, "parent": None, "stance": "root",
                "text": text, "embed_idx": embed_idx})


def _child(log: EventLog, nid: str, parent: str, stance: str, text: str, embed_idx: int = 0) -> None:
    log.append({"t": "node_created", "id": nid, "parent": parent, "stance": stance,
                "text": text, "embed_idx": embed_idx})


def test_node_created_basic(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "Vaccines are safe", embed_idx=7)
    nodes = log.replay()
    assert set(nodes) == {"n1"}
    n = nodes["n1"]
    assert n.parent_id is None
    assert n.stance == "root"
    assert n.text == "Vaccines are safe"
    assert n.embed_idx == 7
    assert not n.deleted
    assert n.merged_into is None


def test_children_index_rebuilt(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "root")
    _child(log, "n2", "n1", "pro", "pro1")
    _child(log, "n3", "n1", "con", "con1")
    nodes = log.replay()
    assert set(nodes["n1"].children) == {"n2", "n3"}
    assert nodes["n2"].children == []


def test_edit_updates_text_and_embedding(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "old text", embed_idx=1)
    log.append({"t": "node_edited", "id": "n1", "text": "new text", "embed_idx": 42})
    nodes = log.replay()
    assert nodes["n1"].text == "new text"
    assert nodes["n1"].embed_idx == 42


def test_deleted_node_excluded_from_parent_children(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "root")
    _child(log, "n2", "n1", "pro", "will be deleted")
    _child(log, "n3", "n1", "pro", "stays")
    log.append({"t": "node_deleted", "id": "n2"})
    nodes = log.replay()
    assert nodes["n2"].deleted
    assert nodes["n1"].children == ["n3"]


def test_scoring(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "root")
    log.append({"t": "node_scored", "id": "n1", "conv": 0.7, "uncert": 0.3, "src": "llm"})
    log.append({"t": "node_scored", "id": "n1", "conv": 0.8})  # latest wins
    nodes = log.replay()
    assert nodes["n1"].conv == 0.8
    assert nodes["n1"].uncert == 0.3


def test_scoring_with_label(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "root")
    log.append({"t": "node_scored", "id": "n1", "label": "very strong", "src": "expander"})
    nodes = log.replay()
    assert nodes["n1"].label == "very strong"
    # latest label wins
    log.append({"t": "node_scored", "id": "n1", "label": "weak"})
    nodes = log.replay()
    assert nodes["n1"].label == "weak"


def test_scoring_with_reasoning(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "root")
    log.append({
        "t": "node_scored", "id": "n1",
        "label": "strong", "reasoning": "Well-supported by evidence.",
    })
    nodes = log.replay()
    assert nodes["n1"].label == "strong"
    assert nodes["n1"].reasoning == "Well-supported by evidence."


def test_scoring_reasoning_persists_independently_of_label(tmp_path):
    """A later score with only `label` set should not wipe a previously-set reasoning."""
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "root")
    log.append({
        "t": "node_scored", "id": "n1",
        "label": "strong", "reasoning": "first reasoning",
    })
    log.append({"t": "node_scored", "id": "n1", "label": "weak"})  # no reasoning key
    nodes = log.replay()
    assert nodes["n1"].label == "weak"
    assert nodes["n1"].reasoning == "first reasoning"


def test_scoring_label_independent_of_conv(tmp_path):
    """label and numeric conv coexist on the same node — neither overwrites the other."""
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "root")
    log.append({"t": "node_scored", "id": "n1", "conv": 0.6})
    log.append({"t": "node_scored", "id": "n1", "label": "strong"})
    nodes = log.replay()
    assert nodes["n1"].conv == 0.6
    assert nodes["n1"].label == "strong"


def test_user_mark_dig_here(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "root")
    log.append({"t": "user_marked", "id": "n1", "mark": "dig_here"})
    nodes = log.replay()
    assert nodes["n1"].dig_here is True
    log.append({"t": "user_marked", "id": "n1", "mark": "dig_here", "value": False})
    nodes = log.replay()
    assert nodes["n1"].dig_here is False


def test_merge_and_resolve(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "canonical")
    _root(log, "n2", "duplicate")
    log.append({"t": "node_merged", "id": "n2", "into": "n1", "reason": "cosine=0.95"})
    nodes = log.replay()
    assert nodes["n2"].merged_into == "n1"
    # resolve follows the pointer
    assert resolve(nodes, "n2") is nodes["n1"]
    assert resolve(nodes, "n1") is nodes["n1"]


def test_merged_child_not_in_parent_children(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "root", "r")
    _child(log, "n1", "root", "pro", "keeper")
    _child(log, "n2", "root", "pro", "merged away")
    log.append({"t": "node_merged", "id": "n2", "into": "n1"})
    nodes = log.replay()
    assert nodes["root"].children == ["n1"]


def test_negates_bidirectional(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "a", "X is true")
    _root(log, "b", "X is false")
    log.append({"t": "node_negates", "a": "a", "b": "b"})
    nodes = log.replay()
    assert nodes["a"].negates == {"b"}
    assert nodes["b"].negates == {"a"}


def test_focus_set_is_ignored_in_node_fold(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "r")
    log.append({"t": "focus_set", "session": "s1", "id": "n1"})
    nodes = log.replay()  # should not raise
    assert "n1" in nodes


def test_append_produces_valid_jsonl(tmp_path):
    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    _root(log, "n1", "hello")
    log.append({"t": "node_scored", "id": "n1", "conv": 0.5})
    raw = path.read_text().splitlines()
    assert len(raw) == 2
    for line in raw:
        obj = json.loads(line)
        assert "t" in obj and "ts" in obj


def test_resolve_handles_deleted_target(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "n1", "x")
    _root(log, "n2", "y")
    log.append({"t": "node_merged", "id": "n2", "into": "n1"})
    log.append({"t": "node_deleted", "id": "n1"})
    nodes = log.replay()
    assert resolve(nodes, "n2") is None
    assert resolve(nodes, "n1") is None


def test_node_potential_dupe_records_suggestion(tmp_path):
    """node_potential_dupe records that node `id` looks similar to `dupe_of`,
    surfacing as `node.potential_dupes` for UI rendering. The new node stays
    in place — this is informational, not a structural change."""
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "p", "parent")
    _child(log, "a", "p", "pro", "argument A")
    _child(log, "b", "p", "pro", "argument B")
    log.append({"t": "node_potential_dupe", "id": "b", "dupe_of": "a"})
    nodes = log.replay()
    assert nodes["b"].potential_dupes == ["a"]
    # b is still a normal child of p — not aliased, not merged
    assert "b" in nodes["p"].children


def test_node_potential_dupe_no_duplicate_in_list(tmp_path):
    """Same suggestion emitted twice keeps the list de-duplicated."""
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "p", "parent")
    _child(log, "a", "p", "pro", "A")
    _child(log, "b", "p", "pro", "B")
    log.append({"t": "node_potential_dupe", "id": "b", "dupe_of": "a"})
    log.append({"t": "node_potential_dupe", "id": "b", "dupe_of": "a"})
    nodes = log.replay()
    assert nodes["b"].potential_dupes == ["a"]


def test_node_potential_dupe_multiple_suggestions(tmp_path):
    """A node can have several potential duplicates recorded in the order emitted."""
    log = EventLog(tmp_path / "log.jsonl")
    _root(log, "p", "parent")
    _child(log, "a", "p", "pro", "A")
    _child(log, "b", "p", "pro", "B")
    _child(log, "c", "p", "pro", "C")
    log.append({"t": "node_potential_dupe", "id": "c", "dupe_of": "a"})
    log.append({"t": "node_potential_dupe", "id": "c", "dupe_of": "b"})
    nodes = log.replay()
    assert nodes["c"].potential_dupes == ["a", "b"]


def test_replay_reconstructs_from_disk_only(tmp_path):
    """Writing via one EventLog and reading via a fresh one yields the same state."""
    path = tmp_path / "log.jsonl"
    w = EventLog(path)
    _root(w, "n1", "root")
    _child(w, "n2", "n1", "pro", "pro1")

    r = EventLog(path)
    nodes = r.replay()
    assert set(nodes) == {"n1", "n2"}
    assert nodes["n1"].children == ["n2"]
