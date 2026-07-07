# Technology Justification — botree_chat_backend

Every significant technology and architectural choice in this backend, with the reason it was picked and what was considered instead. Companion docs: `README.md` (what this is), `SETUP.md` (how to run it).

---

## Language & Framework

### Python 3.11+
**Why:** The AI/LLM ecosystem (Anthropic SDK, sentence-transformers, sqlglot, LangChain-era tooling) is Python-first; the existing prototype (`conversational_bot_v15.py`) is Python, so the domain knowledge (schema catalog, SQL rules, RBAC logic) ports directly instead of being rewritten in another language.
**Rejected:** Node/TypeScript backend — would unify language with the frontend, but local embedding models and SQL AST tooling are weaker in Node, and the prototype port would be a full rewrite.

### FastAPI
**Why:** Async-native (needed for streaming chat responses and concurrent DB access), automatic OpenAPI docs, first-class Pydantic integration for request/response validation, and the de-facto standard for Python API services. The prototype's Streamlit-based API path was not concurrency-safe; FastAPI with proper dependency injection fixes that class of bug structurally.
**Rejected:** Flask (sync-first, streaming is bolted on), Django (ORM/admin weight we don't need), keeping Streamlit (a UI tool, not an API server — its `session_state` under concurrent requests was the prototype's biggest flaw).

### Uvicorn
**Why:** Standard ASGI server for FastAPI; `uvicorn[standard]` brings uvloop/httptools for performance.

---

## Data Stores

### PostgreSQL (app database)
**Why:** One production-grade database holds users, chat threads/messages, the query cache, the result cache, and the SQL audit log. ACID for chat history, JSONB for opaque message payloads, and mature operational tooling.
**Rejected:** SQLite (fine for demos, weak under concurrent writes, no pgvector), MongoDB (no need for schemaless; we want relational integrity between users/threads/messages).

### pgvector extension
**Why:** The semantic cache needs nearest-neighbour search over question embeddings. pgvector gives cosine similarity with an HNSW index **inside the database we already run** — no extra vector-DB service to deploy, back up, or keep consistent.
**Rejected:** FAISS (in-process index that must be rebuilt/persisted manually, doesn't survive multi-instance deployment), Pinecone/Weaviate/Qdrant (a whole extra managed service for a table that will hold thousands, not billions, of rows).

### Redis — deliberately NOT used
**Why not:** The result cache (short-TTL rows) lives in Postgres. At this system's traffic, Postgres handles it easily, and it's one less moving part to operate. If hot-path latency ever demands it, the result-cache module is isolated so Redis can be swapped in behind the same interface.

### MySQL (analytics source, read-only)
**Why:** Not a choice — it's the existing Bisk Farm reporting database. We connect with a read-only posture (SELECT-only guard, statement timeout, row cap) and never migrate or write to it.

---

## LLM Layer

### Pluggable provider (`LLM_PROVIDER=anthropic|cloudflare`)
**Why:** Explicit product requirement: switch providers via env without code changes. Both implementations sit behind one interface; everything downstream (validation, RBAC, execution) is provider-agnostic, so a weak model can never bypass safety.
**Current running default:** this deployment's `.env` sets `LLM_PROVIDER=cloudflare` (Llama 3.1) — a project decision to test/run on the free/already-provisioned model rather than Claude; `.env.example`'s template value (`anthropic`) is just a placeholder default for a fresh checkout. See `SETUP.md`.

### Anthropic Claude (default: claude-sonnet-5 for SQL, claude-haiku-4-5 for cheap steps)
**Why:** Best-in-class SQL generation with native tool-calling, which gives us the self-correction loop (model sees the SQL error, fixes it) for free. **Prompt caching** matters enormously here: the static schema + rules + glossary block is large, and Anthropic bills cached reads at ~10% of input price — a direct answer to the token-cost requirement. Small/cheap model (Haiku) handles follow-up rewriting and thread titling, where intelligence isn't needed.
**Rejected as sole option:** it's the default, not the only path (see below).

### Cloudflare Workers AI — Llama 3.1 8B (alternative provider)
**Why:** Already provisioned by the team (prototype used it); near-zero marginal cost. It lacks reliable native tool-calling, so this provider uses JSON-mode single-shot generation with a JSON-repair pass and one validation-feedback retry. Because all safety enforcement is deterministic code outside the LLM, the weaker model degrades answer quality — never safety.

### Token-saving architecture (the core cost design)
**Why each layer exists:**
1. **L0 exact cache** (normalized question → SQL template): repeat questions cost 0 LLM tokens.
2. **L1 semantic cache** (local embeddings + pgvector, cosine ≥ 0.92): paraphrases ("sales today" vs "current day sales") cost 0 LLM tokens. Embeddings are computed **locally** (sentence-transformers), so cache lookups themselves cost no API tokens.
3. **SQL templates cached pre-RBAC, parameterized:** one cache entry serves every user (RBAC filters are injected per-user after retrieval) and survives date rollover (date expressions stored as placeholders, re-bound at execution). This is what makes "user 1 and user 2 ask the same thing → LLM called once" true.
4. **Result cache** (hash of final SQL + RBAC scope, 5-min TTL): repeated identical hits skip MySQL entirely.
5. **Anthropic prompt caching** on the static system prompt: even cold questions pay ~10% for the schema block.
6. **Follow-up rewriter** uses the cheapest model, and is skipped when the message is already standalone.
**Threshold justification:** 0.92 cosine is the researched band (0.90–0.95); below ~0.90 semantic caches demonstrably return wrong queries for subtly different intents. A temporal-intent keyword guard additionally prevents "today" matching "yesterday", the classic false-positive for embedding caches over analytics questions.

### sentence-transformers + BAAI/bge-small-en-v1.5
**Why:** Local, free, fast, 384-dim (small index), and top-tier quality among small embedding models. Cache-lookup quality depends far more on threshold calibration than on embedding model size, so a small local model is the right trade.
**Rejected:** OpenAI/Voyage embedding APIs (per-call cost and latency on the *token-saving* path defeats the purpose; also an extra vendor).

---

## SQL Safety & RBAC

### sqlglot
**Why:** The security core. All generated SQL is parsed into an AST to (a) enforce single-statement SELECT-only, (b) whitelist tables, (c) inject/clamp LIMIT, and (d) inject per-user RBAC predicates by rewriting the tree — correctly handling aliases, subqueries, and UNION branches. The prototype spliced RBAC filters into SQL strings by index, which breaks on subqueries and is an injection risk; AST rewriting with `exp.Literal` (auto-escaped) eliminates that class of bug. Fail-closed: if the tree can't be resolved unambiguously, the query is blocked — never guessed.
**Rejected:** regex/string manipulation (the prototype's approach — fragile and unsafe), sqlparse (tokenizer only, no real AST to rewrite).

### Defense in depth (layers, not one gate)
LLM proposes → AST guard (SELECT-only, whitelist) → LIMIT clamp → RBAC injection → statement timeout → (recommended) read-only MySQL account. Any single layer failing still leaves the others; the LLM is never trusted to enforce anything.

### Audit log (every execution attempt)
**Why:** Production accountability — who asked what, what SQL ran, which cache level served it, how many tokens it cost, and why blocked queries were blocked. Also the measurement tool for cache hit rates and token spend.

---

## Auth

### PyJWT (HS256) + bcrypt
**Why:** The prototype referenced a missing `chatbot_auth` module; this replaces it. HS256 JWTs are the simplest correct choice for a first-party frontend↔backend pair and match the planned SFA SSO handoff (which mints the same shape of token). bcrypt for password hashing (battle-tested, adaptive cost).
**Rejected:** OAuth2/OIDC provider integration now (blocks on external SFA details; JWT keeps the seam ready), sessions-in-DB (stateless tokens are simpler across the Next.js proxy).

---

## Streaming & Frontend Contract

### AI SDK v6 UI message stream, hand-implemented (`app/chat/stream.py`)
**Why:** The frontend is assistant-ui + Vercel AI SDK v6; its transport expects the documented AI SDK UI message stream wire format over SSE (`start`/`start-step`, `text-start`/`text-delta`/`text-end`, `tool-input-start`/`tool-input-available`/`tool-output-available`, `finish-step`/`finish`, the `x-vercel-ai-ui-message-stream: v1` header). **Correction (verified against code):** the `assistant-stream` Python package (assistant-ui's own emitter for this format) is not installed/used in this codebase — `app/chat/stream.py`'s `UIMessageStream` class implements the same stable, documented wire format directly, in ~100 lines, with no extra dependency. Text deltas and the `query_database` tool-call/result parts still render in the existing UI with zero custom protocol code on the frontend side; the difference is only which side of the FastAPI process assembles the SSE frames.
**Rejected:** the `assistant-stream` package itself (an extra dependency for a small, stable wire format that's cheaper to hand-roll and test directly), a fully custom (non-AI-SDK) SSE protocol (would force hand-written parsing in the frontend, re-implementing what the AI SDK runtime already does), WebSockets (unneeded bidirectionality, worse proxy/deploy story).

### Next.js proxy pattern (JWT in httpOnly cookie, frontend never calls FastAPI directly)
**Why:** The token is invisible to browser JavaScript (XSS-proof), there's no CORS surface, and the backend URL stays server-side. The Next.js route handlers attach `Authorization: Bearer` and pipe the stream through untouched.

---

## Persistence & Migrations

### SQLAlchemy 2.0 (async) + asyncpg
**Why:** The standard async ORM; typed 2.0 style; asyncpg is the fastest Postgres driver for asyncio.

### Alembic
**Why:** Versioned, reviewable schema migrations for the app database (the analytics MySQL is never migrated). Required for anything calling itself production-ready.

### PyMySQL + sshtunnel + DBUtils PooledDB (analytics access)
**Why:** PyMySQL is a pure-Python MySQL client that works cleanly in worker threads (analytics queries run via `anyio.to_thread` so they never block the event loop). `sshtunnel` reproduces the required SSH hop to the pre-prod DB host, managed by a single supervised tunnel with health-check restart — the prototype's ad-hoc tunnel handling was a known flakiness source. Connection pooling via DBUtils avoids per-request connection setup.

---

## Operations

### structlog (JSON logs + request IDs)
**Why:** Machine-parseable structured logs with a per-request correlation ID — the difference between debugging production and guessing. Console-pretty in dev, JSON in prod, same call sites.

### Docker + docker-compose (pgvector/pgvector:pg16 image)
**Why:** One-command reproducible environment (API + Postgres-with-pgvector preinstalled); the compose file doubles as executable documentation of the runtime topology.

### pytest (+ httpx ASGI client)
**Why:** The safety-critical modules (SQL guard, RBAC injector, cache correctness) are pure functions by design, so they're covered by fast table-driven/golden-file tests that run without any LLM or database — the properties that must never regress are the cheapest to test.

### ruff
**Why:** Single fast tool for linting + formatting discipline.
