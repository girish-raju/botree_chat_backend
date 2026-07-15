"""Tests for the per-request LLM usage tally (`app/llm/usage.py`)."""

from __future__ import annotations

import asyncio
import contextvars

from app.llm.usage import (
    UsageTally,
    current_tally,
    record_usage,
    start_tally,
    usage_from_dict,
)


def _in_fresh_context(fn):
    """Run `fn` in a copied context so tally state never leaks between tests."""
    return contextvars.copy_context().run(fn)


def test_record_usage_without_tally_is_noop():
    def scenario():
        record_usage(10, 20)  # must not raise
        tally = start_tally()
        assert (tally.tokens_in, tally.tokens_out) == (0, 0)

    _in_fresh_context(scenario)


def test_start_tally_resets_to_fresh():
    def scenario():
        start_tally()
        record_usage(5, 7)
        second = start_tally()
        assert (second.tokens_in, second.tokens_out) == (0, 0)
        assert current_tally() is second

    _in_fresh_context(scenario)


def test_record_usage_accumulates():
    def scenario():
        tally = start_tally()
        record_usage(3, 4)
        record_usage(10, 0)
        record_usage(0, 6)
        assert (tally.tokens_in, tally.tokens_out) == (13, 10)

    _in_fresh_context(scenario)


def test_record_usage_coerces_none():
    def scenario():
        tally = start_tally()
        record_usage(None, None)  # type: ignore[arg-type]
        assert (tally.tokens_in, tally.tokens_out) == (0, 0)

    _in_fresh_context(scenario)


def test_usage_from_dict_openai_shape():
    assert usage_from_dict({"prompt_tokens": 12, "completion_tokens": 34}) == (12, 34)


def test_usage_from_dict_anthropic_shape():
    assert usage_from_dict({"input_tokens": 5, "output_tokens": 9}) == (5, 9)


def test_usage_from_dict_missing_or_bad():
    assert usage_from_dict({}) == (0, 0)
    assert usage_from_dict(None) == (0, 0)
    assert usage_from_dict("usage") == (0, 0)
    assert usage_from_dict({"prompt_tokens": "abc", "completion_tokens": 1}) == (0, 0)


async def test_wait_for_child_task_mutates_parent_tally():
    """`suggest_followups` runs inside `asyncio.wait_for`; its recording must
    land in the caller's tally (3.12 runs inline; 3.11 copies the context but
    shares the same mutable UsageTally object)."""
    tally = start_tally()

    async def child():
        record_usage(11, 22)

    await asyncio.wait_for(child(), timeout=5)
    assert (tally.tokens_in, tally.tokens_out) == (11, 22)


async def test_concurrent_tasks_are_isolated():
    """Two concurrent request-like tasks each get their own tally."""

    async def turn(tokens_in: int, tokens_out: int) -> UsageTally:
        tally = start_tally()
        await asyncio.sleep(0)
        record_usage(tokens_in, tokens_out)
        await asyncio.sleep(0)
        return tally

    t1, t2 = await asyncio.gather(
        asyncio.create_task(turn(1, 2)), asyncio.create_task(turn(100, 200))
    )
    assert (t1.tokens_in, t1.tokens_out) == (1, 2)
    assert (t2.tokens_in, t2.tokens_out) == (100, 200)
