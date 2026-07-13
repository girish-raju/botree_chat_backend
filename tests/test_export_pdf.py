"""Tests for the thread PDF export (route + renderer helpers)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.passwords import hash_password
from app.db.models import User
from app.threads.pdf import active_branch, scope_line, split_prose

PASSWORD = "s3cret-pass"


async def _make_user_and_token(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    username: str,
    **user_fields,
) -> tuple[User, str]:
    async with db_sessionmaker() as session:
        user = User(
            username=username,
            password_hash=hash_password(PASSWORD),
            display_name=username,
            role=user_fields.pop("role", "RSM"),
            **user_fields,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    response = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200
    return user, response.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def user_a(client, db_sessionmaker):
    return await _make_user_and_token(client, db_sessionmaker, "pdf-user-a")


@pytest_asyncio.fixture
async def user_b(client, db_sessionmaker):
    return await _make_user_and_token(client, db_sessionmaker, "pdf-user-b")


def _assistant_content() -> dict:
    """A realistic persisted aui/v6 assistant message: prose + appended
    markdown table in the text part, and a query_database tool part."""
    return {
        "role": "assistant",
        "parts": [
            {"type": "step-start"},
            {
                "type": "tool-query_database",
                "toolCallId": "q0",
                "state": "output-available",
                "input": {"sql": "SELECT ..."},
                "output": {
                    "sql": "SELECT d.geo_hier3_name AS Region, SUM(x) AS TotalSales ...",
                    "columns": ["Region", "TotalSales"],
                    "rows": [
                        {"Region": "Bangalore", "TotalSales": 24086657.09},
                        {"Region": "Srinagar", "TotalSales": 15600279.21},
                    ],
                    "row_count": 2,
                    "cached": False,
                },
            },
            {
                "type": "text",
                "text": (
                    "Bangalore generates the highest sales.\n\n"
                    "| Region | TotalSales |\n| --- | --- |\n"
                    "| Bangalore | ₹2,40,86,657.09 |"
                ),
            },
            {"type": "data-suggestions", "data": ["Month wise"]},
        ],
    }


async def _seed_thread(client: AsyncClient, token: str) -> str:
    resp = await client.post("/api/threads", headers=_auth(token), json={})
    assert resp.status_code == 201
    thread_id = resp.json()["remoteId"]

    for msg in (
        {
            "id": "m-user-1",
            "parent_id": None,
            "format": "aui/v6",
            "content": {
                "role": "user",
                "parts": [{"type": "text", "text": "region wise sales"}],
            },
        },
        {
            "id": "m-asst-1",
            "parent_id": "m-user-1",
            "format": "aui/v6",
            "content": _assistant_content(),
            "head": True,
        },
    ):
        resp = await client.post(
            f"/api/threads/{thread_id}/messages", headers=_auth(token), json=msg
        )
        assert resp.status_code == 204
    return thread_id


async def test_export_pdf_happy_path(client: AsyncClient, user_a):
    _, token = user_a
    thread_id = await _seed_thread(client, token)

    resp = await client.get(
        f"/api/threads/{thread_id}/export/pdf", headers=_auth(token)
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.pdf"')
    assert resp.content[:5] == b"%PDF-"


async def test_export_pdf_tenant_isolation(client: AsyncClient, user_a, user_b):
    _, token_a = user_a
    _, token_b = user_b
    thread_id = await _seed_thread(client, token_a)

    resp = await client.get(
        f"/api/threads/{thread_id}/export/pdf", headers=_auth(token_b)
    )
    assert resp.status_code == 404


async def test_export_pdf_empty_thread(client: AsyncClient, user_a):
    _, token = user_a
    resp = await client.post("/api/threads", headers=_auth(token), json={})
    thread_id = resp.json()["remoteId"]

    resp = await client.get(
        f"/api/threads/{thread_id}/export/pdf", headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.content[:5] == b"%PDF-"


async def test_export_pdf_tolerates_malformed_content(client: AsyncClient, user_a):
    _, token = user_a
    resp = await client.post("/api/threads", headers=_auth(token), json={})
    thread_id = resp.json()["remoteId"]

    for msg in (
        {"id": "bad-1", "parent_id": None, "format": "aui/v6", "content": {"v": 1}},
        {"id": "bad-2", "parent_id": "bad-1", "format": "aui/v6",
         "content": {"role": "assistant", "parts": "not-a-list"}, "head": True},
    ):
        resp = await client.post(
            f"/api/threads/{thread_id}/messages", headers=_auth(token), json=msg
        )
        assert resp.status_code == 204

    resp = await client.get(
        f"/api/threads/{thread_id}/export/pdf", headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.content[:5] == b"%PDF-"


async def test_export_pdf_requires_auth(client: AsyncClient):
    resp = await client.get("/api/threads/00000000-0000-0000-0000-000000000000/export/pdf")
    assert resp.status_code == 401


# --- renderer helpers (pure) -------------------------------------------------


def _msg(id: str, parent_id: str | None):
    return SimpleNamespace(id=id, parent_id=parent_id, content=None)


def test_active_branch_follows_head_chain_and_skips_abandoned_fork():
    root = _msg("m1", None)
    fork_a = _msg("m2a", "m1")  # abandoned edit
    fork_b = _msg("m2b", "m1")  # current branch
    tail = _msg("m3", "m2b")
    branch = active_branch("m3", [root, fork_a, fork_b, tail])
    assert [m.id for m in branch] == ["m1", "m2b", "m3"]


def test_active_branch_falls_back_without_head():
    messages = [_msg("m1", None), _msg("m2", "m1")]
    assert active_branch(None, messages) == messages
    assert active_branch("missing", messages) == messages


def test_active_branch_survives_parent_cycles():
    a = _msg("a", "b")
    b = _msg("b", "a")
    branch = active_branch("a", [a, b])
    # A parent cycle must terminate, include each message once, and keep the
    # head as the newest (last) entry.
    assert [m.id for m in branch] == ["b", "a"]


def test_split_prose_strips_appended_markdown_table():
    text = "Total sales are ₹5,20,97,837.23.\n\n| Month | TotalSales |\n| --- | --- |\n| May | 1 |"
    assert split_prose(text) == "Total sales are ₹5,20,97,837.23."
    assert split_prose("Just prose, no table.") == "Just prose, no table."
    assert split_prose("| only | table |") == ""


def test_scope_line_variants():
    vp = SimpleNamespace(
        id=1, role="VP", sf_level=100, sf_code=None,
        allowed_geo_col=None, allowed_geo_vals=None,
    )
    rsm = SimpleNamespace(
        id=2, role="RSM", sf_level=300, sf_code="303",
        allowed_geo_col="geo_hier3_name", allowed_geo_vals=["REGION 6"],
    )
    assert scope_line(vp) == "Role VP · Full access"
    assert scope_line(rsm) == "Role RSM · Region: REGION 6"
