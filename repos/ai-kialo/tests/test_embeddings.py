"""Tests for embeddings.py.

Loads the real data/vocab.txt + data/vectors.bin via memmap — no RAM commit,
so tests are cheap. Run after build_vectors.py has populated data/.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from embeddings import DIM, WordVectors, get_default_vectors, sentence_embed, tokenize


# ---------- tokenize ----------

def test_tokenize_basic():
    assert tokenize("Hello, world!") == ["hello", ",", "world", "!"]


def test_tokenize_lowercases():
    assert tokenize("DOGS Chase Cats") == ["dogs", "chase", "cats"]


def test_tokenize_keeps_punctuation_as_tokens():
    assert tokenize("yes.") == ["yes", "."]


def test_tokenize_empty():
    assert tokenize("") == []


def test_tokenize_multiple_whitespace():
    assert tokenize("  hello\tworld \n") == ["hello", "world"]


# ---------- WordVectors: shared fixture, backed by real data ----------

@pytest.fixture(scope="module")
def wv() -> WordVectors:
    return get_default_vectors()


def test_vocab_loaded(wv: WordVectors):
    assert len(wv) >= 1_000_000
    # build_vectors.py produced these as lines 0, 1, 2:
    assert wv.words[0] == "."
    assert wv.words[1] == "the"
    assert wv.words[2] == ","


def test_lookup_known_word_shape_and_dtype(wv: WordVectors):
    vec = wv["the"]
    assert vec.shape == (DIM,)
    assert vec.dtype == np.float32


def test_roundtrip_first_vector_matches_source(wv: WordVectors):
    """First vector in vectors.bin should match the first vector in the original txt file.

    From earlier bake logs: first vec word='.'  first 5 floats:
      [-0.110582, -0.078721, -0.101495, 0.086523, 0.143684]
    """
    vec = wv["."]
    expected_first_five = np.array(
        [-0.110582, -0.078721, -0.101495, 0.086523, 0.143684], dtype=np.float32
    )
    assert np.allclose(vec[:5], expected_first_five, atol=1e-6)


def test_oov_lookup_returns_none(wv: WordVectors):
    assert wv.get("zzqxfakewordzzzz") is None


def test_known_word_contains(wv: WordVectors):
    assert "the" in wv
    assert "zzqxfakewordzzzz" not in wv


def test_embed_tokens_all_oov_returns_zero(wv: WordVectors):
    v = wv.embed_tokens(["zzqxfake", "anotherfakeword123"])
    assert v.shape == (DIM,)
    assert np.allclose(v, 0)


def test_embed_tokens_mixed_oov_uses_known_only(wv: WordVectors):
    known = wv["the"]
    mixed = wv.embed_tokens(["the", "zzqxfakeword"])
    assert np.allclose(mixed, known)


# ---------- sentence_embed ----------

def test_sentence_embed_shape_and_dtype():
    v = sentence_embed("the dog")
    assert v.shape == (DIM,)
    assert v.dtype == np.float32


def test_sentence_embed_empty_text_returns_zero():
    v = sentence_embed("")
    assert np.allclose(v, 0)


def test_semantic_sanity_animals_vs_finance():
    """Closely-related topics should cosine higher than unrelated ones.

    This exercises the full pipeline: tokenize -> lookup -> mean-pool.
    If it fails, it's a signal that naive mean-pool is insufficient and we
    need IDF weighting or stopword filtering sooner than expected.
    """
    def cos(a, b):
        na = np.linalg.norm(a)
        nb = np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    a = sentence_embed("dogs are animals")
    b = sentence_embed("cats are animals")
    c = sentence_embed("the stock market crashed")

    sim_related = cos(a, b)
    sim_unrelated = cos(a, c)
    assert sim_related > sim_unrelated, (
        f"related ({sim_related:.3f}) should exceed unrelated ({sim_unrelated:.3f})"
    )
