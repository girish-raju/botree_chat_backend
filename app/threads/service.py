"""Business logic for threads/messages, mirroring the frontend's
`RemoteThreadListAdapter` + message-history-row contract (see
`local-storage-adapter.tsx`). Message `content` is opaque JSON — never
inspected or parsed here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, Thread, User
from app.errors import NotFoundError

ThreadStatus = Literal["regular", "archived"]


def _parse_thread_id(thread_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(thread_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise NotFoundError("Thread not found") from exc


def _toTitle(text: str) -> str:
    """Strip + collapse whitespace, then truncate to 48 chars with an
    ellipsis, mirroring the frontend adapter's `toTitle`."""
    collapsed = " ".join(text.split())
    if len(collapsed) > 48:
        return f"{collapsed[:48].rstrip()}…"
    return collapsed


async def _get_thread_or_404(session: AsyncSession, user: User, thread_id: str) -> Thread:
    parsed_id = _parse_thread_id(thread_id)
    result = await session.execute(
        select(Thread).where(
            Thread.id == parsed_id,
            Thread.user_id == user.id,
            Thread.deleted_at.is_(None),
        )
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        raise NotFoundError("Thread not found")
    return thread


async def list_threads(
    session: AsyncSession, user: User, status: ThreadStatus | None
) -> list[Thread]:
    stmt = select(Thread).where(Thread.user_id == user.id, Thread.deleted_at.is_(None))
    if status is not None:
        stmt = stmt.where(Thread.status == status)
    stmt = stmt.order_by(Thread.updated_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_thread(session: AsyncSession, user: User) -> Thread:
    thread = Thread(id=uuid.uuid4(), user_id=user.id, status="regular")
    session.add(thread)
    await session.flush()
    return thread


async def get_thread(session: AsyncSession, user: User, thread_id: str) -> Thread:
    return await _get_thread_or_404(session, user, thread_id)


async def update_thread(
    session: AsyncSession,
    user: User,
    thread_id: str,
    *,
    title: str | None,
    status: ThreadStatus | None,
) -> None:
    thread = await _get_thread_or_404(session, user, thread_id)
    if title is not None:
        thread.title = title
    if status is not None:
        thread.status = status
    await session.flush()


async def delete_thread(session: AsyncSession, user: User, thread_id: str) -> None:
    thread = await _get_thread_or_404(session, user, thread_id)
    thread.deleted_at = datetime.now(timezone.utc)
    await session.flush()


async def set_thread_title(session: AsyncSession, user: User, thread_id: str, text: str) -> str:
    thread = await _get_thread_or_404(session, user, thread_id)
    title = _toTitle(text)
    thread.title = title
    await session.flush()
    return title


async def list_messages(
    session: AsyncSession, user: User, thread_id: str
) -> tuple[str | None, list[Message]]:
    thread = await _get_thread_or_404(session, user, thread_id)
    result = await session.execute(
        select(Message).where(Message.thread_id == thread.id).order_by(Message.created_at.asc())
    )
    return thread.head_id, list(result.scalars().all())


async def upsert_message(
    session: AsyncSession,
    user: User,
    thread_id: str,
    *,
    id: str,
    parent_id: str | None,
    format: str,
    content: Any,
    head: bool,
) -> None:
    thread = await _get_thread_or_404(session, user, thread_id)

    existing = await session.get(Message, id)
    if existing is not None and existing.thread_id != thread.id:
        # Message ids are client-generated and globally unique; a same-id row in
        # another thread must never be silently "moved" (cross-tenant hijack).
        raise NotFoundError("Message not found in this thread")
    if existing is not None:
        existing.parent_id = parent_id
        existing.format = format
        existing.content = content
    else:
        session.add(
            Message(
                id=id,
                thread_id=thread.id,
                parent_id=parent_id,
                format=format,
                content=content,
            )
        )

    if head:
        thread.head_id = id
    thread.updated_at = datetime.now(timezone.utc)
    await session.flush()


async def delete_messages(
    session: AsyncSession, user: User, thread_id: str, ids: list[str]
) -> None:
    thread = await _get_thread_or_404(session, user, thread_id)
    if not ids:
        return
    await session.execute(
        delete(Message).where(Message.thread_id == thread.id, Message.id.in_(ids))
    )
    if thread.head_id is not None and thread.head_id in ids:
        thread.head_id = None
    await session.flush()


__all__ = [
    "list_threads",
    "create_thread",
    "get_thread",
    "update_thread",
    "delete_thread",
    "set_thread_title",
    "list_messages",
    "upsert_message",
    "delete_messages",
]
