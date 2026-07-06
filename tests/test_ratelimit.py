"""Tests for `app.middleware.ratelimit.RateLimitMiddleware`.

The `app`/`client` fixtures (see `conftest.py`) disable rate limiting by
default so the rest of the suite isn't affected; each test here explicitly
re-enables it (and dials the limits down) via `monkeypatch.setattr` on the
live `Settings` singleton, then resets bucket state so tests don't leak into
each other.
"""

from __future__ import annotations

import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.chat import get_pipeline
from app.auth.passwords import hash_password
from app.chat.pipeline import Done, TextDelta
from app.config import get_settings
from app.db.models import User
from app.middleware import ratelimit as ratelimit_module

PASSWORD = "s3cret-pass"


class FakePipeline:
    """Emits a trivial event sequence -- no real LLM/DB work."""

    async def run(self, *, user, thread_id, question, history, session):
        yield TextDelta("ok")
        yield Done()


@pytest_asyncio.fixture(autouse=True)
async def _reset_buckets():
    ratelimit_module.reset()
    yield
    ratelimit_module.reset()


@pytest_asyncio.fixture
async def seeded_user(db_sessionmaker: async_sessionmaker[AsyncSession]) -> User:
    async with db_sessionmaker() as session:
        user = User(
            username="rl-user",
            password_hash=hash_password(PASSWORD),
            display_name="Rate Limit User",
            role="RSM",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest_asyncio.fixture
async def second_user(db_sessionmaker: async_sessionmaker[AsyncSession]) -> User:
    async with db_sessionmaker() as session:
        user = User(
            username="rl-user-2",
            password_hash=hash_password(PASSWORD),
            display_name="Second User",
            role="RSM",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _login(client: AsyncClient, username: str) -> str:
    resp = await client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200
    return resp.json()["token"]


def _enable(monkeypatch, **overrides) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


async def test_under_limit_returns_200s(client: AsyncClient, monkeypatch) -> None:
    _enable(monkeypatch, rate_limit_default_per_min=5)

    for _ in range(5):
        resp = await client.get("/api/auth/me")
        assert resp.status_code in (401, 200)  # unauthenticated -> 401, but never 429


async def test_default_bucket_exceeded_returns_429_envelope(client: AsyncClient, monkeypatch) -> None:
    _enable(monkeypatch, rate_limit_default_per_min=3)

    statuses = []
    for _ in range(5):
        resp = await client.get("/api/auth/me")
        statuses.append(resp.status_code)

    assert 429 in statuses
    # Re-issue to inspect the 429 body/headers directly.
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "rate_limited"
    assert "message" in body["error"]
    assert "request_id" in body["error"]
    assert "retry-after" in {k.lower() for k in resp.headers.keys()}
    assert int(resp.headers["retry-after"]) >= 1


async def test_login_limit_exceeded_returns_429(client: AsyncClient, monkeypatch) -> None:
    _enable(monkeypatch, rate_limit_login_per_min=2)

    statuses = []
    for _ in range(4):
        resp = await client.post(
            "/api/auth/login", json={"username": "nobody", "password": "whatever"}
        )
        statuses.append(resp.status_code)

    assert statuses[:2] == [401, 401]
    assert 429 in statuses[2:]


async def test_chat_limit_exceeded_returns_429(
    app: FastAPI, client: AsyncClient, seeded_user: User, monkeypatch
) -> None:
    _enable(monkeypatch, rate_limit_chat_per_min=2)
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline()
    token = await _login(client, "rl-user")

    statuses = []
    for _ in range(4):
        resp = await client.post(
            "/api/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        statuses.append(resp.status_code)

    assert statuses[:2] == [200, 200]
    assert 429 in statuses[2:]

    app.dependency_overrides.pop(get_pipeline, None)


async def test_different_users_bucketed_separately(
    app: FastAPI, client: AsyncClient, seeded_user: User, second_user: User, monkeypatch
) -> None:
    _enable(monkeypatch, rate_limit_chat_per_min=1)
    app.dependency_overrides[get_pipeline] = lambda: FakePipeline()

    token_a = await _login(client, "rl-user")
    token_b = await _login(client, "rl-user-2")

    resp_a = await client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    resp_a_second = await client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    resp_b = await client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp_a.status_code == 200
    assert resp_a_second.status_code == 429  # user A exhausted their own bucket
    assert resp_b.status_code == 200  # user B has a separate bucket

    app.dependency_overrides.pop(get_pipeline, None)


async def test_disabled_flag_bypasses_limiter(client: AsyncClient, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_default_per_min", 1)

    for _ in range(10):
        resp = await client.get("/api/auth/me")
        assert resp.status_code != 429


async def test_healthz_and_readyz_exempt(client: AsyncClient, monkeypatch) -> None:
    _enable(monkeypatch, rate_limit_default_per_min=1)

    for _ in range(10):
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 200
