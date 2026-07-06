"""Tests for the app.cache package (semantic + result caching core).

`query_cache` uses a `pgvector` column that doesn't compile on SQLite (see
`tests/conftest.py`), so `app.cache.semantic.QueryCache` is exercised here
against a mocked `AsyncSession` rather than a real database -- these tests
cover the SQL-building/decision logic (similarity threshold + temporal veto),
not pgvector itself. `app.cache.results.ResultCache` uses a plain JSON table
that DOES exist on SQLite, so those tests run against the real `db_sessionmaker`
fixture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import sqlglot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache.embeddings import Embedder
from app.errors import UpstreamLLMError
from app.cache.normalizer import extract_temporal_intent, normalize_question
from app.cache.results import ResultCache, jsonable_rows, result_cache_key
from app.cache.semantic import QueryCache
from app.cache.templater import bind_template, parameterize_sql

# ============================================================
# normalizer: normalize_question
# ============================================================


def test_normalize_case_punct_whitespace_are_equivalent():
    a = normalize_question("What is  Total Sales?!")
    b = normalize_question("what is total sales")
    c = normalize_question("  WHAT   IS TOTAL SALES  ")
    assert a == b == c


def test_normalize_synonym_canonicalization():
    assert normalize_question("revenue today") == normalize_question("sales today")


def test_normalize_synonym_multiword_phrase_preferred_over_component_word():
    # "purchase spend" -> "purchase_value" as a unit, not "purchase" + "spend"
    assert "purchase_value" in normalize_question("purchase spend this month")
    assert "spend" not in normalize_question("purchase spend this month")


def test_normalize_strips_leading_filler():
    assert normalize_question("can you show me sales today") == normalize_question("sales today")


def test_normalize_strips_multiple_filler_phrases():
    assert (
        normalize_question("please can you tell me what is total sales today")
        == normalize_question("total sales today")
    )


def test_normalize_keeps_percent_sign():
    assert "%" in normalize_question("growth % this month")


def test_normalize_is_deterministic_and_pure():
    text = "Show me the Revenue for Tamil Nadu, please."
    assert normalize_question(text) == normalize_question(text)


# ============================================================
# normalizer: extract_temporal_intent
# ============================================================


@pytest.mark.parametrize(
    "text,expected",
    [
        ("What are sales today?", "today"),
        ("sales yesterday", "yesterday"),
        ("revenue this week", "this_week"),
        ("revenue last week", "last_week"),
        ("sales this month", "this_month"),
        ("sales MTD", "this_month"),
        ("sales month to date", "this_month"),
        ("sales last month", "last_month"),
        ("sales this quarter", "this_quarter"),
        ("sales last quarter", "last_quarter"),
        ("sales YTD", "this_year"),
        ("sales this year", "this_year"),
        ("sales last year", "last_year"),
        ("sales between 2026-01-01 and 2026-01-31", "date_range"),
        ("sales on 2026-07-06", "date_range"),
        ("total distributors", "none"),
    ],
)
def test_extract_temporal_intent_classification(text, expected):
    assert extract_temporal_intent(text) == expected


def test_extract_temporal_intent_today_vs_yesterday_differ():
    # CACHE-06 guard: two near-identical questions must not share an intent.
    assert extract_temporal_intent("sales today") != extract_temporal_intent("sales yesterday")


def test_extract_temporal_intent_case_insensitive():
    assert extract_temporal_intent("SALES TODAY") == extract_temporal_intent("sales today")


# ============================================================
# templater
# ============================================================


def test_parameterize_date_literal_recorded():
    sql = "SELECT * FROM sales WHERE order_date = '2026-07-06'"
    template, spec = parameterize_sql(sql)
    assert ":date1" in template
    assert spec["date1"] == {"type": "date", "value": "2026-07-06"}


def test_parameterize_entity_string_recorded():
    sql = "SELECT * FROM distributor_t WHERE geo_hier4_name = 'TAMILNADU STATE'"
    template, spec = parameterize_sql(sql)
    assert ":str1" in template
    assert spec["str1"] == {"type": "str", "value": "TAMILNADU STATE"}


def test_parameterize_curdate_left_untouched():
    sql = "SELECT * FROM sales WHERE order_date = CURDATE()"
    template, spec = parameterize_sql(sql)
    assert "CURDATE" in template.upper() or "CURRENT_DATE" in template.upper()
    assert spec == {}


def test_parameterize_limit_number_left_untouched():
    sql = "SELECT * FROM sales WHERE amt > 1000 LIMIT 25"
    template, spec = parameterize_sql(sql)
    assert "LIMIT 25" in template
    assert "1000" in template
    assert spec == {}


def test_parameterize_unparseable_sql_passthrough():
    bad_sql = "not ( valid sql at all((("
    template, spec = parameterize_sql(bad_sql)
    assert template == bad_sql
    assert spec == {}


def test_bind_template_round_trip_matches_original():
    original = (
        "SELECT * FROM sales WHERE order_date = '2026-07-06' "
        "AND state = 'Tamil Nadu' AND amt > 1000 LIMIT 10"
    )
    template, spec = parameterize_sql(original)
    bound = bind_template(template, spec, "sales for tamil nadu")

    normalized_original = sqlglot.parse_one(original, read="mysql").sql(dialect="mysql")
    normalized_bound = sqlglot.parse_one(bound, read="mysql").sql(dialect="mysql")
    assert normalized_bound == normalized_original


@pytest.mark.parametrize("malicious_value", ["O'Brien", "x'; DROP TABLE--"])
def test_bind_template_injection_attempt_is_safely_quoted(malicious_value):
    escaped = malicious_value.replace("'", "''")
    original = f"SELECT * FROM distributor_t WHERE distributor_name = '{escaped}'"
    template, spec = parameterize_sql(original)

    bound = bind_template(template, spec, "any question")

    # Must remain parseable (i.e. the value never broke out of its literal).
    parsed = sqlglot.parse_one(bound, read="mysql")
    assert parsed is not None
    literal = next(parsed.find_all(sqlglot.exp.Literal))
    assert literal.this == malicious_value


def test_bind_template_never_raises_on_garbage_input():
    # Should not raise even with a nonsensical template/spec.
    result = bind_template("SELECT * FROM t WHERE x = :str1 AND y = :missing", {"str1": {"type": "str", "value": "ok"}}, "q")
    assert "ok" in result


# ============================================================
# embeddings
# ============================================================


def _cf_settings():
    return SimpleNamespace(
        cloudflare_account_id="acc",
        cloudflare_api_token="tok",
        cloudflare_embedding_model="@cf/baai/bge-small-en-v1.5",
    )


def _mock_embed_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_embedder_encode_calls_cloudflare_and_normalizes():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"result": {"shape": [1, 3], "data": [[3.0, 0.0, 4.0]]}, "success": True}
        )

    embedder = Embedder(_cf_settings(), client=_mock_embed_client(handler))
    vector = await embedder.encode("sales today")

    assert vector == [0.6, 0.0, 0.8]  # L2-normalized [3, 0, 4]
    assert all(isinstance(x, float) for x in vector)
    assert seen["path"].endswith("@cf/baai/bge-small-en-v1.5")
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {"text": "sales today"}


async def test_embedder_encode_raises_on_api_failure():
    def handler(request):
        return httpx.Response(200, json={"success": False, "errors": ["boom"]})

    embedder = Embedder(_cf_settings(), client=_mock_embed_client(handler))
    with pytest.raises(UpstreamLLMError):
        await embedder.encode("x")


async def test_embedder_warmup_never_raises():
    def handler(request):
        return httpx.Response(503)

    embedder = Embedder(_cf_settings(), client=_mock_embed_client(handler))
    # warmup must swallow failures so a bad embeddings config never blocks startup
    await embedder.warmup()


# ============================================================
# semantic.py: decision logic against a mocked AsyncSession
# ============================================================


@dataclass
class _FakeEntry:
    normalized_question: str
    temporal_intent: str | None
    hit_count: int = 0
    last_hit_at: datetime | None = None
    is_valid: bool = True


def _mock_session_with_execute_result(result_obj) -> AsyncSession:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=result_obj)
    return session


async def test_lookup_exact_bumps_hit_count():
    entry = _FakeEntry(normalized_question="sales today", temporal_intent="today", hit_count=3)
    result_obj = MagicMock()
    result_obj.scalar_one_or_none = MagicMock(return_value=entry)
    session = _mock_session_with_execute_result(result_obj)

    cache = QueryCache()
    found = await cache.lookup_exact(session, "sales today")

    assert found is entry
    assert found.hit_count == 4
    assert found.last_hit_at is not None


async def test_lookup_exact_miss_returns_none():
    result_obj = MagicMock()
    result_obj.scalar_one_or_none = MagicMock(return_value=None)
    session = _mock_session_with_execute_result(result_obj)

    cache = QueryCache()
    found = await cache.lookup_exact(session, "nonexistent question")

    assert found is None


async def test_lookup_semantic_accepts_when_similarity_and_temporal_match():
    entry = _FakeEntry(normalized_question="sales today", temporal_intent="today")
    result_obj = MagicMock()
    result_obj.first = MagicMock(return_value=(entry, 0.03))  # similarity = 0.97
    session = _mock_session_with_execute_result(result_obj)

    cache = QueryCache()
    hit = await cache.lookup_semantic(session, [0.1] * 384, temporal_intent="today", threshold=0.92)

    assert hit is not None
    found_entry, similarity = hit
    assert found_entry is entry
    assert similarity == pytest.approx(0.97)
    assert found_entry.hit_count == 1


async def test_lookup_semantic_rejects_below_threshold():
    entry = _FakeEntry(normalized_question="sales today", temporal_intent="today")
    result_obj = MagicMock()
    result_obj.first = MagicMock(return_value=(entry, 0.5))  # similarity = 0.5
    session = _mock_session_with_execute_result(result_obj)

    cache = QueryCache()
    hit = await cache.lookup_semantic(session, [0.1] * 384, temporal_intent="today", threshold=0.92)

    assert hit is None
    assert entry.hit_count == 0


async def test_lookup_semantic_rejects_temporal_mismatch_even_at_near_perfect_similarity():
    # The critical false-positive guard test (CACHE-06): "sales today" vs a
    # cached "sales yesterday" entry, near-identical in embedding space.
    entry = _FakeEntry(normalized_question="sales yesterday", temporal_intent="yesterday")
    result_obj = MagicMock()
    result_obj.first = MagicMock(return_value=(entry, 0.01))  # similarity = 0.99
    session = _mock_session_with_execute_result(result_obj)

    cache = QueryCache()
    hit = await cache.lookup_semantic(session, [0.1] * 384, temporal_intent="today", threshold=0.92)

    assert hit is None
    assert entry.hit_count == 0


async def test_lookup_semantic_no_rows_returns_none():
    result_obj = MagicMock()
    result_obj.first = MagicMock(return_value=None)
    session = _mock_session_with_execute_result(result_obj)

    cache = QueryCache()
    hit = await cache.lookup_semantic(session, [0.1] * 384, temporal_intent="today", threshold=0.92)

    assert hit is None


# ============================================================
# results.py
# ============================================================


def test_result_cache_key_stable_and_differs_by_fingerprint():
    sql = "SELECT * FROM sales WHERE id = 1"
    key_a = result_cache_key(sql, "fingerprint-a")
    key_b = result_cache_key(sql, "fingerprint-a")
    key_c = result_cache_key(sql, "fingerprint-b")

    assert key_a == key_b
    assert key_a != key_c


def test_result_cache_key_differs_by_sql():
    fp = "fingerprint-a"
    key_a = result_cache_key("SELECT 1", fp)
    key_b = result_cache_key("SELECT 2", fp)
    assert key_a != key_b


def test_jsonable_rows_converts_decimal_datetime_bytes():
    rows = [
        {
            "amount": Decimal("123.45"),
            "created": datetime(2026, 7, 6, 12, 0, 0),
            "raw": b"hello",
        }
    ]
    converted = jsonable_rows(rows)

    assert converted == [
        {
            "amount": 123.45,
            "created": "2026-07-06T12:00:00",
            "raw": "hello",
        }
    ]
    assert isinstance(converted[0]["amount"], float)


async def test_result_cache_put_then_get_roundtrip(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    cache = ResultCache()
    key = result_cache_key("SELECT 1", "fp")

    async with db_sessionmaker() as session:
        await cache.put(session, key, ["a"], [{"a": 1}], row_count=1, ttl_s=300)
        await session.commit()

    async with db_sessionmaker() as session:
        entry = await cache.get(session, key)
        assert entry is not None
        assert entry.columns == ["a"]
        assert entry.rows == [{"a": 1}]
        assert entry.row_count == 1


async def test_result_cache_expired_entry_treated_as_miss(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    cache = ResultCache()
    key = result_cache_key("SELECT 1", "fp")

    async with db_sessionmaker() as session:
        await cache.put(session, key, ["a"], [{"a": 1}], row_count=1, ttl_s=-10)
        await session.commit()

    async with db_sessionmaker() as session:
        entry = await cache.get(session, key)
        assert entry is None


async def test_result_cache_sweep_deletes_expired_only(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    cache = ResultCache()
    expired_key = result_cache_key("SELECT 1", "fp")
    fresh_key = result_cache_key("SELECT 2", "fp")

    async with db_sessionmaker() as session:
        await cache.put(session, expired_key, ["a"], [{"a": 1}], row_count=1, ttl_s=-10)
        await cache.put(session, fresh_key, ["a"], [{"a": 1}], row_count=1, ttl_s=300)
        await session.commit()

    async with db_sessionmaker() as session:
        deleted = await cache.sweep(session)
        await session.commit()
        assert deleted == 1

    async with db_sessionmaker() as session:
        assert await cache.get(session, fresh_key) is not None
        assert await cache.get(session, expired_key) is None
