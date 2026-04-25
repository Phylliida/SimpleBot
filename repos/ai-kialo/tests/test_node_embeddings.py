"""Tests for NodeEmbeddings: append/get roundtrip, all() returns full matrix, error cases."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from embeddings import DIM
from node_embeddings import NodeEmbeddings


def _rand(rng: np.random.Generator) -> np.ndarray:
    return rng.standard_normal(DIM).astype(np.float32)


def test_empty_store_has_zero_length(tmp_path):
    store = NodeEmbeddings(tmp_path / "node.bin")
    assert len(store) == 0


def test_empty_all_returns_empty_matrix(tmp_path):
    store = NodeEmbeddings(tmp_path / "node.bin")
    m = store.all()
    assert m.shape == (0, DIM)


def test_append_returns_monotonic_index(tmp_path):
    rng = np.random.default_rng(0)
    store = NodeEmbeddings(tmp_path / "node.bin")
    assert store.append(_rand(rng)) == 0
    assert store.append(_rand(rng)) == 1
    assert store.append(_rand(rng)) == 2
    assert len(store) == 3


def test_roundtrip_single(tmp_path):
    rng = np.random.default_rng(1)
    store = NodeEmbeddings(tmp_path / "node.bin")
    v = _rand(rng)
    idx = store.append(v)
    got = store.get(idx)
    assert got.shape == (DIM,)
    assert got.dtype == np.float32
    assert np.allclose(got, v)


def test_roundtrip_multiple(tmp_path):
    rng = np.random.default_rng(42)
    store = NodeEmbeddings(tmp_path / "node.bin")
    vecs = [_rand(rng) for _ in range(10)]
    for v in vecs:
        store.append(v)
    for i, expected in enumerate(vecs):
        assert np.allclose(store.get(i), expected)


def test_all_returns_stacked_matrix(tmp_path):
    rng = np.random.default_rng(2)
    store = NodeEmbeddings(tmp_path / "node.bin")
    vecs = np.stack([_rand(rng) for _ in range(5)])
    for v in vecs:
        store.append(v)
    m = store.all()
    assert m.shape == (5, DIM)
    assert np.allclose(m, vecs)


def test_all_picks_up_new_appends(tmp_path):
    rng = np.random.default_rng(3)
    store = NodeEmbeddings(tmp_path / "node.bin")
    store.append(_rand(rng))
    assert store.all().shape == (1, DIM)
    store.append(_rand(rng))
    assert store.all().shape == (2, DIM)


def test_get_out_of_range_raises(tmp_path):
    store = NodeEmbeddings(tmp_path / "node.bin")
    with pytest.raises(IndexError):
        store.get(0)
    store.append(np.zeros(DIM, dtype=np.float32))
    with pytest.raises(IndexError):
        store.get(1)
    with pytest.raises(IndexError):
        store.get(-1)


def test_append_wrong_shape_raises(tmp_path):
    store = NodeEmbeddings(tmp_path / "node.bin")
    with pytest.raises(ValueError):
        store.append(np.zeros(DIM - 1, dtype=np.float32))
    with pytest.raises(ValueError):
        store.append(np.zeros((1, DIM), dtype=np.float32))


def test_persistence_across_instances(tmp_path):
    """A new NodeEmbeddings reading the same file sees previously-appended rows."""
    rng = np.random.default_rng(7)
    p = tmp_path / "node.bin"
    w = NodeEmbeddings(p)
    v = _rand(rng)
    w.append(v)
    r = NodeEmbeddings(p)
    assert len(r) == 1
    assert np.allclose(r.get(0), v)


def test_accepts_non_contiguous_input(tmp_path):
    """Slices / views get made contiguous via ascontiguousarray before write."""
    rng = np.random.default_rng(8)
    store = NodeEmbeddings(tmp_path / "node.bin")
    big = rng.standard_normal((3, DIM)).astype(np.float32)
    # row 1 of `big` is a view, not contiguous on its own depending on layout
    store.append(big[1])
    assert np.allclose(store.get(0), big[1])
