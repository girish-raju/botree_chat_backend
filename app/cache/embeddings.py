"""Text-embedding provider for the semantic query cache.

Embeddings are produced by the Cloudflare Workers AI embeddings API
(`@cf/baai/bge-small-en-v1.5` by default) rather than a locally-downloaded
model, so no multi-hundred-MB model — and no `torch` — is ever installed on the
server. It's the same small BGE model, just called as an API, consistent with
how the LLM is used.

Module-level `init_embedder` / `get_embedder` mirror `app.db.postgres`'s engine
singleton pattern. `warmup()` is called once from the FastAPI lifespan so a
misconfigured embeddings API surfaces in the logs before the first request.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.config import Settings
from app.errors import UpstreamLLMError

logger = structlog.get_logger(__name__)

_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"


def _l2_normalize(vector: list[float]) -> list[float]:
    """Scale `vector` to unit length (no-op for an all-zero or already-unit vector)."""
    norm = sum(x * x for x in vector) ** 0.5
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


class Embedder:
    """Text embedder backed by the Cloudflare Workers AI embeddings API.

    `encode` returns an L2-normalized embedding for a piece of text. An
    `httpx.AsyncClient` may be injected (e.g. in tests) to avoid real network
    calls; otherwise one is created and owned by this instance.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._own_client = client is None

    @property
    def _url(self) -> str:
        return _BASE_URL.format(
            account_id=self._settings.cloudflare_account_id,
            model=self._settings.cloudflare_embedding_model,
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.cloudflare_api_token}"}

    async def encode(self, text: str) -> list[float]:
        """Return an L2-normalized embedding vector for `text`."""
        try:
            response = await self._client.post(
                self._url, headers=self._headers, json={"text": text}
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise UpstreamLLMError(f"Cloudflare embeddings request failed: {exc}") from exc
        return _l2_normalize(self._extract_vector(body))

    @staticmethod
    def _extract_vector(body: Any) -> list[float]:
        """Pull the 1-D float vector out of a Cloudflare embeddings response.

        Shape: ``{"result": {"shape": [1, N], "data": [[...N floats...]]},
        "success": true}``.
        """
        if isinstance(body, dict) and body.get("success") is False:
            raise UpstreamLLMError(f"Cloudflare embeddings error: {body.get('errors')}")
        try:
            data = body["result"]["data"]
            vector = data[0] if data and isinstance(data[0], list) else data
            return [float(x) for x in vector]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise UpstreamLLMError(
                f"Unexpected Cloudflare embeddings response: {body!r}"
            ) from exc

    async def warmup(self) -> None:
        """Make one embedding call so a misconfig surfaces in logs before traffic.

        Runs as a background task from the lifespan; never raises — a failure
        here must not block startup, and the first real request would surface it
        anyway.
        """
        try:
            await self.encode("warmup")
            logger.info("embedder_ready")
        except Exception:
            logger.warning("embedder_warmup_failed", exc_info=True)

    async def aclose(self) -> None:
        """Close the owned HTTP client (no-op if a client was injected)."""
        if self._own_client:
            await self._client.aclose()


_embedder: Embedder | None = None


def init_embedder(settings: Settings, client: httpx.AsyncClient | None = None) -> Embedder:
    """Create (and remember) the module-level `Embedder`."""
    global _embedder
    _embedder = Embedder(settings, client=client)
    return _embedder


def get_embedder() -> Embedder:
    """Return the module-level `Embedder`, raising if `init_embedder` wasn't called."""
    if _embedder is None:
        raise RuntimeError("Embedder not initialized. Call init_embedder() first.")
    return _embedder


async def dispose_embedder() -> None:
    """Close and forget the module-level `Embedder`, if any."""
    global _embedder
    if _embedder is not None:
        await _embedder.aclose()
    _embedder = None


__all__ = ["Embedder", "init_embedder", "get_embedder", "dispose_embedder"]
