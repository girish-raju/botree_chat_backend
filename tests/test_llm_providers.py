"""Tests for the LLM provider abstraction (`app.llm`).

Everything is mocked — no real network/API calls. The Anthropic SDK client
and httpx client are both faked with lightweight stand-ins built from
`SimpleNamespace` / `unittest.mock`, matching the duck-typed shapes the
providers actually touch (``.content``, ``.usage``, ``block.type`` /
``.name`` / ``.input`` / ``.id`` / ``.text``).
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.errors import UpstreamLLMError
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import Turn
from app.llm.bedrock_provider import BedrockProvider
from app.llm.cloudflare_provider import CloudflareProvider, parse_llm_json
from app.llm.factory import get_provider, reset_provider
from app.llm.prompts import build_static_system_block


def _settings(**overrides) -> Settings:
    base = dict(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        cloudflare_account_id="acct123",
        cloudflare_api_token="tok123",
        cloudflare_model="@cf/meta/llama-3.1-8b-instruct",
        bedrock_api_key="bedrock-test-key",
        # Pin explicitly: Settings still reads .env, and a developer's
        # BEDROCK_MODEL override would otherwise leak into assertions.
        bedrock_model="openai.gpt-oss-20b-1:0",
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_returns_anthropic_provider():
    reset_provider()
    provider = get_provider(_settings(llm_provider="anthropic"))
    assert isinstance(provider, AnthropicProvider)
    reset_provider()


def test_factory_returns_cloudflare_provider():
    reset_provider()
    provider = get_provider(_settings(llm_provider="cloudflare"))
    assert isinstance(provider, CloudflareProvider)
    reset_provider()


def test_factory_returns_bedrock_provider():
    reset_provider()
    provider = get_provider(_settings(llm_provider="bedrock"))
    assert isinstance(provider, BedrockProvider)
    reset_provider()


def test_factory_unknown_provider_raises_value_error():
    reset_provider()
    with pytest.raises(ValueError, match="anthropic"):
        get_provider(_settings(llm_provider="not-a-real-provider"))
    reset_provider()


def test_factory_singleton_cached_until_reset():
    reset_provider()
    p1 = get_provider(_settings())
    p2 = get_provider(_settings())
    assert p1 is p2
    reset_provider()
    p3 = get_provider(_settings())
    assert p3 is not p1


# ---------------------------------------------------------------------------
# Prompt static block regression
# ---------------------------------------------------------------------------


def test_static_block_is_deterministic_and_dateless():
    block1 = build_static_system_block()
    block2 = build_static_system_block()
    assert block1 == block2
    today_iso = date.today().isoformat()
    assert today_iso not in block1
    # Also guard against a "DD-MM-YYYY"-style rendering of today's date.
    assert date.today().strftime("%d-%m-%Y") not in block1


# ---------------------------------------------------------------------------
# Anthropic provider helpers
# ---------------------------------------------------------------------------


def _tool_use_block(sql: str, reasoning: str = "because", block_id: str = "tool_1"):
    return SimpleNamespace(
        type="tool_use",
        name="query_database",
        id=block_id,
        input={"sql": sql, "reasoning": reasoning},
    )


def _text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def _usage(input_tokens=10, output_tokens=5, cache_read_input_tokens=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


def _response(content, usage=None):
    return SimpleNamespace(content=content, usage=usage or _usage())


def _fake_anthropic_client(create_side_effects):
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=create_side_effects)
    return client


@pytest.mark.asyncio
async def test_anthropic_generate_sql_happy_path():
    response = _response(
        [
            _tool_use_block("SELECT 1"),
        ],
        usage=_usage(input_tokens=100, output_tokens=20, cache_read_input_tokens=50),
    )
    client = _fake_anthropic_client([response])
    provider = AnthropicProvider(_settings(), client=client)

    plan = await provider.generate_sql("how many customers?", [], validate=None)

    assert plan.mode == "db"
    assert plan.sql == "SELECT 1"
    assert plan.reasoning == "because"
    assert plan.attempts == 1
    assert plan.tokens_in == 100
    assert plan.tokens_out == 20
    assert plan.cache_read_tokens == 50


@pytest.mark.asyncio
async def test_anthropic_cache_control_on_first_system_block_only():
    response = _response([_tool_use_block("SELECT 1")])
    client = _fake_anthropic_client([response])
    provider = AnthropicProvider(_settings(), client=client)

    await provider.generate_sql("q", [], validate=None)

    _, kwargs = client.messages.create.call_args
    system = kwargs["system"]
    assert len(system) == 2
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[1]


@pytest.mark.asyncio
async def test_anthropic_validate_fail_then_pass_two_iterations():
    bad_response = _response([_tool_use_block("SELECT * FROM secret", block_id="tool_bad")])
    good_response = _response([_tool_use_block("SELECT 1 FROM allowed", block_id="tool_good")])
    client = _fake_anthropic_client([bad_response, good_response])
    provider = AnthropicProvider(_settings(), client=client)

    calls = {"n": 0}

    def validate(sql: str) -> str | None:
        calls["n"] += 1
        if "secret" in sql:
            return "table 'secret' is not allowed"
        return None

    plan = await provider.generate_sql("q", [], validate=validate)

    assert plan.attempts == 2
    assert plan.sql == "SELECT 1 FROM allowed"
    assert client.messages.create.call_count == 2

    # Second call must carry the tool_result with is_error=True back to the model.
    _, second_kwargs = client.messages.create.call_args_list[1]
    messages = second_kwargs["messages"]
    tool_result_msg = messages[-1]
    assert tool_result_msg["role"] == "user"
    tool_result_content = tool_result_msg["content"][0]
    assert tool_result_content["type"] == "tool_result"
    assert tool_result_content["is_error"] is True
    assert "not allowed" in tool_result_content["content"]


@pytest.mark.asyncio
async def test_anthropic_three_strikes_raises_upstream_error():
    responses = [_response([_tool_use_block("SELECT bad", block_id=f"tool_{i}")]) for i in range(3)]
    client = _fake_anthropic_client(responses)
    provider = AnthropicProvider(_settings(), client=client)

    def validate(sql: str) -> str | None:
        return "always invalid"

    with pytest.raises(UpstreamLLMError):
        await provider.generate_sql("q", [], validate=validate)

    assert client.messages.create.call_count == 3


@pytest.mark.asyncio
async def test_anthropic_plain_text_response_is_general_mode():
    response = _response([_text_block("Hello, I can't help with that.")])
    client = _fake_anthropic_client([response])
    provider = AnthropicProvider(_settings(), client=client)

    plan = await provider.generate_sql("hi there", [], validate=None)

    assert plan.mode == "general"
    assert plan.answer == "Hello, I can't help with that."
    assert plan.sql == ""


@pytest.mark.asyncio
async def test_anthropic_rewrite_and_title_return_trimmed_single_line():
    rewrite_response = _response([_text_block('"What was Q2 revenue in Tamil Nadu?"\n')])
    title_response = _response([_text_block("Q2 Revenue In Tamil Nadu\n")])
    client = _fake_anthropic_client([rewrite_response, title_response])
    provider = AnthropicProvider(_settings(), client=client)

    rewritten = await provider.rewrite_question(
        [Turn(role="user", text="how did TN do")], "and Q2?"
    )
    title = await provider.generate_title("A long conversation about Tamil Nadu revenue")

    assert rewritten == "What was Q2 revenue in Tamil Nadu?"
    assert "\n" not in title
    assert title == "Q2 Revenue In Tamil Nadu"


@pytest.mark.asyncio
async def test_anthropic_wraps_sdk_errors():
    import anthropic

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        side_effect=anthropic.APIConnectionError(request=MagicMock())
    )
    provider = AnthropicProvider(_settings(), client=client)

    with pytest.raises(UpstreamLLMError):
        await provider.generate_sql("q", [], validate=None)


# ---------------------------------------------------------------------------
# Cloudflare JSON repair (`parse_llm_json`)
# ---------------------------------------------------------------------------


def test_parse_llm_json_direct():
    payload = {"mode": "db", "sql": "SELECT 1", "answer": ""}
    assert parse_llm_json(json.dumps(payload)) == payload


def test_parse_llm_json_fenced_code_block():
    text = '```json\n{"mode": "db", "sql": "SELECT 1", "answer": ""}\n```'
    result = parse_llm_json(text)
    assert result == {"mode": "db", "sql": "SELECT 1", "answer": ""}


def test_parse_llm_json_prose_around_braces():
    text = 'Sure, here you go: {"mode": "db", "sql": "SELECT 1", "answer": ""} Hope that helps!'
    result = parse_llm_json(text)
    assert result == {"mode": "db", "sql": "SELECT 1", "answer": ""}


def test_parse_llm_json_trailing_comma():
    text = '{"mode": "db", "sql": "SELECT 1", "answer": "",}'
    result = parse_llm_json(text)
    assert result == {"mode": "db", "sql": "SELECT 1", "answer": ""}


def test_parse_llm_json_single_quotes():
    text = "{'mode': 'db', 'sql': 'SELECT 1', 'answer': ''}"
    result = parse_llm_json(text)
    assert result == {"mode": "db", "sql": "SELECT 1", "answer": ""}


def test_parse_llm_json_unparseable_returns_none():
    assert parse_llm_json("") is None
    assert parse_llm_json("not json at all, no braces here") is None


# ---------------------------------------------------------------------------
# Cloudflare provider
# ---------------------------------------------------------------------------


def _cf_response(body: dict) -> SimpleNamespace:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=body)
    return resp


@pytest.mark.asyncio
async def test_cloudflare_generate_sql_happy_path():
    client = MagicMock()
    body = {"result": {"response": json.dumps({"mode": "db", "sql": "SELECT 1", "answer": ""})}}
    client.post = AsyncMock(return_value=_cf_response(body))
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    plan = await provider.generate_sql("how many customers?", [], validate=None)

    assert plan.mode == "db"
    assert plan.sql == "SELECT 1"
    assert plan.attempts == 1
    assert client.post.call_count == 1


@pytest.mark.asyncio
async def test_cloudflare_response_as_chat_dict_with_content():
    """Live Llama 3.1 returns result.response as a dict {content, tool_calls},
    not a plain string — the extractor must pull `content` (regression)."""
    client = MagicMock()
    payload = json.dumps({"mode": "db", "sql": "SELECT SUM(measure_14) FROM rpt_invoice_summary_t"})
    body = {"result": {"response": {"content": payload, "tool_calls": []}}}
    client.post = AsyncMock(return_value=_cf_response(body))
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    plan = await provider.generate_sql("total sales today", [], validate=None)

    assert plan.mode == "db"
    assert plan.sql == "SELECT SUM(measure_14) FROM rpt_invoice_summary_t"


@pytest.mark.asyncio
async def test_cloudflare_populates_token_usage():
    """Token counts from result.usage must land on the SQLPlan for audit/cost
    accounting (regression — was returning 0/0)."""
    client = MagicMock()
    body = {
        "result": {
            "response": {"mode": "db", "sql": "SELECT 1"},
            "usage": {"prompt_tokens": 1936, "completion_tokens": 40},
        }
    }
    client.post = AsyncMock(return_value=_cf_response(body))
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    plan = await provider.generate_sql("total sales today", [], validate=None)

    assert plan.tokens_in == 1936
    assert plan.tokens_out == 40


@pytest.mark.asyncio
async def test_cloudflare_response_as_prealready_parsed_plan_dict():
    """Live vLLM/Workers AI with JSON response format returns result.response
    ALREADY parsed into the plan dict {mode, sql, answer} — regression for the
    actual shape observed against the Cloudflare account."""
    client = MagicMock()
    body = {
        "result": {
            "response": {
                "answer": "",
                "mode": "db",
                "sql": "SELECT SUM(inv.measure_14) FROM rpt_invoice_summary_t inv",
            }
        }
    }
    client.post = AsyncMock(return_value=_cf_response(body))
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    plan = await provider.generate_sql("total sales today", [], validate=None)

    assert plan.mode == "db"
    assert plan.sql == "SELECT SUM(inv.measure_14) FROM rpt_invoice_summary_t inv"


@pytest.mark.asyncio
async def test_cloudflare_response_as_tool_call_arguments():
    """If the JSON lands in a tool call's arguments, extract it from there."""
    client = MagicMock()
    args = json.dumps({"mode": "db", "sql": "SELECT 1"})
    body = {
        "result": {"response": {"content": "", "tool_calls": [{"function": {"arguments": args}}]}}
    }
    client.post = AsyncMock(return_value=_cf_response(body))
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    plan = await provider.generate_sql("total sales today", [], validate=None)

    assert plan.sql == "SELECT 1"


@pytest.mark.asyncio
async def test_cloudflare_validate_fail_then_one_retry_succeeds():
    bad_body = {
        "result": {
            "response": json.dumps({"mode": "db", "sql": "SELECT * FROM secret", "answer": ""})
        }
    }
    good_body = {
        "result": {
            "response": json.dumps({"mode": "db", "sql": "SELECT 1 FROM allowed", "answer": ""})
        }
    }
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_cf_response(bad_body), _cf_response(good_body)])
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    def validate(sql: str) -> str | None:
        return "table 'secret' is not allowed" if "secret" in sql else None

    plan = await provider.generate_sql("q", [], validate=validate)

    assert plan.attempts == 2
    assert plan.sql == "SELECT 1 FROM allowed"
    assert client.post.call_count == 2

    _, second_kwargs = client.post.call_args_list[1]
    second_payload = second_kwargs["json"]
    combined_text = json.dumps(second_payload)
    assert "not allowed" in combined_text


@pytest.mark.asyncio
async def test_cloudflare_both_attempts_fail_raises_upstream_error():
    bad_body = {
        "result": {
            "response": json.dumps({"mode": "db", "sql": "SELECT * FROM secret", "answer": ""})
        }
    }
    client = MagicMock()
    client.post = AsyncMock(return_value=_cf_response(bad_body))
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    def validate(sql: str) -> str | None:
        return "always invalid"

    with pytest.raises(UpstreamLLMError):
        await provider.generate_sql("q", [], validate=validate)

    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_cloudflare_rewrite_and_title_trimmed():
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=[
            _cf_response({"result": {"response": '"What about Q2?"\n'}}),
            _cf_response({"result": {"response": "Q2 Revenue Summary\n"}}),
        ]
    )
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    rewritten = await provider.rewrite_question([Turn(role="user", text="hi")], "and Q2?")
    title = await provider.generate_title("long text")

    assert rewritten == "What about Q2?"
    assert "\n" not in title
    assert title == "Q2 Revenue Summary"


class _FakeSSEResponse:
    """Minimal async-context-manager stand-in for an httpx streaming response."""

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


@pytest.mark.asyncio
async def test_cloudflare_stream_answer_concatenates_sse_deltas():
    lines = [
        'data: {"response": "The "}',
        'data: {"response": "answer "}',
        'data: {"response": "is 42."}',
        "data: [DONE]",
    ]
    client = MagicMock()
    client.stream = MagicMock(return_value=_FakeSSEResponse(lines))
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    deltas = [chunk async for chunk in provider.stream_answer("q", {"total": 42}, [], [])]

    assert "".join(deltas) == "The answer is 42."


@pytest.mark.asyncio
async def test_cloudflare_general_refusal_retries_and_forces_db():
    """Llama sometimes wrongly refuses a data question (mode=general, e.g. 'I
    need to know your zone'). Since greetings are filtered upstream, force a DB
    answer once — the retry's SQL is used."""
    refusal = {
        "result": {
            "response": json.dumps(
                {"mode": "general", "sql": "", "answer": "I cannot answer without your zone."}
            )
        }
    }
    good = {
        "result": {
            "response": json.dumps(
                {
                    "mode": "db",
                    "sql": "SELECT inv.product_name FROM rpt_invoice_summary_t inv",
                    "answer": "",
                }
            )
        }
    }
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_cf_response(refusal), _cf_response(good)])
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    plan = await provider.generate_sql("which product sold most in my zone", [], validate=None)

    assert plan.mode == "db"
    assert plan.sql == "SELECT inv.product_name FROM rpt_invoice_summary_t inv"
    assert plan.attempts == 2
    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_cloudflare_genuine_general_survives_forced_retry():
    """If it stays general even after the forced retry, accept the general
    answer (a genuinely non-DB question)."""
    g1 = {"result": {"response": json.dumps({"mode": "general", "sql": "", "answer": "Hello!"})}}
    g2 = {
        "result": {
            "response": json.dumps(
                {"mode": "general", "sql": "", "answer": "I'm a data assistant."}
            )
        }
    }
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_cf_response(g1), _cf_response(g2)])
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    plan = await provider.generate_sql("who are you", [], validate=None)

    assert plan.mode == "general"
    assert plan.answer


@pytest.mark.asyncio
async def test_cloudflare_general_question_with_empty_db_retry_returns_answer():
    """Regression (prod crash): a genuine general question ('who is the PM of
    India?') that, when forced to DB, returns mode='db' with EMPTY sql must NOT
    raise — it returns the conversational answer."""
    g1 = {
        "result": {
            "response": {
                "mode": "general",
                "sql": "",
                "answer": "The PM of India is Narendra Modi.",
            }
        }
    }
    # forced retry: model complies with mode=db but produces no SQL
    r2 = {
        "result": {
            "response": {"mode": "db", "sql": "", "answer": "Narendra Modi is the current PM."}
        }
    }
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_cf_response(g1), _cf_response(r2)])
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    def validate(sql: str) -> str | None:
        return "Empty SQL statement is not allowed." if not sql.strip() else None

    plan = await provider.generate_sql("Hi, who is the PM of India?", [], validate=validate)

    assert plan.mode == "general"
    assert plan.answer  # a conversational answer, not an error


# ---------------------------------------------------------------------------
# Bedrock provider (OpenAI-compatible chat completions, gpt-oss)
# ---------------------------------------------------------------------------


def _oai_body(content, usage: dict | None = None, reasoning: str | None = None) -> dict:
    msg: dict = {"content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return {
        "choices": [{"message": msg}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _bedrock_provider(client, **overrides) -> BedrockProvider:
    settings = _settings(llm_provider="bedrock", bedrock_region="ap-south-1", **overrides)
    return BedrockProvider(settings, client=client)


@pytest.mark.asyncio
async def test_bedrock_generate_sql_happy_path():
    client = MagicMock()
    body = _oai_body(
        json.dumps({"mode": "db", "sql": "SELECT 1", "answer": ""}),
        usage={"prompt_tokens": 1200, "completion_tokens": 42},
    )
    client.post = AsyncMock(return_value=_cf_response(body))
    provider = _bedrock_provider(client)

    plan = await provider.generate_sql("how many customers?", [], validate=None)

    assert plan.mode == "db"
    assert plan.sql == "SELECT 1"
    assert plan.attempts == 1
    assert plan.tokens_in == 1200
    assert plan.tokens_out == 42
    assert client.post.call_count == 1

    args, kwargs = client.post.call_args
    assert "bedrock-runtime.ap-south-1.amazonaws.com/openai/v1/chat/completions" in args[0]
    assert kwargs["headers"]["Authorization"] == "Bearer bedrock-test-key"
    payload = kwargs["json"]
    assert payload["model"] == "openai.gpt-oss-20b-1:0"
    assert payload["reasoning_effort"] == "low"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"].startswith("Reasoning: low")


@pytest.mark.asyncio
async def test_bedrock_strips_inline_reasoning_tags():
    """Bedrock's gpt-oss serving inlines chain-of-thought as
    <reasoning>...</reasoning> INSIDE content (observed live) — it must be
    stripped before JSON parsing / display."""
    client = MagicMock()
    content = (
        "<reasoning>Need to output strict JSON with those fields.</reasoning>"
        '{"mode": "db", "sql": "SELECT 9", "answer": ""}'
    )
    client.post = AsyncMock(return_value=_cf_response(_oai_body(content)))
    provider = _bedrock_provider(client)

    plan = await provider.generate_sql("q", [], validate=None)

    assert plan.mode == "db"
    assert plan.sql == "SELECT 9"


def test_bedrock_generates_bearer_token_from_iam_keys():
    """With no BEDROCK_API_KEY, a bearer token is derived from IAM keys via
    offline SigV4 presigning and cached."""
    provider = _bedrock_provider(
        MagicMock(),
        bedrock_api_key="",
        aws_access_key_id="AKIAFAKEFAKEFAKEFAKE",
        aws_secret_access_key="fake-secret",
    )

    auth = provider._headers["Authorization"]
    assert auth.startswith("Bearer bedrock-api-key-")
    # Cached: same token on the second call.
    assert provider._headers["Authorization"] == auth


def test_bedrock_no_credentials_raises_clear_error():
    provider = _bedrock_provider(
        MagicMock(), bedrock_api_key="", aws_access_key_id="", aws_secret_access_key=""
    )

    with pytest.raises(UpstreamLLMError, match="no Bedrock credentials"):
        _ = provider._headers


@pytest.mark.asyncio
async def test_bedrock_ignores_reasoning_content():
    """gpt-oss emits chain-of-thought in message.reasoning_content on some
    servings — the plan must come from `content` only."""
    client = MagicMock()
    body = _oai_body(
        json.dumps({"mode": "db", "sql": "SELECT 2", "answer": ""}),
        reasoning='{"mode": "general", "sql": "", "answer": "thinking junk"}',
    )
    client.post = AsyncMock(return_value=_cf_response(body))
    provider = _bedrock_provider(client)

    plan = await provider.generate_sql("q", [], validate=None)

    assert plan.mode == "db"
    assert plan.sql == "SELECT 2"


@pytest.mark.asyncio
async def test_bedrock_content_as_typed_parts_list():
    """Some OpenAI-compatible servers return content as a list of typed parts."""
    client = MagicMock()
    text = json.dumps({"mode": "db", "sql": "SELECT 3", "answer": ""})
    body = {
        "choices": [{"message": {"content": [{"type": "text", "text": text}]}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    client.post = AsyncMock(return_value=_cf_response(body))
    provider = _bedrock_provider(client)

    plan = await provider.generate_sql("q", [], validate=None)

    assert plan.sql == "SELECT 3"


@pytest.mark.asyncio
async def test_bedrock_json_repair_fenced():
    client = MagicMock()
    fenced = '```json\n{"mode": "db", "sql": "SELECT 1", "answer": ""}\n```'
    client.post = AsyncMock(return_value=_cf_response(_oai_body(fenced)))
    provider = _bedrock_provider(client)

    plan = await provider.generate_sql("q", [], validate=None)

    assert plan.mode == "db"
    assert plan.sql == "SELECT 1"


@pytest.mark.asyncio
async def test_bedrock_validate_fail_then_one_retry_succeeds():
    bad = _oai_body(json.dumps({"mode": "db", "sql": "SELECT * FROM secret", "answer": ""}))
    good = _oai_body(json.dumps({"mode": "db", "sql": "SELECT 1 FROM allowed", "answer": ""}))
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_cf_response(bad), _cf_response(good)])
    provider = _bedrock_provider(client)

    def validate(sql: str) -> str | None:
        return "table 'secret' is not allowed" if "secret" in sql else None

    plan = await provider.generate_sql("q", [], validate=validate)

    assert plan.attempts == 2
    assert plan.sql == "SELECT 1 FROM allowed"
    assert client.post.call_count == 2

    _, second_kwargs = client.post.call_args_list[1]
    assert "not allowed" in json.dumps(second_kwargs["json"])


@pytest.mark.asyncio
async def test_bedrock_both_attempts_fail_raises_upstream_error():
    bad = _oai_body(json.dumps({"mode": "db", "sql": "SELECT * FROM secret", "answer": ""}))
    client = MagicMock()
    client.post = AsyncMock(return_value=_cf_response(bad))
    provider = _bedrock_provider(client)

    def validate(sql: str) -> str | None:
        return "always invalid"

    with pytest.raises(UpstreamLLMError):
        await provider.generate_sql("q", [], validate=validate)

    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_bedrock_general_refusal_retries_and_forces_db():
    refusal = _oai_body(
        json.dumps({"mode": "general", "sql": "", "answer": "I cannot answer without your zone."})
    )
    good = _oai_body(
        json.dumps(
            {
                "mode": "db",
                "sql": "SELECT inv.product_name FROM rpt_invoice_summary_t inv",
                "answer": "",
            }
        )
    )
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_cf_response(refusal), _cf_response(good)])
    provider = _bedrock_provider(client)

    plan = await provider.generate_sql("which product sold most in my zone", [], validate=None)

    assert plan.mode == "db"
    assert plan.sql == "SELECT inv.product_name FROM rpt_invoice_summary_t inv"
    assert plan.attempts == 2


@pytest.mark.asyncio
async def test_bedrock_genuine_general_survives_forced_retry():
    g1 = _oai_body(json.dumps({"mode": "general", "sql": "", "answer": "Hello!"}))
    g2 = _oai_body(json.dumps({"mode": "general", "sql": "", "answer": "I'm a data assistant."}))
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_cf_response(g1), _cf_response(g2)])
    provider = _bedrock_provider(client)

    plan = await provider.generate_sql("who are you", [], validate=None)

    assert plan.mode == "general"
    assert plan.answer


@pytest.mark.asyncio
async def test_bedrock_reasoning_effort_rejected_drops_param_and_retries():
    """If the endpoint 400s on reasoning_effort, retry once without it."""
    bad_response = MagicMock()
    bad_response.status_code = 400
    bad_response.text = '{"error": {"message": "Unknown parameter: reasoning_effort"}}'
    error = httpx.HTTPStatusError("400", request=MagicMock(), response=bad_response)
    first = MagicMock()
    first.raise_for_status = MagicMock(side_effect=error)
    good = _cf_response(_oai_body(json.dumps({"mode": "db", "sql": "SELECT 1", "answer": ""})))

    client = MagicMock()
    client.post = AsyncMock(side_effect=[first, good])
    provider = _bedrock_provider(client)

    plan = await provider.generate_sql("q", [], validate=None)

    assert plan.sql == "SELECT 1"
    assert client.post.call_count == 2
    _, retry_kwargs = client.post.call_args_list[1]
    assert "reasoning_effort" not in retry_kwargs["json"]


@pytest.mark.asyncio
async def test_bedrock_http_error_includes_response_body():
    bad_response = MagicMock()
    bad_response.status_code = 403
    bad_response.text = '{"message": "The security token included in the request is invalid"}'
    error = httpx.HTTPStatusError("403", request=MagicMock(), response=bad_response)
    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=error)
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    provider = _bedrock_provider(client)

    with pytest.raises(UpstreamLLMError, match="security token"):
        await provider.generate_sql("q", [], validate=None)


@pytest.mark.asyncio
async def test_bedrock_stream_answer_concatenates_openai_deltas():
    lines = [
        'data: {"choices": [{"delta": {"content": "The "}}]}',
        'data: {"choices": [{"delta": {"reasoning_content": "hmm, let me think"}}]}',
        'data: {"choices": [{"delta": {"content": "answer "}}]}',
        'data: {"choices": [{"delta": {"content": "is 42."}}]}',
        'data: {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 9}}',
        "data: [DONE]",
    ]
    client = MagicMock()
    client.stream = MagicMock(return_value=_FakeSSEResponse(lines))
    provider = _bedrock_provider(client)

    deltas = [chunk async for chunk in provider.stream_answer("q", {"total": 42}, [], [])]

    assert "".join(deltas) == "The answer is 42."


@pytest.mark.asyncio
async def test_bedrock_stream_strips_inline_reasoning_even_split_across_chunks():
    """The live stream shape: chain-of-thought arrives inside content deltas as
    <reasoning>...</reasoning>, potentially split across chunk boundaries."""
    lines = [
        'data: {"choices": [{"delta": {"content": ""}}]}',
        'data: {"choices": [{"delta": {"content": "<reas"}}]}',
        'data: {"choices": [{"delta": {"content": "oning>They want 42. Easy.</reason"}}]}',
        'data: {"choices": [{"delta": {"content": "ing>The answer "}}]}',
        'data: {"choices": [{"delta": {"content": "is 42."}}]}',
        "data: [DONE]",
    ]
    client = MagicMock()
    client.stream = MagicMock(return_value=_FakeSSEResponse(lines))
    provider = _bedrock_provider(client)

    deltas = [chunk async for chunk in provider.stream_answer("q", {"total": 42}, [], [])]

    joined = "".join(deltas)
    assert joined == "The answer is 42."
    assert "reasoning" not in joined


@pytest.mark.asyncio
async def test_bedrock_stream_failure_falls_back_to_non_streaming():
    client = MagicMock()
    client.stream = MagicMock(side_effect=httpx.ConnectError("boom"))
    client.post = AsyncMock(return_value=_cf_response(_oai_body("fallback answer")))
    provider = _bedrock_provider(client)

    deltas = [chunk async for chunk in provider.stream_answer("q", {}, [], [])]

    assert deltas == ["fallback answer"]


@pytest.mark.asyncio
async def test_bedrock_rewrite_and_title_trimmed():
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=[
            _cf_response(_oai_body('"What about Q2?"\n')),
            _cf_response(_oai_body("Q2 Revenue Summary\nextra line")),
        ]
    )
    provider = _bedrock_provider(client)

    rewritten = await provider.rewrite_question([Turn(role="user", text="hi")], "and Q2?")
    title = await provider.generate_title("long text")

    assert rewritten == "What about Q2?"
    assert "\n" not in title
    assert title == "Q2 Revenue Summary"


# ---------------------------------------------------------------------------
# Follow-up suggestions
# ---------------------------------------------------------------------------


def test_parse_suggestion_list_clean_array():
    from app.llm.base import parse_suggestion_list

    assert parse_suggestion_list('["Month wise", "Region wise"]') == [
        "Month wise",
        "Region wise",
    ]


def test_parse_suggestion_list_tolerates_fences_and_prose():
    from app.llm.base import parse_suggestion_list

    text = 'Here you go:\n```json\n["Top 10 products", "Compare with last year"]\n```'
    assert parse_suggestion_list(text) == ["Top 10 products", "Compare with last year"]


def test_parse_suggestion_list_caps_dedupes_and_strips_markers():
    from app.llm.base import parse_suggestion_list

    text = '["1. Month wise", "- Month Wise", "Region wise", "Top 10", "Week wise"]'
    # marker stripped, case-insensitive dupe dropped, capped at 3
    assert parse_suggestion_list(text) == ["Month wise", "Region wise", "Top 10"]


def test_parse_suggestion_list_garbage_returns_empty():
    from app.llm.base import parse_suggestion_list

    assert parse_suggestion_list("") == []
    assert parse_suggestion_list("I have no suggestions for you.") == []
    assert parse_suggestion_list('{"mode": "db"}') == []
    assert parse_suggestion_list('[1, 2, {"a": 1}]') == []
    assert parse_suggestion_list("[unclosed") == []


@pytest.mark.asyncio
async def test_anthropic_suggest_followups_parses_array():
    response = _response([_text_block('["Month wise sales", "Region wise sales"]')])
    client = _fake_anthropic_client([response])
    provider = AnthropicProvider(_settings(), client=client)

    items = await provider.suggest_followups("total sales this year", ["TotalSales"], 1)

    assert items == ["Month wise sales", "Region wise sales"]


@pytest.mark.asyncio
async def test_cloudflare_suggest_followups_parses_array():
    client = MagicMock()
    client.post = AsyncMock(
        return_value=_cf_response({"result": {"response": '["Month wise sales"]'}})
    )
    provider = CloudflareProvider(_settings(llm_provider="cloudflare"), client=client)

    items = await provider.suggest_followups("total sales this year", ["TotalSales"], 1)

    assert items == ["Month wise sales"]


@pytest.mark.asyncio
async def test_bedrock_suggest_followups_parses_array_and_strips_reasoning():
    client = MagicMock()
    content = '<reasoning>thinking...</reasoning>["Month wise sales", "Top 10 products"]'
    client.post = AsyncMock(return_value=_cf_response(_oai_body(content)))
    provider = _bedrock_provider(client)

    items = await provider.suggest_followups("total sales this year", ["TotalSales"], 1)

    assert items == ["Month wise sales", "Top 10 products"]


@pytest.mark.asyncio
async def test_bedrock_suggest_followups_junk_reply_yields_empty():
    client = MagicMock()
    client.post = AsyncMock(return_value=_cf_response(_oai_body("no suggestions, sorry")))
    provider = _bedrock_provider(client)

    items = await provider.suggest_followups("hello there", [], 0)

    assert items == []
