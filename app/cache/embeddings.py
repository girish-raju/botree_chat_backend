"""Sentence-embedding provider for the semantic query cache.

`Embedder` lazy-loads the `sentence_transformers` model on first use, so
importing this module (or the whole `app.cache` package) never pulls a
multi-hundred-MB model download into a request path, a test run, or a cold
import. `sentence_transformers` itself is imported *inside* the loader method
for the same reason.

Module-level `init_embedder` / `get_embedder` mirror `app.db.postgres`'s
engine singleton pattern. `warmup()` is meant to be called once from the
FastAPI lifespan (wired up elsewhere, not by this module) so the first real
request doesn't pay the model-load latency.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
import structlog

logger = structlog.get_logger(__name__)

#: Signature of a model loader: takes the model name, returns an object with
#: an `.encode(text, normalize_embeddings=bool) -> array-like` method (the
#: `SentenceTransformer` API). Injectable for tests.
ModelLoader = Callable[[str], Any]


def _default_model_loader(model_name: str) -> Any:
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    return SentenceTransformer(model_name)


class Embedder:
    """Lazily-loaded sentence embedder.

    The underlying model is loaded at most once, on the first call to
    `encode`, via `model_loader` (defaults to loading a real
    `SentenceTransformer`). Pass a fake `model_loader` in tests to avoid any
    network/disk access.
    """

    def __init__(self, model_name: str, model_loader: ModelLoader | None = None) -> None:
        self._model_name = model_name
        self._model_loader = model_loader or _default_model_loader
        self._model: Any | None = None

    def _ensure_loaded(self) -> Any:
        if self._model is None:
            logger.info("embedding_model_loading", model=self._model_name)
            self._model = self._model_loader(self._model_name)
            logger.info("embedding_model_loaded", model=self._model_name)
        return self._model

    def _encode_sync(self, text: str) -> list[float]:
        model = self._ensure_loaded()
        vector = model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vector]

    async def encode(self, text: str) -> list[float]:
        """Return a 384-float, L2-normalized embedding for `text`."""
        return await anyio.to_thread.run_sync(self._encode_sync, text)

    async def warmup(self) -> None:
        """Force the model to load and run one encode, ahead of first request."""
        await self.encode("warmup")


_embedder: Embedder | None = None


def init_embedder(settings: Any, model_loader: ModelLoader | None = None) -> Embedder:
    """Create (and remember) the module-level `Embedder` from `settings.embedding_model`."""
    global _embedder
    _embedder = Embedder(settings.embedding_model, model_loader=model_loader)
    return _embedder


def get_embedder() -> Embedder:
    """Return the module-level `Embedder`, raising if `init_embedder` wasn't called."""
    if _embedder is None:
        raise RuntimeError("Embedder not initialized. Call init_embedder() first.")
    return _embedder


__all__ = ["Embedder", "ModelLoader", "init_embedder", "get_embedder"]
