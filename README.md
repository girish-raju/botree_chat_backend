# Botree Chat Backend

Natural-language-to-SQL chatbot backend for Bisk Farm's FMCG distribution
data — ask a business question in plain English, get a role-scoped answer
grounded in real numbers from the analytics database, without writing SQL and
without leaking data outside your role's visibility.

**Status:** feature-complete, 242 tests passing (`pytest -q`, fully offline).

## What it does

- Takes a natural-language business question (e.g. "what were total sales in
  Tamil Nadu last month") from an authenticated user in a chat thread.
- Resolves follow-ups against conversation history, checks a two-tier cache
  for a matching question, and — on a miss — asks an LLM to generate a
  single read-only SQL query.
- Runs every candidate SQL query through a deterministic, fail-closed safety
  gate (parse-time AST whitelist) and an RBAC row-scoping step (geo +
  sales-hierarchy predicates injected per user) before it ever touches the
  database.
- Executes against a read-only MySQL analytics database (Bisk Farm's
  reporting DB, reached over SSH), streams the answer back as text plus a
  result table, and audits every attempt (ok, blocked, or error).

## Architecture

Two databases, five cache/safety tiers between the question and the data:

```
                        ┌─────────────────────────┐
 User (JWT) ──HTTP/SSE──▶  FastAPI (app/api/*)     │
                        └────────────┬─────────────┘
                                     │
                          app/chat/pipeline.py
                                     │
        ┌───────────────┬───────────┼────────────┬──────────────┐
        ▼               ▼           ▼            ▼              ▼
   greeting /      L0 exact    L1 semantic   LLM generate   (all miss)
   follow-up       cache       cache          (Anthropic /
   rewrite         (normalized (pgvector,     Cloudflare,
   (app/chat/      text ==)    cosine≥0.92    self-correcting)
   rewriter.py)                + temporal
                                veto)
        │               │           │            │
        └───────────────┴─────┬─────┴────────────┘
                              ▼
                  app/sqlsafety  (AST whitelist, fail-closed)
                              │
                              ▼
                  app/rbac      (per-user row scoping, fail-closed)
                              │
                              ▼
                 L2 result cache (final_sql + rbac_fingerprint, TTL)
                              │
                              ▼
        ┌─────────────────────────────────────┐   ┌───────────────────┐
        │  Postgres (app DB)                  │   │  MySQL (Bisk Farm) │
        │  users, threads, messages,          │   │  read-only, via     │
        │  query_cache, result_cache,         │◀──┤  SSH tunnel          │
        │  sql_audit_log (pgvector for L1)   │   │  (analytics source) │
        └─────────────────────────────────────┘   └───────────────────┘
                              │
                              ▼
              deterministic totals + streamed NL answer
              (AI SDK v6 UI message stream over SSE)
                              │
                              ▼
                    sql_audit_log row written (always)
```

## Key features

- **Provider-switchable LLM** — `LLM_PROVIDER=anthropic|cloudflare` behind
  one `LLMProvider` interface; Anthropic uses native tool-calling + prompt
  caching, Cloudflare (Llama 3.1) uses JSON-mode with a repair/retry pass.
  Neither can bypass downstream safety — a weaker model only degrades answer
  quality, never security.
- **Three-tier, token-saving cache** — L0 exact match, L1 semantic match
  (local embeddings, no API cost) with a temporal false-positive guard, L2
  result cache keyed by final SQL + RBAC scope. Repeat/paraphrased questions
  cost zero LLM tokens.
- **RBAC row-scoping** — AST-based predicate injection (geo + sales-force
  hierarchy), fail-closed: anything that can't be safely scoped is blocked,
  never under-scoped.
- **SQL safety guard** — sqlglot-parsed AST whitelist (SELECT-only, table
  whitelist, no dangerous functions, single statement, LIMIT enforcement).
- **Full audit trail** — every chat turn (ok/blocked/error) writes one
  `sql_audit_log` row with cache level, tokens, and timing.
- **Streaming** — AI SDK v6 UI message stream over SSE, compatible with the
  assistant-ui / Vercel AI SDK frontend.

## Tech stack

| Layer | Choice |
|---|---|
| Language / framework | Python 3.11+, FastAPI, Uvicorn |
| App database | PostgreSQL 16 + pgvector, SQLAlchemy 2.0 (async) + asyncpg, Alembic |
| Analytics source | MySQL (read-only), PyMySQL + sshtunnel + DBUtils pooling |
| SQL parsing/safety | sqlglot (AST guard, limiter, RBAC injection) |
| LLM | Anthropic Claude (native tools) or Cloudflare Workers AI / Llama 3.1 (JSON-mode), switchable via env |
| Embeddings | sentence-transformers, `BAAI/bge-small-en-v1.5` (local, free) |
| Auth | PyJWT (HS256) + bcrypt |
| Logging | structlog (console dev / JSON prod, per-request id) |
| Testing | pytest + pytest-asyncio + httpx ASGI client, SQLite for unit tests |

## Quickstart

See **[SETUP.md](SETUP.md)** for the full from-zero setup (Postgres via
Docker, migrations, seeded users, running the app, switching LLM providers,
the MySQL SSH-key gotcha, running tests).

## Further reading

- **[SETUP.md](SETUP.md)** — step-by-step environment setup and run instructions.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — branch/PR conventions, testing expectations, code style, the fail-closed rule.
- **[CODEBASE.md](CODEBASE.md)** — directory map, request-flow layering, and a "where do I add X?" cookbook.
- **[TECH_JUSTIFICATION.md](TECH_JUSTIFICATION.md)** — why each technology/architecture choice was made, and what was rejected.
- **[TEST_PLAN.md](TEST_PLAN.md)** — the full test case catalog across auth, RBAC, SQL safety, caching, LLM providers, streaming, security, and performance.

## Test status

```
242 passed in ~11s   (pytest -q — fully offline, no Docker/network/API keys required)
```
