"""Streaming chat endpoint.

`POST /api/chat` accepts the Vercel AI SDK v6 request shape
(`{messages: [...], threadId?}`), extracts the latest user message as the
question and the prior messages as conversation history, runs `ChatPipeline`,
and streams the result back as an AI SDK v6 UI message stream over SSE.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.embeddings import get_embedder
from app.cache.results import ResultCache
from app.cache.semantic import QueryCache
from app.chat.pipeline import ChatPipeline, TextDelta, ToolResult, ToolSQL
from app.chat.stream import STREAM_HEADERS, UIMessageStream
from app.config import get_settings
from app.db.models import User
from app.deps import get_current_user, get_session
from app.llm.base import Turn
from app.llm.factory import get_provider

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]


def get_pipeline() -> ChatPipeline:
    """Build a `ChatPipeline` from the process-wide singletons.

    Overridable in tests via `app.dependency_overrides[get_pipeline]`.
    """
    settings = get_settings()
    return ChatPipeline(
        settings,
        get_provider(settings),
        QueryCache(),
        ResultCache(),
        get_embedder(),
    )


PipelineDep = Annotated[ChatPipeline, Depends(get_pipeline)]


def _message_text(message: dict[str, Any]) -> str:
    """Extract the concatenated text of a message (v6 `parts` or legacy `content`)."""
    parts = message.get("parts")
    if isinstance(parts, list):
        return "".join(
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and p.get("type") == "text"
        )
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def _parse_body(body: dict[str, Any]) -> tuple[str, list[Turn], str | None]:
    """Return `(question, history, thread_id)` from an AI SDK v6 request body."""
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        messages = []

    question = ""
    last_user_idx: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_idx = i
            question = _message_text(msg)
            break

    history: list[Turn] = []
    prior = messages[:last_user_idx] if last_user_idx is not None else []
    for msg in prior:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _message_text(msg)
        if text.strip():
            history.append(Turn(role=role, text=text))

    thread_id = body.get("threadId") or body.get("thread_id")
    return question, history, thread_id


async def _event_stream(
    pipeline: ChatPipeline,
    *,
    user: User,
    thread_id: str | None,
    question: str,
    history: list[Turn],
    session: AsyncSession,
) -> AsyncIterator[str]:
    """Adapt pipeline events into encoded AI SDK v6 UI-message-stream frames."""
    enc = UIMessageStream()
    yield enc.start()
    try:
        async for event in pipeline.run(
            user=user,
            thread_id=thread_id,
            question=question,
            history=history,
            session=session,
        ):
            if isinstance(event, TextDelta):
                yield enc.text_delta(event.text)
            elif isinstance(event, ToolSQL):
                yield enc.tool_input(event.sql)
            elif isinstance(event, ToolResult):
                yield enc.tool_output(event.payload)
            # Done needs no frame of its own; finish() is emitted below.
    except asyncio.CancelledError:  # client disconnected mid-stream
        logger.info("chat_stream_cancelled")
        raise
    except Exception:  # pragma: no cover - pipeline already handles its errors
        logger.exception("chat_stream_error")
    yield enc.finish()


@router.post("")
async def chat(
    request: Request,
    user: UserDep,
    session: SessionDep,
    pipeline: PipelineDep,
) -> StreamingResponse:
    """Stream an answer to the user's latest message."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    question, history, thread_id = _parse_body(body)

    generator = _event_stream(
        pipeline,
        user=user,
        thread_id=thread_id,
        question=question,
        history=history,
        session=session,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )


__all__ = ["router", "get_pipeline"]
