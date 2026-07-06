"""Tests for the threads/messages API (Phase 3)."""

from __future__ import annotations

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.db.models import User

PASSWORD = "s3cret-pass"


async def _make_user_and_token(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    username: str,
) -> tuple[User, str]:
    async with db_sessionmaker() as session:
        user = User(
            username=username,
            password_hash=hash_password(PASSWORD),
            display_name=username,
            role="RSM",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    response = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200
    token = response.json()["token"]
    return user, token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def user_a(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> tuple[User, str]:
    return await _make_user_and_token(client, db_sessionmaker, "user-a")


@pytest_asyncio.fixture
async def user_b(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> tuple[User, str]:
    return await _make_user_and_token(client, db_sessionmaker, "user-b")


# --- create / list / fetch -------------------------------------------------


async def test_create_list_fetch_thread(
    client: AsyncClient, user_a: tuple[User, str]
) -> None:
    _, token = user_a

    create_resp = await client.post("/api/threads", json={}, headers=_auth(token))
    assert create_resp.status_code == 201
    body = create_resp.json()
    remote_id = body["remoteId"]
    assert body["externalId"] is None
    assert remote_id

    list_resp = await client.get("/api/threads", headers=_auth(token))
    assert list_resp.status_code == 200
    threads = list_resp.json()["threads"]
    assert threads == [{"remoteId": remote_id, "status": "regular", "title": None}]

    fetch_resp = await client.get(f"/api/threads/{remote_id}", headers=_auth(token))
    assert fetch_resp.status_code == 200
    assert fetch_resp.json() == {"remoteId": remote_id, "status": "regular", "title": None}


async def test_create_ignores_client_thread_id(
    client: AsyncClient, user_a: tuple[User, str]
) -> None:
    _, token = user_a
    resp = await client.post(
        "/api/threads", json={"threadId": "client-supplied-id"}, headers=_auth(token)
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["remoteId"] != "client-supplied-id"
    assert body["externalId"] is None


# --- status filter / archive ------------------------------------------------


async def test_status_filter_and_archive_unarchive(
    client: AsyncClient, user_a: tuple[User, str]
) -> None:
    _, token = user_a
    regular_id = (
        await client.post("/api/threads", json={}, headers=_auth(token))
    ).json()["remoteId"]
    archived_id = (
        await client.post("/api/threads", json={}, headers=_auth(token))
    ).json()["remoteId"]

    patch_resp = await client.patch(
        f"/api/threads/{archived_id}", json={"status": "archived"}, headers=_auth(token)
    )
    assert patch_resp.status_code == 204

    all_resp = await client.get("/api/threads", headers=_auth(token))
    all_ids = {t["remoteId"] for t in all_resp.json()["threads"]}
    assert all_ids == {regular_id, archived_id}

    regular_resp = await client.get(
        "/api/threads", params={"status": "regular"}, headers=_auth(token)
    )
    regular_ids = {t["remoteId"] for t in regular_resp.json()["threads"]}
    assert regular_ids == {regular_id}

    archived_resp = await client.get(
        "/api/threads", params={"status": "archived"}, headers=_auth(token)
    )
    archived_ids = {t["remoteId"] for t in archived_resp.json()["threads"]}
    assert archived_ids == {archived_id}

    unarchive_resp = await client.patch(
        f"/api/threads/{archived_id}", json={"status": "regular"}, headers=_auth(token)
    )
    assert unarchive_resp.status_code == 204
    fetched = await client.get(f"/api/threads/{archived_id}", headers=_auth(token))
    assert fetched.json()["status"] == "regular"


# --- rename / title derivation ---------------------------------------------


async def test_rename_via_patch(client: AsyncClient, user_a: tuple[User, str]) -> None:
    _, token = user_a
    remote_id = (
        await client.post("/api/threads", json={}, headers=_auth(token))
    ).json()["remoteId"]

    patch_resp = await client.patch(
        f"/api/threads/{remote_id}", json={"title": "My Thread"}, headers=_auth(token)
    )
    assert patch_resp.status_code == 204

    fetched = await client.get(f"/api/threads/{remote_id}", headers=_auth(token))
    assert fetched.json()["title"] == "My Thread"


async def test_title_derivation_short_text(
    client: AsyncClient, user_a: tuple[User, str]
) -> None:
    _, token = user_a
    remote_id = (
        await client.post("/api/threads", json={}, headers=_auth(token))
    ).json()["remoteId"]

    resp = await client.post(
        f"/api/threads/{remote_id}/title", json={"text": "hello world"}, headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.json() == {"title": "hello world"}


async def test_title_derivation_truncates_long_text(
    client: AsyncClient, user_a: tuple[User, str]
) -> None:
    _, token = user_a
    remote_id = (
        await client.post("/api/threads", json={}, headers=_auth(token))
    ).json()["remoteId"]

    long_text = "x" * 60
    resp = await client.post(
        f"/api/threads/{remote_id}/title", json={"text": long_text}, headers=_auth(token)
    )
    assert resp.status_code == 200
    title = resp.json()["title"]
    assert title == ("x" * 48) + "…"


async def test_title_derivation_collapses_whitespace(
    client: AsyncClient, user_a: tuple[User, str]
) -> None:
    _, token = user_a
    remote_id = (
        await client.post("/api/threads", json={}, headers=_auth(token))
    ).json()["remoteId"]

    resp = await client.post(
        f"/api/threads/{remote_id}/title",
        json={"text": "  hello   \n  world  "},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json() == {"title": "hello world"}


# --- soft delete -------------------------------------------------------------


async def test_soft_delete_removes_from_list_and_404s(
    client: AsyncClient, user_a: tuple[User, str]
) -> None:
    _, token = user_a
    remote_id = (
        await client.post("/api/threads", json={}, headers=_auth(token))
    ).json()["remoteId"]

    delete_resp = await client.delete(f"/api/threads/{remote_id}", headers=_auth(token))
    assert delete_resp.status_code == 204

    list_resp = await client.get("/api/threads", headers=_auth(token))
    assert list_resp.json()["threads"] == []

    fetch_resp = await client.get(f"/api/threads/{remote_id}", headers=_auth(token))
    assert fetch_resp.status_code == 404
    assert fetch_resp.json()["error"]["code"] == "not_found"


# --- tenant isolation ---------------------------------------------------------


async def test_tenant_isolation(
    client: AsyncClient, user_a: tuple[User, str], user_b: tuple[User, str]
) -> None:
    _, token_a = user_a
    _, token_b = user_b

    remote_id = (
        await client.post("/api/threads", json={}, headers=_auth(token_a))
    ).json()["remoteId"]

    fetch_resp = await client.get(f"/api/threads/{remote_id}", headers=_auth(token_b))
    assert fetch_resp.status_code == 404

    patch_resp = await client.patch(
        f"/api/threads/{remote_id}", json={"title": "hijack"}, headers=_auth(token_b)
    )
    assert patch_resp.status_code == 404

    delete_resp = await client.delete(f"/api/threads/{remote_id}", headers=_auth(token_b))
    assert delete_resp.status_code == 404

    messages_get_resp = await client.get(
        f"/api/threads/{remote_id}/messages", headers=_auth(token_b)
    )
    assert messages_get_resp.status_code == 404

    messages_post_resp = await client.post(
        f"/api/threads/{remote_id}/messages",
        json={"id": "m1", "parent_id": None, "format": "aui/v6", "content": {}},
        headers=_auth(token_b),
    )
    assert messages_post_resp.status_code == 404

    messages_delete_resp = await client.request(
        "DELETE",
        f"/api/threads/{remote_id}/messages",
        json={"ids": ["m1"]},
        headers=_auth(token_b),
    )
    assert messages_delete_resp.status_code == 404

    title_resp = await client.post(
        f"/api/threads/{remote_id}/title", json={"text": "hijack"}, headers=_auth(token_b)
    )
    assert title_resp.status_code == 404

    # Sanity: user A can still access their own thread.
    own_fetch = await client.get(f"/api/threads/{remote_id}", headers=_auth(token_a))
    assert own_fetch.status_code == 200

    # user_a's thread must not leak into user_b's list.
    b_list = await client.get("/api/threads", headers=_auth(token_b))
    assert b_list.json()["threads"] == []


async def test_nonexistent_thread_404s(client: AsyncClient, user_a: tuple[User, str]) -> None:
    _, token = user_a
    resp = await client.get(
        "/api/threads/00000000-0000-0000-0000-000000000000", headers=_auth(token)
    )
    assert resp.status_code == 404

    resp2 = await client.get("/api/threads/not-a-uuid", headers=_auth(token))
    assert resp2.status_code == 404


# --- messages ------------------------------------------------------------------


async def test_message_upsert_roundtrip_with_nested_content(
    client: AsyncClient, user_a: tuple[User, str]
) -> None:
    _, token = user_a
    remote_id = (
        await client.post("/api/threads", json={}, headers=_auth(token))
    ).json()["remoteId"]

    content = {
        "role": "user",
        "parts": [{"type": "text", "text": "hi"}],
        "nested": {"a": [1, 2, {"b": None, "c": True}]},
    }
    post_resp = await client.post(
        f"/api/threads/{remote_id}/messages",
        json={"id": "m1", "parent_id": None, "format": "aui/v6", "content": content},
        headers=_auth(token),
    )
    assert post_resp.status_code == 204

    get_resp = await client.get(f"/api/threads/{remote_id}/messages", headers=_auth(token))
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["headId"] == "m1"
    assert body["rows"] == [
        {"id": "m1", "parent_id": None, "format": "aui/v6", "content": content}
    ]

    # Upsert with same id updates content in place, no duplicate row.
    updated_content = {"role": "user", "parts": [{"type": "text", "text": "edited"}]}
    upsert_resp = await client.post(
        f"/api/threads/{remote_id}/messages",
        json={
            "id": "m1",
            "parent_id": None,
            "format": "aui/v6",
            "content": updated_content,
            "head": False,
        },
        headers=_auth(token),
    )
    assert upsert_resp.status_code == 204

    get_resp2 = await client.get(f"/api/threads/{remote_id}/messages", headers=_auth(token))
    rows = get_resp2.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["content"] == updated_content
    # head=False on the upsert leaves headId as previously set.
    assert get_resp2.json()["headId"] == "m1"


async def test_message_id_collision_across_threads_fails_closed(
    client: AsyncClient, user_a: tuple[User, str], user_b: tuple[User, str]
) -> None:
    """A same-id message in another thread must never be moved/overwritten,
    even across threads owned by different users (cross-tenant hijack)."""
    _, token_a = user_a
    _, token_b = user_b

    thread_a = (
        await client.post("/api/threads", json={}, headers=_auth(token_a))
    ).json()["remoteId"]
    await client.post(
        f"/api/threads/{thread_a}/messages",
        json={"id": "shared-id", "parent_id": None, "format": "aui/v6", "content": {"v": 1}},
        headers=_auth(token_a),
    )

    # User B owns their own thread but reuses A's message id.
    thread_b = (
        await client.post("/api/threads", json={}, headers=_auth(token_b))
    ).json()["remoteId"]
    hijack = await client.post(
        f"/api/threads/{thread_b}/messages",
        json={"id": "shared-id", "parent_id": None, "format": "aui/v6", "content": {"v": 2}},
        headers=_auth(token_b),
    )
    assert hijack.status_code == 404

    # A's message is untouched, B's thread has no messages.
    rows_a = (
        await client.get(f"/api/threads/{thread_a}/messages", headers=_auth(token_a))
    ).json()["rows"]
    assert rows_a[0]["content"] == {"v": 1}
    rows_b = (
        await client.get(f"/api/threads/{thread_b}/messages", headers=_auth(token_b))
    ).json()["rows"]
    assert rows_b == []


async def test_head_id_advances_and_parent_branching(
    client: AsyncClient, user_a: tuple[User, str]
) -> None:
    _, token = user_a
    remote_id = (
        await client.post("/api/threads", json={}, headers=_auth(token))
    ).json()["remoteId"]

    await client.post(
        f"/api/threads/{remote_id}/messages",
        json={"id": "m1", "parent_id": None, "format": "aui/v6", "content": {"n": 1}},
        headers=_auth(token),
    )
    await client.post(
        f"/api/threads/{remote_id}/messages",
        json={"id": "m2", "parent_id": "m1", "format": "aui/v6", "content": {"n": 2}},
        headers=_auth(token),
    )
    # Branch off m1 instead of advancing from m2.
    await client.post(
        f"/api/threads/{remote_id}/messages",
        json={"id": "m3", "parent_id": "m1", "format": "aui/v6", "content": {"n": 3}},
        headers=_auth(token),
    )

    get_resp = await client.get(f"/api/threads/{remote_id}/messages", headers=_auth(token))
    body = get_resp.json()
    assert body["headId"] == "m3"
    rows_by_id = {row["id"]: row for row in body["rows"]}
    assert rows_by_id["m2"]["parent_id"] == "m1"
    assert rows_by_id["m3"]["parent_id"] == "m1"
    # insertion order preserved
    assert [row["id"] for row in body["rows"]] == ["m1", "m2", "m3"]


async def test_delete_messages_subset_including_head_clears_head_id(
    client: AsyncClient, user_a: tuple[User, str]
) -> None:
    _, token = user_a
    remote_id = (
        await client.post("/api/threads", json={}, headers=_auth(token))
    ).json()["remoteId"]

    for mid in ("m1", "m2", "m3"):
        await client.post(
            f"/api/threads/{remote_id}/messages",
            json={"id": mid, "parent_id": None, "format": "aui/v6", "content": {}},
            headers=_auth(token),
        )
    # head is now m3

    delete_resp = await client.request(
        "DELETE",
        f"/api/threads/{remote_id}/messages",
        json={"ids": ["m2", "m3"]},
        headers=_auth(token),
    )
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/threads/{remote_id}/messages", headers=_auth(token))
    body = get_resp.json()
    assert body["headId"] is None
    assert [row["id"] for row in body["rows"]] == ["m1"]


# --- auth --------------------------------------------------------------------


async def test_unauthenticated_requests_401(client: AsyncClient) -> None:
    resp = await client.get("/api/threads")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "auth_error"

    resp2 = await client.post("/api/threads", json={})
    assert resp2.status_code == 401
