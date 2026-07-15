"""Per-request LLM token accounting.

A `UsageTally` is installed (via a `ContextVar`) by `ChatPipeline.run` at the
start of every chat turn; every provider records the `usage` object of every
upstream LLM call into it (`record_usage`), so the audit row captures the FULL
token spend of the turn — SQL generation, follow-up rewriting, answer
streaming, and suggestion generation alike. Calls made with no tally installed
(e.g. thread title generation, unit tests) are silently ignored.

The tally is deliberately never `reset()`: each HTTP request (and each test)
runs in its own task with its own context, so tallies cannot leak across
requests, and skipping token-based reset avoids `Token.reset()` errors when an
async generator is finalized from a different context.

Known limitation: streaming responses carry usage in the FINAL chunk, so a
stream truncated by an HTTP error loses that call's usage — an undercount,
never an overcount.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class UsageTally:
    """Mutable running total of LLM tokens spent within one chat turn."""

    tokens_in: int = 0
    tokens_out: int = 0


_tally_var: ContextVar[UsageTally | None] = ContextVar("llm_usage_tally", default=None)


def start_tally() -> UsageTally:
    """Install a fresh tally in the current context and return it."""
    tally = UsageTally()
    _tally_var.set(tally)
    return tally


def current_tally() -> UsageTally | None:
    """Return the tally installed in the current context, if any."""
    return _tally_var.get()


def record_usage(tokens_in: int, tokens_out: int) -> None:
    """Add usage to the current tally; no-op when none is installed."""
    tally = _tally_var.get()
    if tally is None:
        return
    tally.tokens_in += int(tokens_in or 0)
    tally.tokens_out += int(tokens_out or 0)


def usage_from_dict(usage: object) -> tuple[int, int]:
    """Parse an OpenAI-style usage dict -> (tokens_in, tokens_out); (0, 0) if absent/bad."""
    if not isinstance(usage, dict):
        return 0, 0
    tokens_in = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    tokens_out = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    try:
        return int(tokens_in), int(tokens_out)
    except (TypeError, ValueError):
        return 0, 0


__all__ = ["UsageTally", "start_tally", "current_tally", "record_usage", "usage_from_dict"]
