"""Tests for the streaming `POST /api/chat` endpoint.

The pipeline is fully faked via `dependency_overrides[get_pipeline]`, so no LLM,
MySQL, embedding model, or query cache is touched. Auth uses the real login
flow against a seeded SQLite user.
"""

from __future__ import annotations

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.chat import get_pipeline
from app.auth.passwords import hash_password
from app.chat.pipeline import Done, TextDelta, ToolResult, ToolSQL
from app.db.models import User

PASSWORD = "s3cret-pass"


class FakePipeline:
    """Emits a fixed sequence of pipeline events (no real work)."""

    async def run(self, *, user, thread_id, question, history, session):
        yield ToolSQL("SELECT code FROM distributor_t WHERE 1=1")
        yield ToolResult(
            {"sql": "SELECT code FROM distributor_t", "columns": ["code"],
             "rows": [{"code": "D1"}], "row_count": 1, "cached": False}
        )
        yield TextDelta("You have ")
        yield TextDelta("1 distributor.")
        yield Done()


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


async def test_chat_streams_v6_frames(app: FastAPI, client: AsyncClient, seeded_user: User):
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline()
    token = await _token(client)

    resp = await client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messages": [
                {"role": "user", "parts": [{"type": "text", "text": "how many distributors"}]}
            ],
            "threadId": None,
        },
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers.get("x-vercel-ai-ui-message-stream") == "v1"

    body = resp.text
    assert '"type":"start"' in body
    assert '"type":"start-step"' in body
    assert '"type":"tool-input-available"' in body
    assert '"toolName":"query_database"' in body
    assert '"type":"tool-output-available"' in body
    assert '"type":"text-delta"' in body
    assert "1 distributor." in body
    assert '"type":"finish"' in body
    assert "data: [DONE]" in body

    app.dependency_overrides.pop(get_pipeline, None)


async def test_chat_requires_auth(app: FastAPI, client: AsyncClient):
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline()

    resp = await client.post(
        "/api/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "auth_error"

    app.dependency_overrides.pop(get_pipeline, None)


async def test_chat_parses_legacy_content_and_history(
    app: FastAPI, client: AsyncClient, seeded_user: User
):
    captured = {}

    class CapturingPipeline:
        async def run(self, *, user, thread_id, question, history, session):
            captured["question"] = question
            captured["history"] = history
            captured["thread_id"] = thread_id
            yield TextDelta("ok")
            yield Done()

    app.dependency_overrides[get_pipeline] = lambda: CapturingPipeline()
    token = await _token(client)

    resp = await client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "threadId": "t-123",
            "messages": [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "an answer"},
                {"role": "user", "content": "second question"},
            ],
        },
    )

    assert resp.status_code == 200
    assert captured["question"] == "second question"
    assert captured["thread_id"] == "t-123"
    assert [t.text for t in captured["history"]] == ["first question", "an answer"]

    app.dependency_overrides.pop(get_pipeline, None)
