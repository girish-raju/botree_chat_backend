"""Cloudflare Workers AI Whisper speech-to-text client.

Mirrors `CloudflareProvider`'s HTTP conventions: same `/ai/run/{model}`
endpoint, Bearer token auth, and `UpstreamLLMError` on transport failures.
Whisper-large-v3-turbo takes base64-encoded audio in a JSON body and returns
`{"result": {"text": ...}, "success": true}`.
"""

from __future__ import annotations

import base64

import httpx

from app.config import Settings
from app.errors import UpstreamLLMError

_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"


class CloudflareWhisper:
    """Transcribes audio clips via Cloudflare Workers AI Whisper."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=60.0)

    @property
    def _url(self) -> str:
        return _BASE_URL.format(
            account_id=self._settings.cloudflare_account_id,
            model=self._settings.cloudflare_whisper_model,
        )

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._settings.cloudflare_api_token}"}

    async def transcribe(self, audio: bytes) -> str:
        payload = {"audio": base64.b64encode(audio).decode("ascii")}
        try:
            response = await self._client.post(self._url, headers=self._headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamLLMError(f"Cloudflare Whisper request failed: {exc}") from exc
        result = response.json().get("result") or {}
        text = result.get("text") or ""
        return text.strip()
