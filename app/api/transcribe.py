"""Speech-to-text endpoint for chat voice input.

`POST /api/transcribe` accepts a multipart audio upload (webm/opus from
Chrome and Android WebView, mp4/AAC from iOS WKWebView), runs it through
Cloudflare Workers AI Whisper, and returns `{"text": ...}` for the frontend
dictation adapter to insert into the composer.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.config import get_settings
from app.db.models import User
from app.deps import get_current_user
from app.llm.whisper import CloudflareWhisper

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/transcribe", tags=["transcribe"])

UserDep = Annotated[User, Depends(get_current_user)]

# The frontend caps recordings at 60s (~1 MB AAC worst case); anything near
# this limit is not a voice query. Base64 inflates ~33%, keeping the
# Cloudflare payload comfortably within its limits.
_MAX_AUDIO_BYTES = 10 * 1024 * 1024


def get_transcriber() -> CloudflareWhisper:
    """Build a `CloudflareWhisper` from settings.

    Overridable in tests via `app.dependency_overrides[get_transcriber]`.
    """
    return CloudflareWhisper(get_settings())


TranscriberDep = Annotated[CloudflareWhisper, Depends(get_transcriber)]


@router.post("")
async def transcribe(
    user: UserDep,
    transcriber: TranscriberDep,
    audio: Annotated[UploadFile, File()],
) -> dict[str, str]:
    """Transcribe an uploaded audio clip to text."""
    data = await audio.read()
    if not data:
        return {"text": ""}
    if len(data) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio file too large")

    text = await transcriber.transcribe(data)
    logger.info(
        "transcription_done",
        user_id=user.id,
        audio_bytes=len(data),
        content_type=audio.content_type,
        text_chars=len(text),
    )
    return {"text": text}
