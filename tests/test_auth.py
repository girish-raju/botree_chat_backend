"""Tests for login and the /me endpoint."""

from __future__ import annotations

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.db.models import User

PASSWORD = "s3cret-pass"


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


async def test_login_success(client: AsyncClient, seeded_user: User) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": "rsm", "password": PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert "token" in body and body["token"]
    assert body["user"] == {
        "id": str(seeded_user.id),
        "username": "rsm",
        "display_name": "Priya - RSM South",
        "role": "RSM",
    }


async def test_login_wrong_password(client: AsyncClient, seeded_user: User) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": "rsm", "password": "wrong"}
    )
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "auth_error"


async def test_login_unknown_user(client: AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": "nobody", "password": "whatever"}
    )
    assert response.status_code == 401


async def test_me_with_valid_token(client: AsyncClient, seeded_user: User) -> None:
    login_response = await client.post(
        "/api/auth/login", json={"username": "rsm", "password": PASSWORD}
    )
    token = login_response.json()["token"]

    response = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "id": str(seeded_user.id),
        "username": "rsm",
        "display_name": "Priya - RSM South",
        "role": "RSM",
        "sf_code": "303",
        "sf_level": 300,
        "allowed_geo_col": "geo_hier3_name",
        "allowed_geo_vals": ["REGION 6"],
    }


async def test_me_without_token(client: AsyncClient) -> None:
    response = await client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_error"


async def test_me_with_garbage_token(client: AsyncClient) -> None:
    response = await client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401
