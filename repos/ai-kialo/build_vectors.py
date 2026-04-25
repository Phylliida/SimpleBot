"""One-off bake: convert the dolma word2vec text file to a compact offset-indexed form.

Input  : models/dolma_300_2024_1.2M.100_combined.txt  (word + 300 floats per line, space-sep)
Output : data/vocab.txt     (one word per line; line number = vector index)
         data/vectors.bin   (float32, row-major; row i = vectors.bin[i * DIM * 4 : (i+1) * DIM * 4])

Stdlib only. Run once; rerun if the source file changes.
"""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "models" / "dolma_300_2024_1.2M.100_combined.txt"
OUT_DIR = ROOT / "data"
VOCAB = OUT_DIR / "vocab.txt"
VECTORS = OUT_DIR / "vectors.bin"
DIM = 300
PACK = struct.Struct(f"<{DIM}f").pack
PROGRESS_EVERY = 50_000


def main() -> int:
    if not SRC.exists():
        print(f"source file not found: {SRC}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    kept = 0
    skipped = 0
    start = time.time()
    expected_parts = DIM + 1

    with open(SRC, encoding="utf-8", errors="replace") as f_in, \
         open(VOCAB, "w", encoding="utf-8") as f_vocab, \
         open(VECTORS, "wb") as f_vec:
        for i, line in enumerate(f_in):
            parts = line.rstrip("\n").split(" ")
            if len(parts) != expected_parts:
                skipped += 1
                if skipped <= 5:
                    print(f"skip line {i}: {len(parts)} parts (want {expected_parts})", file=sys.stderr)
                continue
            word = parts[0]
            if not word:
                skipped += 1
                continue
            try:
                floats = [float(x) for x in parts[1:]]
            except ValueError as e:
                skipped += 1
                if skipped <= 5:
                    print(f"skip line {i}: float parse error: {e}", file=sys.stderr)
                continue
            f_vocab.write(word + "\n")
            f_vec.write(PACK(*floats))
            kept += 1
            if kept % PROGRESS_EVERY == 0:
                rate = kept / max(1e-9, time.time() - start)
                print(f"  {kept:>9,} kept  ({rate:,.0f}/s)", file=sys.stderr, flush=True)

    elapsed = time.time() - start
    print(f"done: {kept:,} vectors in {elapsed:.1f}s  (skipped {skipped})", file=sys.stderr)
    print(f"  vocab.txt   -> {VOCAB}  ({VOCAB.stat().st_size:,} bytes)", file=sys.stderr)
    print(f"  vectors.bin -> {VECTORS}  ({VECTORS.stat().st_size:,} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
