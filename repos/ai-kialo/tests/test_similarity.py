"""Tests for similarity.py.

Covers the pure `find_similar` numerics (orthogonal, opposite, identity, threshold,
k bounds, mismatches, zero queries) and the node-graph wrapper `find_similar_in_nodes`
(live-filter, scope, text query).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from embeddings import DIM
from event_log import EventLog
from node_embeddings import NodeEmbeddings
from similarity import _cosine_scores, find_similar, find_similar_in_nodes


# ---------- helpers ----------

def _unit(rng: np.random.Generator, d: int = DIM) -> np.ndarray:
    v = rng.standard_normal(d).astype(np.float32)
    return v / np.linalg.norm(v)


def _matrix_from(vecs: list[np.ndarray]) -> np.ndarray:
    return np.stack(vecs).astype(np.float32)


# ---------- _cosine_scores ----------

def test_cosine_identity():
    v = np.array([1, 0, 0] + [0] * (DIM - 3), dtype=np.float32)
    m = _matrix_from([v])
    assert np.allclose(_cosine_scores(m, v), [1.0], atol=1e-6)


def test_cosine_orthogonal_is_zero():
    a = np.zeros(DIM, dtype=np.float32); a[0] = 1
    b = np.zeros(DIM, dtype=np.float32); b[1] = 1
    assert abs(_cosine_scores(_matrix_from([b]), a)[0]) < 1e-6


def test_cosine_opposite_is_minus_one():
    a = np.zeros(DIM, dtype=np.float32); a[0] = 1
    b = -a
    assert abs(_cosine_scores(_matrix_from([b]), a)[0] + 1.0) < 1e-6


def test_cosine_zero_query_returns_zeros():
    q = np.zeros(DIM, dtype=np.float32)
    m = np.ones((3, DIM), dtype=np.float32)
    assert np.allclose(_cosine_scores(m, q), 0.0)


def test_cosine_zero_rows_score_zero_not_nan():
    q = np.ones(DIM, dtype=np.float32)
    m = np.zeros((2, DIM), dtype=np.float32)
    s = _cosine_scores(m, q)
    assert np.allclose(s, 0.0)
    assert not np.isnan(s).any()


# ---------- find_similar: pure numerics ----------

def test_find_similar_empty_matrix_returns_empty():
    assert find_similar(np.ones(DIM, dtype=np.float32), np.zeros((0, DIM), dtype=np.float32), [], k=5) == []


def test_find_similar_k_zero_returns_empty():
    m = np.ones((3, DIM), dtype=np.float32)
    assert find_similar(np.ones(DIM, dtype=np.float32), m, ["a", "b", "c"], k=0) == []


def test_find_similar_returns_k_sorted_descending():
    # three distinct, not-too-similar vectors plus the query copies one of them
    a = np.zeros(DIM, dtype=np.float32); a[0] = 1.0
    b = np.zeros(DIM, dtype=np.float32); b[1] = 1.0
    c = np.zeros(DIM, dtype=np.float32); c[2] = 1.0
    m = _matrix_from([a, b, c])
    results = find_similar(a, m, ["a", "b", "c"], k=3)
    assert len(results) == 3
    assert results[0][0] == "a"
    # scores must be monotonically non-increasing
    for i in range(len(results) - 1):
        assert results[i][1] >= results[i + 1][1]


def test_find_similar_respects_k():
    rng = np.random.default_rng(0)
    vecs = [_unit(rng) for _ in range(10)]
    m = _matrix_from(vecs)
    ids = [f"n{i}" for i in range(10)]
    assert len(find_similar(vecs[0], m, ids, k=3)) == 3
    assert len(find_similar(vecs[0], m, ids, k=7)) == 7


def test_find_similar_k_larger_than_n_caps():
    rng = np.random.default_rng(0)
    vecs = [_unit(rng) for _ in range(3)]
    m = _matrix_from(vecs)
    assert len(find_similar(vecs[0], m, ["a", "b", "c"], k=100)) == 3


def test_find_similar_threshold_filters():
    a = np.zeros(DIM, dtype=np.float32); a[0] = 1.0
    b = np.zeros(DIM, dtype=np.float32); b[1] = 1.0  # orthogonal
    c = -a                                            # opposite
    m = _matrix_from([a, b, c])
    # only `a` should pass threshold=0.5
    results = find_similar(a, m, ["a", "b", "c"], k=10, threshold=0.5)
    assert [id for id, _ in results] == ["a"]


def test_find_similar_threshold_none_keeps_all():
    a = np.zeros(DIM, dtype=np.float32); a[0] = 1.0
    c = -a
    m = _matrix_from([a, c])
    results = find_similar(a, m, ["a", "c"], k=5, threshold=None)
    assert len(results) == 2


def test_find_similar_query_string_tokenized_and_embedded():
    """String queries should be embedded via sentence_embed. Smoke test the wiring."""
    a = np.ones(DIM, dtype=np.float32)
    m = _matrix_from([a])
    results = find_similar("some text", m, ["only"], k=1)
    # we don't assert the cosine value (depends on embedding), just the pipeline
    assert len(results) == 1
    assert results[0][0] == "only"
    assert isinstance(results[0][1], float)


def test_find_similar_ids_length_mismatch_raises():
    m = np.ones((3, DIM), dtype=np.float32)
    with pytest.raises(ValueError):
        find_similar(np.ones(DIM, dtype=np.float32), m, ["a", "b"], k=1)


def test_find_similar_query_wrong_shape_raises():
    m = np.ones((1, DIM), dtype=np.float32)
    with pytest.raises(ValueError):
        find_similar(np.zeros(DIM - 1, dtype=np.float32), m, ["a"], k=1)


def test_find_similar_preserves_float_scores():
    a = np.array([1.0, 0.0, 0.0] + [0.0] * (DIM - 3), dtype=np.float32)
    results = find_similar(a, _matrix_from([a]), ["a"], k=1)
    assert isinstance(results[0][1], float)
    assert results[0][1] == pytest.approx(1.0, abs=1e-6)


# ---------- find_similar_in_nodes ----------

def _make_store_and_nodes(tmp_path, texts: list[tuple[str, str]]) -> tuple[dict, NodeEmbeddings, EventLog]:
    """Build (nodes, node_embeddings, event_log) from a list of (id, text).

    Each node gets a deterministic non-zero embedding so cosine comparisons are stable.
    Uses a hash-derived base vector so identical text gives identical vectors.
    """
    log = EventLog(tmp_path / "events.jsonl")
    store = NodeEmbeddings(tmp_path / "node.bin")
    for nid, text in texts:
        # deterministic per-text embedding
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.standard_normal(DIM).astype(np.float32)
        v = v / np.linalg.norm(v)
        idx = store.append(v)
        log.append({
            "t": "node_created", "id": nid, "parent": None, "stance": "root",
            "text": text, "embed_idx": idx,
        })
    return log.replay(), store, log


def test_find_similar_in_nodes_identity_match(tmp_path):
    nodes, store, _ = _make_store_and_nodes(tmp_path, [
        ("n1", "claim about cats"),
        ("n2", "claim about dogs"),
        ("n3", "claim about fish"),
    ])
    # Querying with n2's own vector must place n2 first.
    q = store.get(nodes["n2"].embed_idx)
    results = find_similar_in_nodes(q, nodes, store, k=3)
    assert results[0][0] == "n2"
    assert results[0][1] == pytest.approx(1.0, abs=1e-5)


def test_find_similar_in_nodes_filters_deleted(tmp_path):
    nodes, store, log = _make_store_and_nodes(tmp_path, [
        ("n1", "alpha"),
        ("n2", "beta"),
    ])
    log.append({"t": "node_deleted", "id": "n2"})
    nodes = log.replay()
    q = store.get(nodes["n2"].embed_idx)  # still queryable
    results = find_similar_in_nodes(q, nodes, store, k=5)
    assert "n2" not in [id for id, _ in results]
    assert "n1" in [id for id, _ in results]


def test_find_similar_in_nodes_filters_merged(tmp_path):
    nodes, store, log = _make_store_and_nodes(tmp_path, [
        ("canonical", "x"),
        ("dupe", "y"),
    ])
    log.append({"t": "node_merged", "id": "dupe", "into": "canonical"})
    nodes = log.replay()
    results = find_similar_in_nodes(store.get(0), nodes, store, k=5)
    assert "dupe" not in [id for id, _ in results]


def test_find_similar_in_nodes_scope_restricts(tmp_path):
    nodes, store, _ = _make_store_and_nodes(tmp_path, [
        ("a", "one"), ("b", "two"), ("c", "three"),
    ])
    # scope to only {b, c}: a must not appear
    results = find_similar_in_nodes(
        store.get(nodes["a"].embed_idx), nodes, store, k=5, scope=["b", "c"]
    )
    assert set(id for id, _ in results) == {"b", "c"}


def test_find_similar_in_nodes_empty_graph(tmp_path):
    store = NodeEmbeddings(tmp_path / "node.bin")
    assert find_similar_in_nodes("hi", {}, store, k=5) == []


def test_find_similar_in_nodes_accepts_text_query(tmp_path):
    """End-to-end: real sentence_embed + real node embeddings from stored word-avg vectors.

    This is a full-pipeline sanity test: similar claims should outrank different ones.
    """
    from embeddings import sentence_embed
    log = EventLog(tmp_path / "events.jsonl")
    store = NodeEmbeddings(tmp_path / "node.bin")

    for nid, text in [
        ("cats", "cats are wonderful animals"),
        ("dogs", "dogs are loyal animals"),
        ("market", "the stock market crashed this morning"),
    ]:
        v = sentence_embed(text)
        idx = store.append(v)
        log.append({
            "t": "node_created", "id": nid, "parent": None, "stance": "root",
            "text": text, "embed_idx": idx,
        })
    nodes = log.replay()

    results = find_similar_in_nodes("puppies are cute pets", nodes, store, k=3)
    assert len(results) == 3
    # animals-topic claims should rank above finance
    top_two = {id for id, _ in results[:2]}
    assert "market" not in top_two, f"finance topic leaked into top-2: {results}"
