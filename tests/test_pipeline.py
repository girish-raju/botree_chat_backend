"""Tests for the chat pipeline orchestrator.

Everything external is mocked — no real LLM, MySQL, embedding model, or query
cache. The Postgres session is a real in-memory SQLite session so `SqlAuditLog`
rows are actually written and can be asserted. `get_subtree_for` is patched to
avoid the analytics layer during RBAC scoping.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat import pipeline as pipeline_module
from app.chat.answerer import NO_DATA_SENTENCE
from app.chat.pipeline import (
    ChatPipeline,
    Done,
    Suggestions,
    TextDelta,
    ToolResult,
    ToolSQL,
)
from app.config import Settings
from app.db.analytics import QueryResult
from app.db.models import SqlAuditLog, User
from app.domain.formatting import GREETING_REPLY
from app.errors import RBACError
from app.llm.base import SQLPlan
from app.llm.usage import record_usage

FIXED_VECTOR = [0.1] * 8
SAFE_SQL = "SELECT code, name FROM distributor_t"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeProvider:
    """Minimal LLMProvider stand-in with call-tracking mocks.

    Like the real providers, `generate_sql` records its plan's token usage
    into the per-turn tally (the pipeline reads the tally, not the plan).
    """

    name = "fake"

    def __init__(self, plan: SQLPlan | None = None, deltas=("Here is your answer.",)):
        self._plan = plan
        self._deltas = deltas
        self.generate_sql = AsyncMock(side_effect=self._generate_sql)
        self.stream_answer = MagicMock(side_effect=self._stream)
        self.rewrite_question = AsyncMock(side_effect=lambda history, q: q)
        self.generate_title = AsyncMock(return_value="Title")
        self.suggest_followups = AsyncMock(return_value=[])

    async def _generate_sql(self, question, history, validate=None):
        if self._plan is not None:
            record_usage(self._plan.tokens_in, self._plan.tokens_out)
        return self._plan

    async def _stream(self, question, facts, sample_rows, columns):
        for d in self._deltas:
            yield d


class FakeEmbedder:
    def __init__(self):
        self.encode = AsyncMock(return_value=FIXED_VECTOR)


class FakeAnalytics:
    def __init__(self, result: QueryResult | None = None, exc: Exception | None = None):
        self.execute_readonly = AsyncMock(side_effect=self._exec)
        self._result = result
        self._exc = exc

    async def _exec(self, sql, timeout_s=None):
        if self._exc is not None:
            raise self._exc
        return self._result


def _query_cache(exact=None, semantic=None):
    qc = MagicMock()
    qc.lookup_exact = AsyncMock(return_value=exact)
    qc.lookup_semantic = AsyncMock(return_value=semantic)
    qc.add_alias = AsyncMock()
    qc.store = AsyncMock()
    return qc


def _result_cache(cached=None):
    rc = MagicMock()
    rc.get = AsyncMock(return_value=cached)
    rc.put = AsyncMock()
    return rc


def _cache_entry(sql=SAFE_SQL, params=None):
    return SimpleNamespace(sql_template=sql, params_spec=params, temporal_intent="none")


def _query_result(columns=None, rows=None):
    columns = columns if columns is not None else ["code", "name"]
    rows = rows if rows is not None else [{"code": "D1", "name": "Acme"}]
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), duration_ms=3)


def _build_pipeline(provider, query_cache, result_cache, analytics):
    return ChatPipeline(
        Settings(),
        provider,
        query_cache,
        result_cache,
        FakeEmbedder(),
        analytics=analytics,
    )


async def _make_user(session: AsyncSession, *, role="RSM", **kw) -> User:
    defaults = dict(
        username=f"u-{role}",
        password_hash="x",
        display_name=f"{role} user",
        role=role,
        sf_code="303",
        sf_level=300,
        allowed_geo_col="geo_hier3_name",
        allowed_geo_vals=["REGION 6"],
    )
    defaults.update(kw)
    user = User(**defaults)
    session.add(user)
    await session.flush()
    return user


async def _collect(pipeline, **kwargs):
    events = []
    async for event in pipeline.run(**kwargs):
        events.append(event)
    return events


async def _audits(session: AsyncSession) -> list[SqlAuditLog]:
    result = await session.execute(select(SqlAuditLog))
    return list(result.scalars().all())


@pytest_asyncio.fixture
async def session(db_sessionmaker: async_sessionmaker[AsyncSession]):
    async with db_sessionmaker() as s:
        yield s


@pytest.fixture(autouse=True)
def _patch_subtree(monkeypatch):
    """Avoid the analytics layer during RBAC subtree resolution by default."""
    monkeypatch.setattr(
        pipeline_module, "get_subtree_for", AsyncMock(return_value={})
    )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


async def test_greeting_short_circuits(session):
    provider = FakeProvider()
    pipeline = _build_pipeline(provider, _query_cache(), _result_cache(), FakeAnalytics())
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="hello", history=[], session=session
    )

    assert TextDelta(GREETING_REPLY) in events
    assert isinstance(events[-1], Done)
    provider.generate_sql.assert_not_called()
    provider.rewrite_question.assert_not_called()

    audits = await _audits(session)
    assert len(audits) == 1
    assert audits[0].status == "ok"
    assert audits[0].cache_level is None
    assert audits[0].generated_sql is None


async def test_l0_hit_skips_llm_and_applies_rbac(session):
    provider = FakeProvider()
    qc = _query_cache(exact=_cache_entry())
    analytics = FakeAnalytics(result=_query_result())
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="list distributors",
        history=[], session=session,
    )

    provider.generate_sql.assert_not_called()
    tool_sql = [e for e in events if isinstance(e, ToolSQL)]
    tool_res = [e for e in events if isinstance(e, ToolResult)]
    assert tool_sql and tool_res
    # RBAC scoping injected the geo predicate into the executed SQL.
    assert "REGION 6" in tool_res[0].payload["sql"]
    assert tool_res[0].payload["columns"] == ["code", "name"]

    audits = await _audits(session)
    assert len(audits) == 1
    assert audits[0].cache_level == "L0"
    assert audits[0].status == "ok"


async def test_l1_hit_path(session):
    provider = FakeProvider()
    qc = _query_cache(semantic=(_cache_entry(), 0.97))
    analytics = FakeAnalytics(result=_query_result())
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="show me the distributors",
        history=[], session=session,
    )

    provider.generate_sql.assert_not_called()
    qc.lookup_semantic.assert_awaited()
    qc.add_alias.assert_awaited()
    assert any(isinstance(e, ToolResult) for e in events)

    audits = await _audits(session)
    assert audits[0].cache_level == "L1"
    assert audits[0].status == "ok"


async def test_data_answer_appends_full_result_table(session):
    """Every data-driven answer must include ALL rows as a markdown table."""
    rows = [{"code": f"D{i}", "name": f"Dist {i}"} for i in range(20)]
    qc = _query_cache(exact=_cache_entry())
    analytics = FakeAnalytics(result=_query_result(rows=rows))
    provider = FakeProvider(deltas=("Here are your distributors.",))
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="list distributors",
        history=[], session=session,
    )

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "Here are your distributors." in text
    assert "| code | name |" in text
    # All 20 rows are present — not just the 5-row LLM sample.
    for i in range(20):
        assert f"| D{i} |" in text


async def test_scalar_answer_has_no_table(session):
    """A single-value result (e.g. 'total sales this month') stays prose-only."""
    qc = _query_cache(exact=_cache_entry())
    analytics = FakeAnalytics(
        result=_query_result(columns=["total"], rows=[{"total": 12345.0}])
    )
    provider = FakeProvider(deltas=("Total sales this month: ₹12,345.",))
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="total sales this month",
        history=[], session=session,
    )

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "Total sales this month" in text
    assert "| --- |" not in text


async def test_miss_path_generates_executes_and_stores(session):
    plan = SQLPlan(sql=SAFE_SQL, mode="db", tokens_in=11, tokens_out=22)
    provider = FakeProvider(plan=plan)
    qc = _query_cache()
    analytics = FakeAnalytics(result=_query_result())
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="distributor list please",
        history=[], session=session,
    )

    provider.generate_sql.assert_awaited_once()
    analytics.execute_readonly.assert_awaited_once()
    qc.store.assert_awaited_once()  # flywheel
    provider.stream_answer.assert_called_once()
    assert any(isinstance(e, ToolSQL) for e in events)

    audits = await _audits(session)
    assert audits[0].cache_level == "llm"
    assert audits[0].status == "ok"
    assert audits[0].tokens_in == 11
    assert audits[0].tokens_out == 22
    assert audits[0].row_count == 1


async def test_general_mode_answers_without_sql(session):
    plan = SQLPlan(sql="", mode="general", answer="I can help with sales data.")
    provider = FakeProvider(plan=plan)
    pipeline = _build_pipeline(provider, _query_cache(), _result_cache(), FakeAnalytics())
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="who are you exactly here",
        history=[], session=session,
    )

    assert TextDelta("I can help with sales data.") in events
    assert not any(isinstance(e, ToolSQL) for e in events)
    audits = await _audits(session)
    assert audits[0].cache_level == "llm"
    assert audits[0].status == "ok"


async def test_blocked_sql_emits_blocked_audit(session):
    plan = SQLPlan(sql="SELECT * FROM secret_admin_t", mode="db")
    provider = FakeProvider(plan=plan)
    pipeline = _build_pipeline(provider, _query_cache(), _result_cache(), FakeAnalytics())
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="dump the admin table now",
        history=[], session=session,
    )

    assert not any(isinstance(e, ToolResult) for e in events)
    assert any(isinstance(e, TextDelta) for e in events)
    audits = await _audits(session)
    assert audits[0].status == "blocked"


async def test_empty_rows_deterministic_no_data(session):
    plan = SQLPlan(sql=SAFE_SQL, mode="db")
    provider = FakeProvider(plan=plan)
    qc = _query_cache()
    analytics = FakeAnalytics(result=_query_result(rows=[]))
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="distributors in atlantis",
        history=[], session=session,
    )

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert NO_DATA_SENTENCE in text
    provider.stream_answer.assert_not_called()
    # A query that returned 0 rows must NOT be cached — otherwise a subtly-wrong
    # query would lock in an empty answer and survive retries.
    qc.store.assert_not_awaited()
    audits = await _audits(session)
    assert audits[0].status == "ok"


async def test_analytics_error_friendly_and_audited(session):
    plan = SQLPlan(sql=SAFE_SQL, mode="db")
    provider = FakeProvider(plan=plan)
    qc = _query_cache()
    analytics = FakeAnalytics(exc=TimeoutError("query too slow"))
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="huge scan of everything",
        history=[], session=session,
    )

    assert not any(isinstance(e, ToolResult) for e in events)
    assert any(isinstance(e, TextDelta) for e in events)
    # A query that ERRORED must never be cached — a retry has to regenerate it,
    # not re-serve the broken query from cache.
    qc.store.assert_not_awaited()
    audits = await _audits(session)
    assert audits[0].status == "error"
    assert audits[0].error is not None


async def test_blocked_sql_not_cached(session):
    """A query blocked by the safety gate must not be cached either."""
    plan = SQLPlan(sql="DELETE FROM rpt_invoice_summary_t", mode="db")
    provider = FakeProvider(plan=plan)
    qc = _query_cache()
    analytics = FakeAnalytics(result=_query_result())
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    await _collect(
        pipeline, user=user, thread_id=None, question="wipe invoices",
        history=[], session=session,
    )

    qc.store.assert_not_awaited()
    audits = await _audits(session)
    assert audits[0].status == "blocked"


async def test_unrestricted_vp_skips_subtree_fetch(session, monkeypatch):
    spy = AsyncMock(return_value={})
    monkeypatch.setattr(pipeline_module, "get_subtree_for", spy)

    provider = FakeProvider()
    qc = _query_cache(exact=_cache_entry())
    analytics = FakeAnalytics(result=_query_result())
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(
        session, role="VP", sf_code="1", sf_level=100,
        allowed_geo_col=None, allowed_geo_vals=None,
    )

    events = await _collect(
        pipeline, user=user, thread_id=None, question="global sales overview",
        history=[], session=session,
    )

    spy.assert_not_called()
    tool_res = [e for e in events if isinstance(e, ToolResult)]
    assert tool_res
    # Unrestricted -> SQL passes through without a geo predicate.
    assert "REGION 6" not in tool_res[0].payload["sql"]


async def test_rbac_error_blocks(session, monkeypatch):
    monkeypatch.setattr(
        pipeline_module, "apply_scope",
        MagicMock(side_effect=RBACError("cannot scope")),
    )
    provider = FakeProvider()
    qc = _query_cache(exact=_cache_entry())
    pipeline = _build_pipeline(provider, qc, _result_cache(), FakeAnalytics())
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="list distributors",
        history=[], session=session,
    )

    assert not any(isinstance(e, ToolResult) for e in events)
    audits = await _audits(session)
    assert audits[0].status == "blocked"


async def test_result_cache_hit_skips_analytics(session):
    provider = FakeProvider()
    qc = _query_cache(exact=_cache_entry())
    cached = SimpleNamespace(columns=["code", "name"], rows=[{"code": "D9", "name": "Cached"}])
    analytics = FakeAnalytics(result=_query_result())
    pipeline = _build_pipeline(provider, qc, _result_cache(cached=cached), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="list distributors",
        history=[], session=session,
    )

    analytics.execute_readonly.assert_not_called()
    tool_res = [e for e in events if isinstance(e, ToolResult)]
    assert tool_res and tool_res[0].payload["cached"] is True
    # An L2 hit is the deepest cache level and wins the audit's cache_level.
    audits = await _audits(session)
    assert audits[0].cache_level == "result"


def test_text_delta_coerces_non_string():
    """Regression: a numeric delta (some LLM streams emit `273`) must be
    coerced to a string so the AI SDK v6 frame stays schema-valid."""
    import json as _json

    from app.chat.stream import UIMessageStream

    frame = UIMessageStream().text_delta(273)  # type: ignore[arg-type]
    # the encoded output contains a valid text-delta with a STRING delta
    payloads = [
        _json.loads(line[len("data:"):].strip())
        for line in frame.splitlines()
        if line.startswith("data:")
    ]
    delta_frame = next(p for p in payloads if p["type"] == "text-delta")
    assert delta_frame["delta"] == "273"
    assert isinstance(delta_frame["delta"], str)


# --------------------------------------------------------------------------
# Follow-up suggestions
# --------------------------------------------------------------------------


async def test_data_answer_emits_suggestions_before_done(session):
    qc = _query_cache(exact=_cache_entry())
    analytics = FakeAnalytics(result=_query_result())
    provider = FakeProvider()
    provider.suggest_followups = AsyncMock(return_value=["Month wise", "Region wise"])
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="list distributors",
        history=[], session=session,
    )

    sugg = [e for e in events if isinstance(e, Suggestions)]
    assert len(sugg) == 1
    assert sugg[0].items == ["Month wise", "Region wise"]
    # Suggestions are the last event before the terminal Done.
    assert isinstance(events[-1], Done)
    assert isinstance(events[-2], Suggestions)


async def test_suggestions_capped_at_three(session):
    qc = _query_cache(exact=_cache_entry())
    analytics = FakeAnalytics(result=_query_result())
    provider = FakeProvider()
    provider.suggest_followups = AsyncMock(
        return_value=["one", "two", "three", "four", "five"]
    )
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="list distributors",
        history=[], session=session,
    )

    sugg = [e for e in events if isinstance(e, Suggestions)]
    assert sugg[0].items == ["one", "two", "three"]


async def test_greeting_emits_no_suggestions(session):
    provider = FakeProvider()
    provider.suggest_followups = AsyncMock(return_value=["should not appear"])
    pipeline = _build_pipeline(provider, _query_cache(), _result_cache(), FakeAnalytics())
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="hello", history=[], session=session
    )

    assert not any(isinstance(e, Suggestions) for e in events)
    provider.suggest_followups.assert_not_called()


async def test_general_mode_emits_no_suggestions(session):
    plan = SQLPlan(sql="", mode="general", answer="I can help with sales data.")
    provider = FakeProvider(plan=plan)
    provider.suggest_followups = AsyncMock(return_value=["should not appear"])
    pipeline = _build_pipeline(provider, _query_cache(), _result_cache(), FakeAnalytics())
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="who are you exactly here",
        history=[], session=session,
    )

    assert not any(isinstance(e, Suggestions) for e in events)
    provider.suggest_followups.assert_not_called()


async def test_blocked_sql_emits_no_suggestions(session):
    plan = SQLPlan(sql="SELECT * FROM secret_admin_t", mode="db")
    provider = FakeProvider(plan=plan)
    provider.suggest_followups = AsyncMock(return_value=["should not appear"])
    pipeline = _build_pipeline(provider, _query_cache(), _result_cache(), FakeAnalytics())
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="dump the admin table now",
        history=[], session=session,
    )

    assert not any(isinstance(e, Suggestions) for e in events)
    provider.suggest_followups.assert_not_called()


async def test_suggestions_failure_never_breaks_the_answer(session):
    """The suggestions call is best-effort: an exception means no chips,
    while the already-streamed answer and the audit stay intact."""
    qc = _query_cache(exact=_cache_entry())
    analytics = FakeAnalytics(result=_query_result())
    provider = FakeProvider()
    provider.suggest_followups = AsyncMock(side_effect=RuntimeError("llm down"))
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="list distributors",
        history=[], session=session,
    )

    assert not any(isinstance(e, Suggestions) for e in events)
    assert any(isinstance(e, TextDelta) for e in events)
    assert isinstance(events[-1], Done)
    audits = await _audits(session)
    assert audits[0].status == "ok"


# --------------------------------------------------------------------------
# Token accounting (per-turn usage tally -> sql_audit_log.tokens_in/out)
# --------------------------------------------------------------------------


async def test_greeting_audit_tokens_are_zero(session):
    """Greeting rows are 'measured, zero' — 0/0, explicitly not NULL."""
    provider = FakeProvider()
    pipeline = _build_pipeline(provider, _query_cache(), _result_cache(), FakeAnalytics())
    user = await _make_user(session)

    await _collect(
        pipeline, user=user, thread_id=None, question="hello", history=[], session=session
    )

    audits = await _audits(session)
    assert audits[0].tokens_in == 0
    assert audits[0].tokens_out == 0


async def test_cache_hit_audit_records_answer_tokens(session):
    """L0 hits skip SQL generation but still spend tokens on the streamed
    answer — those must land in the audit row (previously NULL)."""
    provider = FakeProvider()

    async def _stream_with_usage(question, facts, sample_rows, columns):
        record_usage(7, 13)
        yield "Here is your answer."

    provider.stream_answer = MagicMock(side_effect=_stream_with_usage)
    qc = _query_cache(exact=_cache_entry())
    analytics = FakeAnalytics(result=_query_result())
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    await _collect(
        pipeline, user=user, thread_id=None, question="list distributors",
        history=[], session=session,
    )

    audits = await _audits(session)
    assert audits[0].cache_level == "L0"
    assert audits[0].tokens_in == 7
    assert audits[0].tokens_out == 13


async def test_rewrite_tokens_counted(session):
    """Tokens spent rewriting a follow-up question count toward the turn."""
    plan = SQLPlan(sql=SAFE_SQL, mode="db", tokens_in=11, tokens_out=22)
    provider = FakeProvider(plan=plan)

    async def _rewrite(history, q):
        record_usage(3, 4)
        return "standalone question about distributors"

    provider.rewrite_question = AsyncMock(side_effect=_rewrite)
    qc = _query_cache()
    analytics = FakeAnalytics(result=_query_result())
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    from app.llm.base import Turn

    await _collect(
        pipeline, user=user, thread_id=None, question="what about last month",
        history=[Turn(role="user", text="list distributors")], session=session,
    )

    audits = await _audits(session)
    assert audits[0].tokens_in == 11 + 3
    assert audits[0].tokens_out == 22 + 4


async def test_blocked_sql_audit_records_generation_tokens(session):
    """Blocked turns still consumed LLM tokens — the audit must say so."""
    plan = SQLPlan(sql="SELECT * FROM secret_admin_t", mode="db", tokens_in=11, tokens_out=22)
    provider = FakeProvider(plan=plan)
    pipeline = _build_pipeline(provider, _query_cache(), _result_cache(), FakeAnalytics())
    user = await _make_user(session)

    await _collect(
        pipeline, user=user, thread_id=None, question="dump the admin table now",
        history=[], session=session,
    )

    audits = await _audits(session)
    assert audits[0].status == "blocked"
    assert audits[0].tokens_in == 11
    assert audits[0].tokens_out == 22


async def test_db_error_audit_records_tokens(session):
    plan = SQLPlan(sql=SAFE_SQL, mode="db", tokens_in=11, tokens_out=22)
    provider = FakeProvider(plan=plan)
    analytics = FakeAnalytics(exc=TimeoutError("query too slow"))
    pipeline = _build_pipeline(provider, _query_cache(), _result_cache(), analytics)
    user = await _make_user(session)

    await _collect(
        pipeline, user=user, thread_id=None, question="huge scan of everything",
        history=[], session=session,
    )

    audits = await _audits(session)
    assert audits[0].status == "error"
    assert audits[0].tokens_in == 11
    assert audits[0].tokens_out == 22


async def test_general_mode_audit_records_tokens(session):
    plan = SQLPlan(sql="", mode="general", answer="I help with sales.",
                   tokens_in=9, tokens_out=5)
    provider = FakeProvider(plan=plan)
    pipeline = _build_pipeline(provider, _query_cache(), _result_cache(), FakeAnalytics())
    user = await _make_user(session)

    await _collect(
        pipeline, user=user, thread_id=None, question="who are you exactly here",
        history=[], session=session,
    )

    audits = await _audits(session)
    assert audits[0].cache_level == "llm"
    assert audits[0].tokens_in == 9
    assert audits[0].tokens_out == 5


async def test_suggestions_tokens_update_audit_row(session):
    """Suggestions run after the audit flush; their tokens must be topped up
    onto the SAME row (one-audit-row invariant)."""
    plan = SQLPlan(sql=SAFE_SQL, mode="db", tokens_in=11, tokens_out=22)
    provider = FakeProvider(plan=plan)

    async def _suggest(question, columns, row_count):
        record_usage(5, 6)
        return ["Month wise"]

    provider.suggest_followups = AsyncMock(side_effect=_suggest)
    qc = _query_cache()
    analytics = FakeAnalytics(result=_query_result())
    pipeline = _build_pipeline(provider, qc, _result_cache(), analytics)
    user = await _make_user(session)

    events = await _collect(
        pipeline, user=user, thread_id=None, question="distributor list please",
        history=[], session=session,
    )

    assert any(isinstance(e, Suggestions) for e in events)
    audits = await _audits(session)
    assert len(audits) == 1
    assert audits[0].tokens_in == 11 + 5
    assert audits[0].tokens_out == 22 + 6
