"""Thread/message endpoints — backend replacement for the frontend's
localStorage-backed `RemoteThreadListAdapter`. All routes require
authentication and are scoped to the current user; a foreign or missing
thread always yields 404 (never 403), so callers can't distinguish
"not yours" from "doesn't exist".
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.deps import get_current_user, get_session
from app.threads import service
from app.threads.pdf import render_thread_pdf, report_filename
from app.threads.service import ThreadStatus

router = APIRouter(prefix="/api/threads", tags=["threads"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]


class ThreadOut(BaseModel):
    remoteId: str
    status: str
    title: str | None


class ThreadListOut(BaseModel):
    threads: list[ThreadOut]


class ThreadCreateRequest(BaseModel):
    threadId: str | None = None


class ThreadCreateResponse(BaseModel):
    remoteId: str
    externalId: str | None = None


class ThreadPatchRequest(BaseModel):
    title: str | None = None
    status: ThreadStatus | None = None


class ThreadTitleRequest(BaseModel):
    text: str


class ThreadTitleResponse(BaseModel):
    title: str


class MessageRow(BaseModel):
    id: str
    parent_id: str | None
    format: str
    content: Any


class MessagesOut(BaseModel):
    headId: str | None
    rows: list[MessageRow]


class MessageUpsertRequest(BaseModel):
    id: str
    parent_id: str | None = None
    format: str
    content: Any
    head: bool = True


class MessageDeleteRequest(BaseModel):
    ids: list[str]


def _to_thread_out(thread) -> ThreadOut:  # type: ignore[no-untyped-def]
    return ThreadOut(remoteId=str(thread.id), status=thread.status, title=thread.title)


@router.get("")
async def list_threads(
    session: SessionDep,
    user: UserDep,
    status_filter: Annotated[ThreadStatus | None, Query(alias="status")] = None,
) -> ThreadListOut:
    threads = await service.list_threads(session, user, status_filter)
    return ThreadListOut(threads=[_to_thread_out(t) for t in threads])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_thread(
    body: ThreadCreateRequest,
    session: SessionDep,
    user: UserDep,
) -> ThreadCreateResponse:
    thread = await service.create_thread(session, user)
    return ThreadCreateResponse(remoteId=str(thread.id), externalId=None)


@router.get("/{thread_id}")
async def get_thread(thread_id: str, session: SessionDep, user: UserDep) -> ThreadOut:
    thread = await service.get_thread(session, user, thread_id)
    return _to_thread_out(thread)


@router.patch("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def patch_thread(
    thread_id: str, body: ThreadPatchRequest, session: SessionDep, user: UserDep
) -> None:
    await service.update_thread(
        session, user, thread_id, title=body.title, status=body.status
    )


@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(thread_id: str, session: SessionDep, user: UserDep) -> None:
    await service.delete_thread(session, user, thread_id)


@router.post("/{thread_id}/title")
async def post_thread_title(
    thread_id: str, body: ThreadTitleRequest, session: SessionDep, user: UserDep
) -> ThreadTitleResponse:
    title = await service.set_thread_title(session, user, thread_id, body.text)
    return ThreadTitleResponse(title=title)


@router.get("/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str, session: SessionDep, user: UserDep
) -> MessagesOut:
    head_id, messages = await service.list_messages(session, user, thread_id)
    return MessagesOut(
        headId=head_id,
        rows=[
            MessageRow(id=m.id, parent_id=m.parent_id, format=m.format, content=m.content)
            for m in messages
        ],
    )


@router.get("/{thread_id}/export/pdf")
async def export_thread_pdf(
    thread_id: str, session: SessionDep, user: UserDep
) -> Response:
    """Download the thread as a branded, insights-only PDF report.

    Ownership is enforced by the service layer (foreign/missing → 404).
    PDF assembly is CPU-bound, so it runs in the threadpool.
    """
    thread = await service.get_thread(session, user, thread_id)
    head_id, messages = await service.list_messages(session, user, thread_id)
    pdf_bytes = await run_in_threadpool(
        lambda: render_thread_pdf(head_id=head_id, messages=messages, user=user)
    )
    filename = report_filename(thread.title)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{thread_id}/messages", status_code=status.HTTP_204_NO_CONTENT)
async def post_thread_message(
    thread_id: str, body: MessageUpsertRequest, session: SessionDep, user: UserDep
) -> None:
    await service.upsert_message(
        session,
        user,
        thread_id,
        id=body.id,
        parent_id=body.parent_id,
        format=body.format,
        content=body.content,
        head=body.head,
    )


@router.delete("/{thread_id}/messages", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread_messages(
    thread_id: str, body: MessageDeleteRequest, session: SessionDep, user: UserDep
) -> None:
    await service.delete_messages(session, user, thread_id, body.ids)


__all__ = ["router"]
