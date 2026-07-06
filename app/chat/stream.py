"""AI SDK v6 "UI message stream" SSE encoder.

The frontend uses the Vercel AI SDK v6 + assistant-ui, which consumes the
documented "UI message stream" protocol over Server-Sent Events. The optional
`assistant-stream` Python package is NOT installed in this environment, so this
module implements the (stable, documented) wire format directly.

Each frame is emitted as an SSE `data: <json>\\n\\n` line. The stream is opened
with `start()` (emitting `start` + `start-step`), text is delivered as
`text-start` / `text-delta` / `text-end`, a `query_database` tool call as
`tool-input-start` / `tool-input-available` / `tool-output-available`, and the
stream is closed with `finish()` (emitting `finish-step` / `finish` and the SSE
terminator `data: [DONE]`).

Responses must also carry the `x-vercel-ai-ui-message-stream: v1` header (see
`STREAM_HEADERS`) alongside `content-type: text/event-stream`.
"""

from __future__ import annotations

import json
from typing import Any

#: HTTP headers a streaming chat response must carry for the frontend to parse
#: it as a v6 UI message stream.
STREAM_HEADERS: dict[str, str] = {"x-vercel-ai-ui-message-stream": "v1"}

_TOOL_NAME = "query_database"


class UIMessageStream:
    """Stateful encoder producing AI SDK v6 UI-message-stream SSE frames.

    Every method returns the already-encoded SSE string (possibly several
    frames concatenated) ready to be yielded from a streaming response body.
    Text and tool-call ids are allocated internally; `text_delta` opens a text
    block on demand, and `tool_input` / `finish` close any open text block
    first, so callers can drive it directly from a flat event stream without
    tracking block state themselves.
    """

    def __init__(self) -> None:
        self._text_n = -1
        self._tool_n = -1
        self._current_text_id: str | None = None
        self._current_tool_id: str | None = None

    @staticmethod
    def _frame(obj: dict[str, Any]) -> str:
        return f"data: {json.dumps(obj, separators=(',', ':'))}\n\n"

    def start(self) -> str:
        """Open the stream: `start` then `start-step`."""
        return self._frame({"type": "start"}) + self._frame({"type": "start-step"})

    def text_start(self) -> str:
        """Begin a new text block, allocating a fresh id."""
        self._text_n += 1
        self._current_text_id = f"t{self._text_n}"
        return self._frame({"type": "text-start", "id": self._current_text_id})

    def text_delta(self, delta: str) -> str:
        """Emit a text delta, opening a text block first if none is open.

        The AI SDK v6 wire schema requires `delta` to be a string; some LLM
        streaming APIs occasionally hand back a numeric token (e.g. `273`).
        Coerce here so a non-string delta can never produce an invalid frame
        that the frontend rejects.
        """
        if not isinstance(delta, str):
            delta = str(delta)
        prefix = ""
        if self._current_text_id is None:
            prefix = self.text_start()
        return prefix + self._frame(
            {"type": "text-delta", "id": self._current_text_id, "delta": delta}
        )

    def text_end(self) -> str:
        """Close the current text block, if one is open (else a no-op)."""
        if self._current_text_id is None:
            return ""
        frame = self._frame({"type": "text-end", "id": self._current_text_id})
        self._current_text_id = None
        return frame

    def tool_input(self, sql: str) -> str:
        """Announce a `query_database` tool call with its SQL input."""
        out = self.text_end()
        self._tool_n += 1
        self._current_tool_id = f"q{self._tool_n}"
        out += self._frame(
            {
                "type": "tool-input-start",
                "toolCallId": self._current_tool_id,
                "toolName": _TOOL_NAME,
            }
        )
        out += self._frame(
            {
                "type": "tool-input-available",
                "toolCallId": self._current_tool_id,
                "toolName": _TOOL_NAME,
                "input": {"sql": sql},
            }
        )
        return out

    def tool_output(self, payload: dict[str, Any]) -> str:
        """Emit the result payload for the most recent tool call."""
        tool_id = self._current_tool_id or "q0"
        return self._frame(
            {"type": "tool-output-available", "toolCallId": tool_id, "output": payload}
        )

    def finish(self) -> str:
        """Close the stream: `finish-step`, `finish`, then `[DONE]`."""
        out = self.text_end()
        out += self._frame({"type": "finish-step"})
        out += self._frame({"type": "finish"})
        out += "data: [DONE]\n\n"
        return out


__all__ = ["UIMessageStream", "STREAM_HEADERS"]
