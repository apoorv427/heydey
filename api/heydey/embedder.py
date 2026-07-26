"""Embedder — mirrors the proven BLOS `vector_store.embed_texts` exactly.

The 35,086 migrated KB points were embedded with BAAI/bge-small-en-v1.5 (384-dim);
queries MUST use the same model or retrieval silently degrades. The corpus itself
is NEVER re-embedded (L7 — copy in-place).

Persistent cache dir: fastembed's default is the system temp dir, which macOS
purges — that killed the LightRAG nightly in 2026-07 (model file gone, HF cache
metadata intact, so fastembed never re-downloaded). Same ~/.cache/fastembed dir
as BLOS, so heydey reuses the already-downloaded model.
"""

import os
from functools import lru_cache

MODEL_NAME = "BAAI/bge-small-en-v1.5"
VECTOR_SIZE = 384


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding  # deferred: ~1s import + model mmap

    cache_dir = os.path.expanduser("~/.cache/fastembed")
    os.makedirs(cache_dir, exist_ok=True)
    return TextEmbedding(MODEL_NAME, cache_dir=cache_dir)


def embed_texts(texts: list[str]) -> list[list[float]]:
    return [e.tolist() for e in _model().embed(texts)]
