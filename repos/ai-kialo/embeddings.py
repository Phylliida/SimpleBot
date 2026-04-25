"""Word-average sentence embeddings from the dolma word vectors.

Loads `data/vocab.txt` + `data/vectors.bin` (memmap-backed, no RAM commit),
exposes `sentence_embed(text) -> np.ndarray` for one-shot use and `WordVectors`
for finer control.

v0 is pure naive mean-pool — no IDF weighting, no stopword filter. Both are
known weaknesses (stopwords swamp content, word order is lost); we'll layer
either on top when concrete quality issues show up.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

DIM = 300
VEC_DTYPE = np.dtype("<f4")  # little-endian float32, matches build_vectors.py

_ROOT = Path(__file__).resolve().parent
DEFAULT_VOCAB = _ROOT / "data" / "vocab.txt"
DEFAULT_VECTORS = _ROOT / "data" / "vectors.bin"

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lowercase; split on whitespace; keep punctuation as its own token.

    Matches roughly how word2vec-style corpora are typically tokenized,
    and the dolma vocab has punctuation entries (".", "," as the first two).
    """
    return _TOKEN_RE.findall(text.lower())


class WordVectors:
    """Read-only view of vocab.txt + vectors.bin.

    Vectors are memmapped — cost is ~zero RAM at construction; pages fault in on access.
    """

    def __init__(
        self,
        vocab_path: Path | str = DEFAULT_VOCAB,
        vectors_path: Path | str = DEFAULT_VECTORS,
    ):
        vocab_path = Path(vocab_path)
        vectors_path = Path(vectors_path)

        with open(vocab_path, encoding="utf-8") as f:
            self.words: list[str] = [line.rstrip("\n") for line in f]
        self.word_to_idx: dict[str, int] = {w: i for i, w in enumerate(self.words)}

        expected_bytes = len(self.words) * DIM * VEC_DTYPE.itemsize
        actual_bytes = vectors_path.stat().st_size
        if actual_bytes != expected_bytes:
            raise ValueError(
                f"vectors.bin size {actual_bytes} != expected {expected_bytes} "
                f"(vocab has {len(self.words)} words × {DIM} dims × {VEC_DTYPE.itemsize} bytes)"
            )

        self.vectors: np.ndarray = np.memmap(
            vectors_path, dtype=VEC_DTYPE, mode="r", shape=(len(self.words), DIM)
        )

    def __len__(self) -> int:
        return len(self.words)

    def __contains__(self, word: str) -> bool:
        return word in self.word_to_idx

    def __getitem__(self, word: str) -> np.ndarray:
        return np.asarray(self.vectors[self.word_to_idx[word]], dtype=np.float32)

    def get(self, word: str) -> np.ndarray | None:
        idx = self.word_to_idx.get(word)
        return np.asarray(self.vectors[idx], dtype=np.float32) if idx is not None else None

    def embed_tokens(self, tokens: list[str]) -> np.ndarray:
        """Mean-pool known tokens' vectors. Returns zero vector if all tokens are OOV."""
        idxs = [self.word_to_idx[t] for t in tokens if t in self.word_to_idx]
        if not idxs:
            return np.zeros(DIM, dtype=np.float32)
        return np.asarray(self.vectors[idxs], dtype=np.float32).mean(axis=0)

    def embed(self, text: str) -> np.ndarray:
        return self.embed_tokens(tokenize(text))


_default: WordVectors | None = None


def get_default_vectors() -> WordVectors:
    """Module-level singleton backed by data/vocab.txt + data/vectors.bin."""
    global _default
    if _default is None:
        _default = WordVectors()
    return _default


def sentence_embed(text: str) -> np.ndarray:
    """Tokenize text and mean-pool its word vectors into a (DIM,) float32 array."""
    return get_default_vectors().embed(text)
