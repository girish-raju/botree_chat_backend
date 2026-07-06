"""Token-savings measurement harness for the NL->SQL cache pipeline.

Measures how much the L0 (exact) and L1 (semantic) query caches save in LLM
tokens by asking a fixed set of analytical questions against a RUNNING
backend twice (cold, then warm) plus a few paraphrases, then reading the
actual per-question token/cache-level/latency figures back out of
`sql_audit_log` (see `app/db/models.py:SqlAuditLog`) and rendering a
before/after report.

Usage (requires a running backend + Postgres + MySQL, e.g. via
`docker compose up`, and the seeded demo users — see scripts/seed_users.py):

    python scripts/token_report.py

Configurable via env:
    BACKEND_URL     default http://localhost:8000
    TEST_USER       default "so"
    TEST_PASSWORD   default "botree123"

Output:
    Prints a markdown table to stdout AND writes it to
    scripts/token_report_out.md.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
TEST_USER = os.environ.get("TEST_USER", "so")
TEST_PASSWORD = os.environ.get("TEST_PASSWORD", "botree123")

REQUEST_TIMEOUT_S = 30.0
RETRY_COUNT = 1

#: ~12 varied analytical questions, answerable against the 10-table schema
#: (see app/domain/schema_catalog.py). Kept distinct from each other so L1
#: semantic matches only happen where intended (the paraphrase set below).
QUESTION_SET: list[str] = [
    "what are the total sales today",
    "total order value this month",
    "top distributors by sales",
    "how many invoices were delivered yesterday",
    "total purchase value this month",
    "what is the coverage percentage today",
    "list top 5 products by sales value",
    "total outstanding amount for customers",
    "how many outlets were visited today",
    "total sales by product category this month",
    "which salesman has the highest sales today",
    "total route coverage plan for this week",
]

#: Paraphrases of a few QUESTION_SET entries — worded differently but
#: semantically equivalent, so they should hit the L1 semantic cache after
#: the originals have been asked once.
PARAPHRASE_SET: list[str] = [
    "what's today's total sales",
    "how much did we sell today in total",
    "give me the total order value for this month",
]


@dataclass
class QuestionRun:
    question: str
    ok: bool = False
    error: str | None = None
    duration_s: float = 0.0


@dataclass
class Report:
    cold: list[QuestionRun] = field(default_factory=list)
    warm: list[QuestionRun] = field(default_factory=list)
    paraphrase: list[QuestionRun] = field(default_factory=list)


async def login(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{BACKEND_URL}/api/auth/login",
        json={"username": TEST_USER, "password": TEST_PASSWORD},
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def _chat_body(question: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "parts": [{"type": "text", "text": question}]}]}


async def ask(client: httpx.AsyncClient, token: str, question: str) -> QuestionRun:
    """POST /api/chat and drain the SSE stream to completion, once, with one retry."""
    run = QuestionRun(question=question)
    started = time.monotonic()
    last_exc: Exception | None = None

    for attempt in range(RETRY_COUNT + 1):
        try:
            async with client.stream(
                "POST",
                f"{BACKEND_URL}/api/chat",
                json=_chat_body(question),
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT_S,
            ) as resp:
                resp.raise_for_status()
                saw_done = False
                async for line in resp.aiter_lines():
                    if line.strip() == "data: [DONE]":
                        saw_done = True
                        break
                run.ok = saw_done
                if not saw_done:
                    run.error = "stream ended without [DONE]"
            run.duration_s = time.monotonic() - started
            return run
        except Exception as exc:  # noqa: BLE001 - resilience is the point here
            last_exc = exc
            await asyncio.sleep(0.5)

    run.ok = False
    run.error = str(last_exc)
    run.duration_s = time.monotonic() - started
    return run


async def run_question_batch(
    client: httpx.AsyncClient, token: str, questions: list[str]
) -> list[QuestionRun]:
    results = []
    for q in questions:
        results.append(await ask(client, token, q))
    return results


@dataclass
class AuditRow:
    question: str
    tokens_in: int | None
    tokens_out: int | None
    cache_level: str | None
    duration_ms: int | None
    status: str
    created_at: Any


async def fetch_audit_rows(questions: list[str]) -> dict[str, list[AuditRow]]:
    """Return, per question text, all matching sql_audit_log rows ordered by time."""
    settings = get_settings()
    engine = create_async_engine(settings.pg_dsn)
    out: dict[str, list[AuditRow]] = {q: [] for q in questions}
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.text(
                    """
                    SELECT question, tokens_in, tokens_out, cache_level, duration_ms,
                           status, created_at
                    FROM sql_audit_log
                    WHERE question = ANY(:questions)
                    ORDER BY created_at ASC
                    """
                ),
                {"questions": questions},
            )
            for row in result:
                m = row._mapping
                q = m["question"]
                if q in out:
                    out[q].append(
                        AuditRow(
                            question=q,
                            tokens_in=m["tokens_in"],
                            tokens_out=m["tokens_out"],
                            cache_level=m["cache_level"],
                            duration_ms=m["duration_ms"],
                            status=m["status"],
                            created_at=m["created_at"],
                        )
                    )
    finally:
        await engine.dispose()
    return out


def _fmt(v: Any) -> str:
    return "-" if v is None else str(v)


def build_markdown(questions: list[str], audit_by_q: dict[str, list[AuditRow]]) -> str:
    lines: list[str] = []
    lines.append("# Token Savings Report\n")
    lines.append(f"Backend: `{BACKEND_URL}`  |  User: `{TEST_USER}`\n")
    lines.append(
        "| Question | Cold tokens (in/out) | Cold level | Warm tokens (in/out) "
        "| Warm level | Cold ms | Warm ms |"
    )
    lines.append("|---|---|---|---|---|---|---|")

    total_cold_in = total_cold_out = total_warm_in = total_warm_out = 0
    cold_durations: list[int] = []
    warm_durations: list[int] = []
    errored = 0

    for q in questions:
        rows = audit_by_q.get(q, [])
        if len(rows) < 2:
            lines.append(f"| {q} | *no data / errored* | - | - | - | - | - |")
            errored += 1 if not rows else 0
            continue

        cold, warm = rows[0], rows[1]
        if cold.status != "ok" or warm.status != "ok":
            errored += 1

        c_in, c_out = cold.tokens_in or 0, cold.tokens_out or 0
        w_in, w_out = warm.tokens_in or 0, warm.tokens_out or 0
        total_cold_in += c_in
        total_cold_out += c_out
        total_warm_in += w_in
        total_warm_out += w_out
        if cold.duration_ms is not None:
            cold_durations.append(cold.duration_ms)
        if warm.duration_ms is not None:
            warm_durations.append(warm.duration_ms)

        lines.append(
            f"| {q} | {c_in}/{c_out} | {_fmt(cold.cache_level)} | {w_in}/{w_out} "
            f"| {_fmt(warm.cache_level)} | {_fmt(cold.duration_ms)} | {_fmt(warm.duration_ms)} |"
        )

    total_cold = total_cold_in + total_cold_out
    total_warm = total_warm_in + total_warm_out
    saved = total_cold - total_warm
    pct = (saved / total_cold * 100.0) if total_cold else 0.0
    avg_cold = sum(cold_durations) / len(cold_durations) if cold_durations else 0.0
    avg_warm = sum(warm_durations) / len(warm_durations) if warm_durations else 0.0

    lines.append("")
    lines.append("## Totals\n")
    lines.append(f"- Total tokens without cache (run 1 / cold): **{total_cold}**")
    lines.append(f"- Total tokens with cache (run 2 / warm): **{total_warm}**")
    lines.append(f"- Absolute tokens saved: **{saved}**")
    lines.append(f"- Percent reduction: **{pct:.1f}%**")
    lines.append(f"- Avg cold latency: **{avg_cold:.0f} ms**")
    lines.append(f"- Avg warm latency: **{avg_warm:.0f} ms**")
    lines.append(f"- Questions with no data / error: **{errored}** / {len(questions)}")

    return "\n".join(lines) + "\n"


async def main() -> None:
    async with httpx.AsyncClient() as client:
        print(f"Logging in as '{TEST_USER}' against {BACKEND_URL} ...")
        token = await login(client)

        print(f"Run 1 (cold): asking {len(QUESTION_SET)} questions ...")
        cold_results = await run_question_batch(client, token, QUESTION_SET)
        n_cold_ok = sum(1 for r in cold_results if r.ok)
        print(f"  cold: {n_cold_ok}/{len(cold_results)} completed")

        print(f"Run 2 (warm): repeating the same {len(QUESTION_SET)} questions ...")
        warm_results = await run_question_batch(client, token, QUESTION_SET)
        n_warm_ok = sum(1 for r in warm_results if r.ok)
        print(f"  warm: {n_warm_ok}/{len(warm_results)} completed")

        print(f"Paraphrases: asking {len(PARAPHRASE_SET)} reworded questions ...")
        paraphrase_results = await run_question_batch(client, token, PARAPHRASE_SET)
        n_para_ok = sum(1 for r in paraphrase_results if r.ok)
        print(f"  paraphrase: {n_para_ok}/{len(paraphrase_results)} completed")

    print("Reading sql_audit_log for token/cache-level/latency data ...")
    audit_by_q = await fetch_audit_rows(QUESTION_SET)

    report_md = build_markdown(QUESTION_SET, audit_by_q)

    out_path = Path(__file__).resolve().parent / "token_report_out.md"
    out_path.write_text(report_md)

    print("\n" + report_md)
    print(f"Report written to {out_path}")

    if PARAPHRASE_SET:
        print("\nParaphrase check (expect L1 semantic-cache hits):")
        para_audit = await fetch_audit_rows(PARAPHRASE_SET)
        for q in PARAPHRASE_SET:
            rows = para_audit.get(q, [])
            level = rows[-1].cache_level if rows else None
            print(f"  '{q}' -> cache_level={level or '-'}")


if __name__ == "__main__":
    asyncio.run(main())
