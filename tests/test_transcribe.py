"""Tests for the `POST /api/transcribe` speech-to-text endpoint.

The Cloudflare Whisper call is faked two ways: at the dependency level
(`dependency_overrides[get_transcriber]`) for endpoint behavior, and at the
httpx transport level (`httpx.MockTransport`) to verify the payload
`CloudflareWhisper` actually sends. Auth uses the real login flow against a
seeded SQLite user, matching `test_chat_api.py`.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.transcribe import get_transcriber
from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.models import User
from app.errors import UpstreamLLMError
from app.llm.whisper import CloudflareWhisper

PASSWORD = "s3cret-pass"


class FakeTranscriber:
    """Records the audio bytes it was given and returns a fixed transcript."""

    def __init__(self, text: str = "hello world") -> None:
        self.text = text
        self.received: bytes | None = None

    async def transcribe(self, audio: bytes) -> str:
        self.received = audio
        return self.text


class FailingTranscriber:
    async def transcribe(self, audio: bytes) -> str:
        raise UpstreamLLMError("Cloudflare Whisper request failed: boom")


@pytest_asyncio.fixture
async def seeded_user(db_sessionmaker: async_sessionmaker[AsyncSession]) -> User:
    async with db_sessionmaker() as session:
        user = User(
            username="rsm",
            password_hash=hash_password(PASSWORD),
            display_name="Priya - RSM South",
            role="RSM",
            sf_code="303",
            sf_level=300,
            allowed_geo_col="geo_hier3_name",
            allowed_geo_vals=["REGION 6"],
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _token(client: AsyncClient) -> str:
    resp = await client.post("/api/auth/login", json={"username": "rsm", "password": PASSWORD})
    return resp.json()["token"]


def _upload(data: bytes, content_type: str = "audio/webm"):
    return {"audio": ("dictation.webm", data, content_type)}


async def test_transcribe_returns_text(app: FastAPI, client: AsyncClient, seeded_user: User):
    fake = FakeTranscriber("how many distributors do I have")
    app.dependency_overrides[get_transcriber] = lambda: fake
    token = await _token(client)

    resp = await client.post(
        "/api/transcribe",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload(b"fake-opus-bytes"),
    )

    assert resp.status_code == 200
    assert resp.json() == {"text": "how many distributors do I have"}
    assert fake.received == b"fake-opus-bytes"

    app.dependency_overrides.pop(get_transcriber, None)


async def test_transcribe_requires_auth(app: FastAPI, client: AsyncClient):
    app.dependency_overrides[get_transcriber] = lambda: FakeTranscriber()

    resp = await client.post("/api/transcribe", files=_upload(b"fake"))

    assert resp.status_code == 401

    app.dependency_overrides.pop(get_transcriber, None)


async def test_transcribe_empty_upload(app: FastAPI, client: AsyncClient, seeded_user: User):
    fake = FakeTranscriber()
    app.dependency_overrides[get_transcriber] = lambda: fake
    token = await _token(client)

    resp = await client.post(
        "/api/transcribe",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload(b""),
    )

    assert resp.status_code == 200
    assert resp.json() == {"text": ""}
    assert fake.received is None  # Cloudflare never called

    app.dependency_overrides.pop(get_transcriber, None)


async def test_transcribe_oversize_upload(app: FastAPI, client: AsyncClient, seeded_user: User):
    app.dependency_overrides[get_transcriber] = lambda: FakeTranscriber()
    token = await _token(client)

    resp = await client.post(
        "/api/transcribe",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload(b"x" * (10 * 1024 * 1024 + 1)),
    )

    assert resp.status_code == 413

    app.dependency_overrides.pop(get_transcriber, None)


async def test_transcribe_upstream_failure_maps_to_502(
    app: FastAPI, client: AsyncClient, seeded_user: User
):
    app.dependency_overrides[get_transcriber] = lambda: FailingTranscriber()
    token = await _token(client)

    resp = await client.post(
        "/api/transcribe",
        headers={"Authorization": f"Bearer {token}"},
        files=_upload(b"fake"),
    )

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "llm_error"

    app.dependency_overrides.pop(get_transcriber, None)


async def test_cloudflare_whisper_sends_base64_audio():
    """`CloudflareWhisper` posts `{"audio": <base64>}` to the configured model URL."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"result": {"text": "  hello  "}, "success": True})

    settings = get_settings().model_copy()
    settings.cloudflare_account_id = "acct-123"
    settings.cloudflare_api_token = "tok-456"
    settings.cloudflare_whisper_model = "@cf/openai/whisper-large-v3-turbo"

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    whisper = CloudflareWhisper(settings, client=client)

    text = await whisper.transcribe(b"opus-audio-bytes")

    assert text == "hello"
    assert captured["url"] == (
        "https://api.cloudflare.com/client/v4/accounts/acct-123"
        "/ai/run/@cf/openai/whisper-large-v3-turbo"
    )
    assert captured["auth"] == "Bearer tok-456"
    assert captured["payload"] == {"audio": base64.b64encode(b"opus-audio-bytes").decode("ascii")}


async def test_cloudflare_whisper_http_error_raises_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    whisper = CloudflareWhisper(get_settings().model_copy(), client=client)

    with pytest.raises(UpstreamLLMError):
        await whisper.transcribe(b"bytes")
