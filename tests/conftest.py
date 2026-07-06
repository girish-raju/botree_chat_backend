"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Base
from app.deps import get_session
from app.main import create_app
from app.middleware import ratelimit as ratelimit_module


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """An in-memory SQLite engine with all tables created (except `query_cache`,
    whose `pgvector` column type doesn't compile on SQLite)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name != "query_cache"]
        await conn.run_sync(Base.metadata.create_all, tables=tables)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def db_sessionmaker(
    db_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def app(db_sessionmaker: async_sessionmaker[AsyncSession]) -> FastAPI:
    """A freshly constructed FastAPI application, wired to the test SQLite DB."""
    # Rate limiting is disabled by default so the existing suite (which fires
    # many requests per test) isn't affected; tests/test_ratelimit.py opts
    # back in explicitly via monkeypatch and resets bucket state itself.
    get_settings().rate_limit_enabled = False
    ratelimit_module.reset()

    application = create_app()

    async def _get_session_override() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    application.dependency_overrides[get_session] = _get_session_override
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An async HTTP client bound to the app via ASGI transport (no network)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
