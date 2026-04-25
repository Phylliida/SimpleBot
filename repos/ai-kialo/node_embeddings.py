"""Append-only per-node embedding store.

Same on-disk format as data/vectors.bin (word embeddings): little-endian float32,
row-major, DIM columns. Writes via append(); reads via get() for one row or
all() for a fresh memmap of the full matrix (used by find_similar).

Usage pattern (caller's responsibility, not enforced here):
    idx = node_embeddings.append(vec)
    event_log.append({"t": "node_created", ..., "embed_idx": idx, ...})
Append FIRST, then emit the event — if the event emit fails, the vector just
leaks (harmless). Reverse order risks a dangling embed_idx.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from embeddings import DIM, VEC_DTYPE

_BYTES_PER_ROW = DIM * VEC_DTYPE.itemsize


class NodeEmbeddings:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def __len__(self) -> int:
        return self.path.stat().st_size // _BYTES_PER_ROW

    def append(self, vec: np.ndarray) -> int:
        if vec.shape != (DIM,):
            raise ValueError(f"expected shape ({DIM},), got {vec.shape}")
        buf = np.ascontiguousarray(vec, dtype=VEC_DTYPE).tobytes()
        idx = len(self)
        with open(self.path, "ab") as f:
            f.write(buf)
        return idx

    def get(self, idx: int) -> np.ndarray:
        n = len(self)
        if not 0 <= idx < n:
            raise IndexError(f"idx {idx} out of range [0, {n})")
        with open(self.path, "rb") as f:
            f.seek(idx * _BYTES_PER_ROW)
            raw = f.read(_BYTES_PER_ROW)
        return np.frombuffer(raw, dtype=VEC_DTYPE).astype(np.float32)

    def all(self) -> np.ndarray:
        """Fresh memmap of the full matrix, shape (N, DIM).

        Call this each time you want the matrix — cheap (OS pages), picks up
        any rows appended since the last call, and avoids stale-memmap issues.
        """
        n = len(self)
        if n == 0:
            return np.zeros((0, DIM), dtype=np.float32)
        return np.memmap(self.path, dtype=VEC_DTYPE, mode="r", shape=(n, DIM))
