"""Cache subsystem: the reason repeated/paraphrased questions skip the LLM.

- `app.cache.normalizer`: question normalization + temporal-intent extraction.
- `app.cache.embeddings`: Cloudflare-API text-embedding provider.
- `app.cache.templater`: SQL literal parameterization for template reuse.
- `app.cache.semantic`: L0 (exact) + L1 (semantic) query cache.
- `app.cache.results`: L2 result-set cache.
"""

from __future__ import annotations

from app.cache.embeddings import Embedder, get_embedder, init_embedder
from app.cache.normalizer import extract_temporal_intent, normalize_question
from app.cache.results import ResultCache, jsonable_rows, result_cache_key
from app.cache.semantic import QueryCache
from app.cache.templater import bind_template, parameterize_sql

__all__ = [
    "normalize_question",
    "extract_temporal_intent",
    "Embedder",
    "init_embedder",
    "get_embedder",
    "parameterize_sql",
    "bind_template",
    "QueryCache",
    "ResultCache",
    "result_cache_key",
    "jsonable_rows",
]
