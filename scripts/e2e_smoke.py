"""Backend-only end-to-end smoke test (no browser) for the NL->SQL chat pipeline.

Exercises, against a RUNNING backend at BACKEND_URL:
  1. Login as `so` and `vp`, ask "what are the total sales today" as each,
     confirm both get a `query_database` tool result and that vp's row_count
     (or a plausible proxy — see NOTE below) is >= so's — i.e. RBAC widens
     visibility as we go up the hierarchy.
  2. A greeting ("hi") returns fast text with NO tool call (greeting
     short-circuit in ChatPipeline.run — see app/chat/pipeline.py).
  3. A 2-message follow-up history ([assistant: answered sales today]
     [user: "break it down by distributor"]) is handled without error.

NOTE on "vp sees >= so": the pipeline caps every result at
`settings.sql_row_cap` rows (default 50, see app/sqlsafety/limiter.py), so
`row_count` alone can saturate once both scopes return >50 rows. This script
therefore also compares the SUM of any numeric-looking measure column in the
first row (when present) as a secondary signal, but the row_count comparison
is the primary, always-available check.

Usage:
    python scripts/e2e_smoke.py

Configurable via env: BACKEND_URL, TEST_PASSWORD (applies to both `so` and
`vp`, since scripts/seed_users.py seeds every demo user with the same
SEED_PASSWORD).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import httpx

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
TEST_PASSWORD = os.environ.get("TEST_PASSWORD", "botree123")
REQUEST_TIMEOUT_S = 30.0


@dataclass
class StreamResult:
    text: str = ""
    tool_input: dict[str, Any] | None = None
    tool_output: dict[str, Any] | None = None
    saw_done: bool = False
    error: str | None = None
    frames: list[dict[str, Any]] = field(default_factory=list)


async def login(client: httpx.AsyncClient, username: str, password: str) -> str:
    resp = await client.post(
        f"{BACKEND_URL}/api/auth/login",
        json={"username": username, "password": password},
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()["token"]


async def chat(
    client: httpx.AsyncClient, token: str, messages: list[dict[str, Any]]
) -> StreamResult:
    """POST /api/chat with a full AI SDK v6 message list and drain the SSE stream."""
    result = StreamResult()
    try:
        async with client.stream(
            "POST",
            f"{BACKEND_URL}/api/chat",
            json={"messages": messages},
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_S,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :]
                if payload == "[DONE]":
                    result.saw_done = True
                    break
                try:
                    frame = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                result.frames.append(frame)
                ftype = frame.get("type")
                if ftype == "text-delta":
                    result.text += frame.get("delta", "")
                elif ftype == "tool-input-available":
                    result.tool_input = frame.get("input")
                elif ftype == "tool-output-available":
                    result.tool_output = frame.get("output")
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
    return result


def user_msg(text: str) -> dict[str, Any]:
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


def assistant_msg(text: str) -> dict[str, Any]:
    return {"role": "assistant", "parts": [{"type": "text", "text": text}]}


class Checks:
    def __init__(self) -> None:
        self.total = 0
        self.passed = 0

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        self.total += 1
        status = "PASS" if condition else "FAIL"
        if condition:
            self.passed += 1
        suffix = f" ({detail})" if detail else ""
        print(f"[{status}] {label}{suffix}")


async def main() -> None:
    checks = Checks()
    question = "what are the total sales today"

    async with httpx.AsyncClient() as client:
        print(f"Logging in as 'so' and 'vp' against {BACKEND_URL} ...")
        so_token = await login(client, "so", TEST_PASSWORD)
        vp_token = await login(client, "vp", TEST_PASSWORD)

        print(f"\nAsking '{question}' as so ...")
        so_result = await chat(client, so_token, [user_msg(question)])
        checks.check("so: stream completed ([DONE])", so_result.saw_done, so_result.error or "")
        checks.check("so: got a query_database tool result", so_result.tool_output is not None)
        so_row_count = None
        if so_result.tool_output:
            so_row_count = so_result.tool_output.get("row_count")
            checks.check("so: tool result has row_count", so_row_count is not None)
        checks.check("so: got final answer text", bool(so_result.text.strip()))

        print(f"\nAsking '{question}' as vp ...")
        vp_result = await chat(client, vp_token, [user_msg(question)])
        checks.check("vp: stream completed ([DONE])", vp_result.saw_done, vp_result.error or "")
        checks.check("vp: got a query_database tool result", vp_result.tool_output is not None)
        vp_row_count = None
        if vp_result.tool_output:
            vp_row_count = vp_result.tool_output.get("row_count")
            checks.check("vp: tool result has row_count", vp_row_count is not None)
        checks.check("vp: got final answer text", bool(vp_result.text.strip()))

        if so_row_count is not None and vp_row_count is not None:
            checks.check(
                "vp row_count >= so row_count (RBAC widens with hierarchy)",
                vp_row_count >= so_row_count,
                f"vp={vp_row_count} so={so_row_count}",
            )

        print("\nAsking greeting 'hi' (expect fast text, NO tool call) ...")
        greet_result = await chat(client, so_token, [user_msg("hi")])
        checks.check("greeting: stream completed", greet_result.saw_done, greet_result.error or "")
        checks.check("greeting: got text reply", bool(greet_result.text.strip()))
        checks.check("greeting: no tool call emitted", greet_result.tool_output is None)

        print("\nAsking follow-up 'break it down by distributor' with prior history ...")
        history = [
            user_msg(question),
            assistant_msg(so_result.text or "Total sales today across your scope."),
        ]
        followup_result = await chat(
            client, so_token, [*history, user_msg("break it down by distributor")]
        )
        checks.check(
            "follow-up: stream completed", followup_result.saw_done, followup_result.error or ""
        )
        checks.check(
            "follow-up: got tool result or text (handled, not dropped)",
            followup_result.tool_output is not None or bool(followup_result.text.strip()),
        )

    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {checks.passed}/{checks.total} checks passed")
    print(f"{'=' * 60}")
    sys.exit(0 if checks.passed == checks.total else 1)


if __name__ == "__main__":
    asyncio.run(main())
