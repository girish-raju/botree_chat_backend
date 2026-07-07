# Codebase Guide

How this backend is put together, and where to add things. Companion docs:
`README.md` (what this is), `SETUP.md` (how to run it), `CONTRIBUTING.md`
(how to work on it), `TECH_JUSTIFICATION.md` (why each technology was
chosen), `TEST_PLAN.md` (the full test catalog).

---

## 1. Directory map

Every package under `app/`, one line each:

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI application factory (`create_app`) + lifespan (startup/shutdown wiring for Postgres, MySQL/SSH, the embedder, the sweeper, middleware, routers). |
| `app/config.py` | `Settings` (pydantic-settings) — the single source of truth for every env var. |
| `app/logging.py` | structlog configuration + `RequestContextMiddleware` (per-request id, echoed as `x-request-id`). |
| `app/errors.py` | `AppError` hierarchy (`AuthError`, `SQLSafetyError`, `RBACError`, `UpstreamLLMError`, ...) and the FastAPI exception handlers that turn them into a consistent `{"error": {...}}` JSON envelope. |
| `app/deps.py` | FastAPI dependencies: `get_session` (per-request `AsyncSession`), `get_current_user` (JWT bearer -> `User`). |
| `app/api/` | HTTP routers only — `auth.py` (login/me), `chat.py` (streaming chat), `threads.py` (thread/message CRUD), `health.py` (`/healthz`, `/readyz`). No business logic lives here; routers call into `app.chat`, `app.threads.service`, etc. |
| `app/auth/` | `jwt.py` (PyJWT HS256 encode/decode), `passwords.py` (bcrypt hash/verify, called directly — not via passlib, see its docstring). |
| `app/threads/` | `service.py` — business logic for thread/message CRUD (tenant isolation, soft delete, opaque message content). |
| `app/chat/` | The conversational core: `pipeline.py` (orchestrator), `rewriter.py` (follow-up resolution), `answerer.py` (deterministic facts + streamed NL answer), `stream.py` (AI SDK v6 UI-message-stream SSE encoder). |
| `app/cache/` | Three-tier cache: `normalizer.py` (text normalization + temporal-intent extraction), `semantic.py` (L0 exact + L1 semantic `QueryCache`), `templater.py` (SQL literal parameterize/bind), `results.py` (L2 `ResultCache`), `embeddings.py` (lazy-loaded local `sentence-transformers` `Embedder`). |
| `app/llm/` | Provider abstraction: `base.py` (`LLMProvider` protocol, `SQLPlan`, `Turn`), `factory.py` (env-driven singleton selection), `anthropic_provider.py`, `cloudflare_provider.py`, `prompts.py` (static/dynamic system-prompt assembly for Anthropic prompt caching). |
| `app/domain/` | The pure semantic layer — schema knowledge with zero framework/IO dependencies: `schema_catalog.py` (table whitelist, column descriptions, column->table maps), `sql_rules.py` (the 18 SQL-generation rules + few-shots), `glossary.py` (business terms, state-name mapping, synonyms), `formatting.py` (greeting detection, rupee formatting, deterministic totals, YoY/MTD detection). |
| `app/sqlsafety/` | `guard.py` (`assert_safe` — AST whitelist gate), `limiter.py` (`enforce_limit` — outer LIMIT clamp), `fixer.py` (`fix_column_aliases` — best-effort alias repair). |
| `app/rbac/` | `profiles.py` (`RBACProfile`, `rbac_fingerprint`), `hierarchy.py` (`resolve_subtree` — sales-force subtree resolution, TTL-cached), `injector.py` (`apply_scope` — AST-based per-user row filtering, fail-closed). |
| `app/db/` | `postgres.py` (async engine/sessionmaker singleton for the app DB), `analytics.py` (`AnalyticsDB` — pooled PyMySQL over an optional SSH tunnel, runs queries in a worker thread), `models.py` (all ORM models). |
| `app/middleware/` | `ratelimit.py` — in-process, per-identity, per-route fixed-window rate limiter. |
| `app/tasks/` | `sweeper.py` — background loop that deletes expired `result_cache` rows. |

## 2. Layered responsibilities — request flow

```
HTTP request
   |
   v
app/api/*.py  (routers: parse request, call get_current_user / get_session deps)
   |
   v
app/deps.py   (JWT -> User, per-request AsyncSession)
   |
   v
app/chat/pipeline.py  (ChatPipeline.run — the orchestrator, chat only)
   |         \
   |          \--> app/threads/service.py   (thread/message CRUD path, bypasses the pipeline)
   v
   +--> app/domain/formatting.py     (greeting short-circuit)
   +--> app/chat/rewriter.py         --> app.llm (follow-up resolution)
   +--> app/cache/normalizer.py      (normalize + temporal intent)
   +--> app/cache/semantic.py        (L0/L1 QueryCache, Postgres)
   +--> app/cache/embeddings.py      (local embedding on L1 miss)
   +--> app/llm/factory.py -> {anthropic,cloudflare}_provider.py   (SQL generation, self-correction)
   +--> app/sqlsafety/fixer.py + guard.py + limiter.py   (AST safety gate, fail-closed)
   +--> app/rbac/profiles.py + hierarchy.py + injector.py   (per-user row scoping, fail-closed)
   +--> app/cache/results.py         (L2 result cache, Postgres)
   +--> app/db/analytics.py          (MySQL execution, only on cache miss)
   +--> app/chat/answerer.py         (deterministic facts, then app.llm streamed prose)
   +--> app/db/models.SqlAuditLog    (exactly one audit row per run, via session)
   |
   v
app/chat/stream.py   (encode PipelineEvents as AI SDK v6 SSE frames)
```

Key rule: **`app.domain` is pure data/logic with no I/O** — schema knowledge,
SQL rule text, glossary terms, formatting helpers. Every other layer reads
from it; it reads from nothing. `app.sqlsafety` and `app.rbac` both import
`app.domain.schema_catalog` as their single source of table/column truth —
never hardcode a table or column name elsewhere.

`app.llm` implementations never call `app.sqlsafety` or `app.rbac` directly;
they only see the `validate` hook the pipeline hands them (guard-only,
pre-RBAC — see `ChatPipeline._validate`). RBAC application happens later in
the pipeline once the user's hierarchy subtree is resolved. This is the
"LLM proposes, code disposes" ordering: nothing the model outputs is ever
trusted without passing through `app.sqlsafety` then `app.rbac`.

## 3. "Where do I add X?" cookbook

- **New HTTP endpoint** → add a router module in `app/api/<name>.py` (business
  logic goes in a service module, e.g. `app/<name>/service.py`, not in the
  router); wire it into `app/main.py` via `app.include_router(...)`; require
  auth with `Annotated[User, Depends(get_current_user)]`. Mirror
  `app/api/threads.py` + `app/threads/service.py` as the reference pattern
  (router = request/response shapes + auth + thin delegation; service =
  actual logic, tenant-scoped queries, `NotFoundError` on cross-tenant access).

- **New DB table/column** → add/edit the ORM model in `app/db/models.py`
  (remember the SQLite-compatibility notes in its module docstring — JSON
  columns via `_json_type()`, `sa.Uuid`, BigInteger-with-sqlite-variant for
  autoincrement PKs), then `alembic revision --autogenerate -m "..."` and
  **review the generated SQL by hand** before committing it (see `alembic/versions/0001_initial.py`
  for the existing style).

- **New business/SQL rule or schema knowledge** → `app/domain/` —
  `schema_catalog.py` for table/column facts and the `INCLUDE_TABLES`
  whitelist, `sql_rules.py` for SQL-generation rules/few-shots, `glossary.py`
  for business terms/synonyms/state-name mapping. This is the single
  semantic-layer source; **never hardcode a schema fact (table name, column
  name, join key) anywhere else** — `app.sqlsafety.guard`, `app.rbac.injector`,
  and `app.llm.prompts` all import from here.

- **New SQL safety check** → `app/sqlsafety/guard.py` (add to
  `_FORBIDDEN_NODE_TYPES` / `_FORBIDDEN_FUNCTIONS`, or extend
  `_assert_table_allowed`), plus a table-driven test in
  `tests/test_sqlsafety.py` (see the existing `SAFE-01..13` cases).

- **New RBAC behavior** → `app/rbac/injector.py` (`apply_scope` /
  `_scope_table` / `_table_predicate`), plus a golden-SQL test in
  `tests/test_rbac_injector.py`. **Fail-closed is mandatory**: if a
  configured scope dimension cannot be enforced on a table, raise
  `RBACError` — never silently under-scope.

- **New LLM provider** → implement the `LLMProvider` protocol
  (`app/llm/base.py`: `generate_sql`, `stream_answer`, `rewrite_question`,
  `generate_title`) in a new `app/llm/<name>_provider.py`, register it in
  `app/llm/factory.py._build_provider`, and add its config fields to
  `app/config.py` + `.env.example`. Providers must never bypass downstream
  validation — the pipeline's `validate` hook and the later RBAC step are
  the only trust boundary; a weaker model may only degrade answer quality,
  never safety.

- **New cache behavior** → `app/cache/` — `normalizer.py` for text
  normalization/temporal-intent rules, `semantic.py` for L0/L1 lookup logic,
  `templater.py` for literal parameterization/binding, `results.py` for the
  L2 result cache. Remember: templates are cached **pre-RBAC** (one template
  serves every user); RBAC is applied per-user after retrieval, and the L2
  result cache is keyed on `(final_sql, rbac_fingerprint)` so it can never
  cross user scopes.

- **New pipeline step** → `app/chat/pipeline.py` (`ChatPipeline.run`). Emit a
  new `PipelineEvent` dataclass if the step produces client-visible output,
  and keep the LLM-proposes/code-disposes ordering (guard -> RBAC -> execute).
  Every terminal path must still write exactly one `SqlAuditLog` row via
  `self._audit(...)`.

- **New config/env var** → add the field to `Settings` in `app/config.py`
  (with a sane default) and document it in `.env.example` under the
  appropriate `# --- Group ---` heading.

- **New background task** → `app/tasks/` (mirror `sweeper.py`: a
  never-raising `*_once` function for testability, a `_*_loop` wrapper, and
  `start_*`/`stop_*` functions called from `app/main.py`'s lifespan, stashing
  the `asyncio.Task` on `app.state`).

## 4. Testing map

242 tests, all in `tests/`, all runnable offline (`pytest -q`, no Docker, no
network, no API keys required):

| Test file | Covers |
|---|---|
| `tests/test_auth.py` | Login, `/me`, JWT validation (`app/api/auth.py`, `app/auth/`). |
| `tests/test_threads.py` | Thread/message CRUD, tenant isolation (`app/api/threads.py`, `app/threads/service.py`). |
| `tests/test_chat_api.py` | `/api/chat` SSE wire format, with `ChatPipeline` faked via `dependency_overrides[get_pipeline]`. |
| `tests/test_pipeline.py` | `ChatPipeline.run` orchestration end-to-end against a real in-memory SQLite session, with LLM/MySQL/embedder/query-cache mocked. |
| `tests/test_sqlsafety.py` | `assert_safe` / `enforce_limit` / `fix_column_aliases` (TEST_PLAN SAFE-01..13). |
| `tests/test_rbac_injector.py` | `apply_scope`, hierarchy resolution, profiles/fingerprints (TEST_PLAN RBAC-01..10, RBAC-12), golden SQL normalized through sqlglot. |
| `tests/test_cache.py` | Normalizer, temporal-intent guard, templater round-trip, `QueryCache`/`ResultCache` logic against a mocked `AsyncSession`. |
| `tests/test_llm_providers.py` | Factory selection, Anthropic tool-loop + self-correction, Cloudflare JSON-mode + `parse_llm_json` repair — all with mocked SDK/httpx clients. |
| `tests/test_domain.py` | Greeting detection, rupee formatting, `compute_total_facts`, YoY/MTD detection (`app/domain/formatting.py`). |
| `tests/test_analytics.py` | `AnalyticsDB` / `SSHTunnelManager` logic with `SSHTunnelForwarder`/`pymysql`/`PooledDB` fully mocked — no real SSH or MySQL. |
| `tests/test_models.py` | ORM models round-trip against the SQLite test DB. |
| `tests/test_ratelimit.py` | `RateLimitMiddleware` (opts back into rate limiting explicitly; the shared `client` fixture disables it by default). |
| `tests/test_sweeper.py` | `sweep_once` TTL deletion logic. |
| `tests/test_health.py` | `/healthz`, `/readyz`. |

**Offline/mocked philosophy:** `tests/conftest.py` builds every test's
Postgres access against an in-memory SQLite engine (`sqlite+aiosqlite:///:memory:`),
with `Base.metadata.create_all` run for every table **except `query_cache`**
— `QueryCacheEntry.question_embedding` uses `pgvector.sqlalchemy.Vector`,
which does not compile on SQLite, so `app.cache.semantic.QueryCache` is
instead exercised against a mocked `AsyncSession` (verifying the SQL-building
and threshold/temporal-veto decision logic, not real pgvector nearest-neighbor
search — that needs a real Postgres+pgvector instance, exercised only
manually/in integration, per `TEST_PLAN.md`'s `[I]`-tagged cache cases).
LLM calls, MySQL/SSH, and the embedding model are always mocked/faked in unit
tests — nothing in `pytest -q` needs a network call, Docker, or an API key.

## 5. Key invariants

- **LLM output is never trusted.** Every generated/cached SQL string passes
  through `app.sqlsafety.guard.assert_safe` (AST whitelist, fail-closed) and
  `app.rbac.injector.apply_scope` (row scoping, fail-closed) before it ever
  reaches MySQL. A provider swap (Anthropic <-> Cloudflare) can change answer
  quality; it cannot change what's allowed to execute.
- **RBAC and SQL safety are deterministic and fail-closed.** Anything the
  guard or injector cannot prove safe/scoped is blocked with a friendly
  message and audited — never guessed, never silently widened.
- **Caches store pre-RBAC SQL templates; RBAC is applied per-user after
  retrieval.** One `query_cache` entry serves every user who asks the same
  (or a semantically similar, temporally-matching) question; the L2
  `result_cache` is keyed on `(final_sql, rbac_fingerprint)` specifically so
  two users with different scopes never share cached rows.
- **Message content is opaque.** `app/threads/service.py` and `Message.content`
  never parse or interpret the AI SDK v6 message JSON — it round-trips
  byte-identical.
- **No secrets in git.** `.env` is git-ignored; only `.env.example` (with
  placeholder values) is committed. See `CONTRIBUTING.md`.

## Fidelity caveat (read before touching `app/rbac/injector.py`)

`app/domain/schema_catalog.SCHEMA_DESCRIPTION` only lists the human-readable
`sales_hierN_name` columns for each fact table; the parallel `sales_hierN_code`
columns the RBAC injector actually filters on are **assumed** to exist
whenever the matching `_name` column is present (`_supports_column` in
`app/rbac/injector.py`). This assumption is currently unverified against the
live Bisk Farm MySQL schema. Because every path here fails closed, a wrong
assumption can only produce an over-blocked (safely refused) query — never an
under-scoped, leaking one — but it should still be verified against the real
schema before relying on hierarchy-based RBAC for a table where this hasn't
been confirmed.
