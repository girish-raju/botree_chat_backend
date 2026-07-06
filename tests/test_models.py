"""Tests exercising the ORM models directly against the SQLite test DB."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Message, SqlAuditLog, Thread, User


async def test_thread_and_messages_roundtrip(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        user = User(
            username="bm",
            password_hash="hashed",
            display_name="Suresh - BM Chennai",
            role="BM",
            sf_code="414",
            sf_level=400,
            allowed_geo_col="geo_hier4_name",
            allowed_geo_vals=["TAMILNADU STATE"],
        )
        session.add(user)
        await session.flush()

        thread = Thread(id=uuid.uuid4(), user_id=user.id, title="Sales this quarter")
        session.add(thread)
        await session.flush()

        message = Message(
            id="msg-1",
            thread_id=thread.id,
            parent_id=None,
            format="text",
            content={"role": "user", "text": "How many orders last week?"},
        )
        session.add(message)
        await session.commit()

    async with db_sessionmaker() as session:
        result = await session.execute(select(Thread).where(Thread.user_id == user.id))
        fetched_thread = result.scalar_one()
        assert fetched_thread.title == "Sales this quarter"
        assert fetched_thread.status == "regular"

        result = await session.execute(
            select(Message).where(Message.thread_id == fetched_thread.id)
        )
        fetched_message = result.scalar_one()
        assert fetched_message.content == {
            "role": "user",
            "text": "How many orders last week?",
        }


async def test_sql_audit_log_insert(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as session:
        entry = SqlAuditLog(
            question="How many distributors in Chennai?",
            generated_sql="SELECT COUNT(*) FROM distributor_t WHERE ...",
            final_sql="SELECT COUNT(*) FROM distributor_t WHERE ... LIMIT 50",
            cache_level="llm",
            row_count=1,
            duration_ms=120,
            status="ok",
        )
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        assert entry.id is not None

    async with db_sessionmaker() as session:
        result = await session.execute(
            select(SqlAuditLog).where(SqlAuditLog.question.like("How many distributors%"))
        )
        fetched = result.scalar_one()
        assert fetched.status == "ok"
        assert fetched.cache_level == "llm"
