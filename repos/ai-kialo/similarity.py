"""Cosine-similarity search over a matrix of embeddings.

Two layers:

- `find_similar(query, matrix, ids, k, threshold)` — pure numerics. Caller provides
  the candidate matrix + parallel id list and gets back top-k `(id, cosine)` pairs,
  sorted descending. Used directly when you've already built your candidate set.

- `find_similar_in_nodes(query, nodes, node_embeddings, k, threshold, scope)` —
  high-level convenience: pulls the live, non-merged, non-deleted nodes' embeddings
  from `NodeEmbeddings`, then delegates to `find_similar`. Used by user-submission
  and tree-expansion code paths.

Note on scale: `matrix @ query` reads every row of the matrix, which is fine for
v0 (thousands of nodes). If N grows past ~100k and this becomes the hot loop, swap
to FAISS or a cached normalized matrix.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np

from embeddings import DIM, sentence_embed
from event_log import Node
from node_embeddings import NodeEmbeddings


def _cosine_scores(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Row-wise cosine similarity between `matrix` (N, D) and `query` (D,).

    Zero rows and zero queries produce score=0 rather than NaN.
    """
    qn = float(np.linalg.norm(query))
    if qn == 0:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    row_norms = np.linalg.norm(matrix, axis=1)
    safe_norms = np.where(row_norms == 0, 1.0, row_norms)
    dots = matrix @ query
    scores = dots / (safe_norms * qn)
    scores = np.where(row_norms == 0, 0.0, scores)
    return scores.astype(np.float32)


def find_similar(
    query: np.ndarray | str,
    matrix: np.ndarray,
    ids: list[str],
    k: int = 10,
    threshold: float | None = None,
) -> list[tuple[str, float]]:
    """Return up to `k` most-similar ids to `query`, descending by cosine.

    - `query` may be a string (tokenized + embedded via sentence_embed) or a (DIM,) vector.
    - `matrix` is (N, DIM); `ids` is a list of N ids parallel to matrix rows.
    - `threshold`, if given, drops results whose cosine is below it.
    - Ties are broken by the stable argsort on the underlying scores.
    """
    if isinstance(query, str):
        query = sentence_embed(query)
    query = np.asarray(query, dtype=np.float32)
    if query.shape != (DIM,):
        raise ValueError(f"query must have shape ({DIM},), got {query.shape}")
    if matrix.shape[0] != len(ids):
        raise ValueError(
            f"matrix has {matrix.shape[0]} rows but ids has {len(ids)} entries"
        )
    if k <= 0 or matrix.shape[0] == 0:
        return []

    scores = _cosine_scores(matrix, query)
    order = np.argsort(-scores, kind="stable")

    result: list[tuple[str, float]] = []
    for i in order:
        s = float(scores[i])
        if threshold is not None and s < threshold:
            break
        result.append((ids[int(i)], s))
        if len(result) >= k:
            break
    return result


def find_similar_in_nodes(
    query: np.ndarray | str,
    nodes: dict[str, Node],
    node_embeddings: NodeEmbeddings,
    k: int = 10,
    threshold: float | None = None,
    scope: Iterable[str] | None = None,
) -> list[tuple[str, float]]:
    """Top-k similar *live* nodes (not deleted, not merged away).

    - `scope`, if given, restricts the search to those node ids. Default = all live nodes.
    - Looks up each in-scope node's embed_idx, stacks the rows from `node_embeddings`,
      and delegates to `find_similar`.
    """
    scope_ids = list(scope) if scope is not None else list(nodes.keys())
    candidate_ids: list[str] = []
    candidate_rows: list[int] = []
    for nid in scope_ids:
        n = nodes.get(nid)
        if n is None or n.deleted or n.merged_into is not None:
            continue
        candidate_ids.append(nid)
        candidate_rows.append(n.embed_idx)

    if not candidate_ids:
        return []

    full = node_embeddings.all()
    sub = np.asarray(full[candidate_rows], dtype=np.float32)
    return find_similar(query, sub, candidate_ids, k=k, threshold=threshold)
