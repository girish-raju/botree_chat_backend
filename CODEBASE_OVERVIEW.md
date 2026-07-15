# Codebase Overview (Beginner-Friendly)

A simple explanation of this project for someone new to it.
For deep detail, see `CODEBASE.md` (developer cookbook) and `README.md`.

---

## 1. What is this project?

- It is a **chatbot backend** built with **Python + FastAPI**.
- Users ask business questions in plain English, like *"What were total sales in Tamil Nadu last month?"*.
- The backend converts the question into a **SQL query** using an AI model (LLM).
- It runs that SQL on Bisk Farm's **MySQL analytics database** (read-only).
- It streams back a friendly answer with real numbers and a result table.
- Users only see data their **role** allows (a manager sees their region, not the whole country).

Think of it as: **English question in → safe SQL → real data → English answer out.**

---

## 2. Tech stack (what tools are used)

| Thing | Tool used | Why |
|---|---|---|
| Web framework | FastAPI + Uvicorn | Handles HTTP requests, async, fast |
| App database | PostgreSQL (+ pgvector) | Stores users, chats, caches, audit logs |
| Data source | MySQL (over an SSH tunnel) | Bisk Farm's reporting data (read-only) |
| AI models | Anthropic Claude / Cloudflare Llama / AWS Bedrock | Turns English into SQL, writes answers |
| Embeddings | Cloudflare Workers AI API (BGE model) | Finds "similar questions" for the cache |
| Voice-to-text | Cloudflare Whisper | Lets users speak their question |
| SQL parsing | sqlglot | Reads SQL as a tree to check it is safe |
| Auth | JWT tokens + bcrypt passwords | Login and identity |
| ORM / migrations | SQLAlchemy 2.0 (async) + Alembic | Talk to Postgres, evolve its schema |
| PDF export | fpdf2 | Download a chat as a branded PDF report |
| Logging | structlog | Structured logs with a per-request ID |
| Tests | pytest (242 tests, fully offline) | No network or API keys needed to test |

---

## 3. The journey of one question (most important part!)

When a user sends a message to `/api/chat`, this happens in `app/chat/pipeline.py`:

1. **Login check** — the JWT token tells us who the user is.
2. **Greeting?** — if the user just says "hi", reply instantly. No AI needed.
3. **Follow-up rewrite** — "what about Kerala?" becomes "total sales in Kerala last month" using chat history.
4. **L0 cache (exact)** — have we seen this exact question before? Reuse the saved SQL.
5. **L1 cache (semantic)** — have we seen a *similar* question? (Compares meaning using embeddings.) Reuse its SQL.
6. **Ask the LLM** — only if both caches miss, the AI writes one read-only SQL query.
7. **Safety guard** — the SQL is parsed and checked: SELECT only, allowed tables only, no dangerous functions, one statement, forced LIMIT. If anything looks wrong → **block it**.
8. **RBAC scoping** — extra WHERE conditions are injected so the user only sees rows for their region / team. If we can't scope it safely → **block it**.
9. **L2 cache (results)** — did someone with the *same access* already run this exact final SQL recently? Reuse those rows.
10. **Run on MySQL** — only on a cache miss, execute the query on the analytics DB.
11. **Answer** — totals are computed by code (not the AI, so numbers are exact), then the AI streams a short prose answer + a result table + follow-up suggestions.
12. **Audit** — every single attempt (success, blocked, or error) writes one row to `sql_audit_log`.

**Golden rule: the AI's output is never trusted.** Everything it writes must pass
the safety guard and RBAC steps before touching the database. This is called
"LLM proposes, code disposes" — and both checks **fail closed** (when in doubt, block).

---

## 4. Folder tour (`app/`)

| Folder / file | In simple words |
|---|---|
| `main.py` | Builds the app: plugs in routes, middleware, and startup/shutdown (DB pools, SSH key, embedder, sweeper). |
| `config.py` | One `Settings` class that reads every `.env` variable. |
| `logging.py` | Log setup + gives every request an ID (`x-request-id`). |
| `errors.py` | Custom error classes → turned into clean JSON error responses. |
| `deps.py` | Shared helpers for routes: "give me a DB session", "give me the logged-in user". |
| `api/` | The HTTP endpoints only — thin layer, no business logic. (`auth`, `chat`, `threads`, `transcribe`, `health`) |
| `auth/` | JWT token create/verify + bcrypt password hashing. |
| `chat/` | The brain: `pipeline.py` (the 12 steps above), `rewriter.py` (follow-ups), `answerer.py` (facts + answer text), `stream.py` (sends events to the browser as SSE). |
| `llm/` | AI providers behind one common interface: `anthropic_provider.py`, `cloudflare_provider.py`, `bedrock_provider.py`, chosen by `factory.py` from `LLM_PROVIDER` env var. `prompts.py` builds the system prompt. `whisper.py` = voice-to-text. |
| `cache/` | The 3-tier cache: `normalizer.py` (clean up text, detect dates), `semantic.py` (L0 + L1), `templater.py` (swap literals for placeholders so one cached SQL serves many questions), `results.py` (L2), `embeddings.py` (calls the embedding API). |
| `domain/` | Pure knowledge, no I/O: `schema_catalog.py` (allowed tables + column meanings), `sql_rules.py` (rules the AI must follow), `glossary.py` (business terms, state names), `formatting.py` (rupee formatting, greetings, exact totals). **All table/column facts live here — nowhere else.** |
| `sqlsafety/` | `guard.py` (the whitelist check), `limiter.py` (force a LIMIT), `fixer.py` (repair small alias mistakes). |
| `rbac/` | `profiles.py` (a user's access scope), `hierarchy.py` (find everyone under a manager), `injector.py` (add the WHERE filters into the SQL tree). |
| `db/` | `postgres.py` (app DB engine), `analytics.py` (MySQL over SSH, pooled), `models.py` (all Postgres tables). |
| `threads/` | `service.py` (chat/message CRUD, users can't see each other's threads), `pdf.py` (export a thread as a PDF report). |
| `middleware/` | `ratelimit.py` — stops one user hammering the API. |
| `tasks/` | `sweeper.py` — background loop that deletes expired cached results. |

Other top-level folders:

- `tests/` — 242 offline tests (SQLite stands in for Postgres; LLM/MySQL/SSH are mocked).
- `alembic/` — database migration scripts for Postgres.
- `scripts/` — helper scripts (seed users, smoke test, token cost report).
- `docker/`, `Dockerfile`, `docker-compose*.yml` — containers for local dev and deployment.

---

## 5. The two databases (don't mix them up)

| | Postgres (app DB) | MySQL (analytics DB) |
|---|---|---|
| Owned by | This app (read + write) | Bisk Farm (read-only!) |
| Holds | users, threads, messages, caches, audit log | the actual sales/business data |
| Reached via | asyncpg connection pool | PyMySQL through an SSH tunnel |
| Tables | `users`, `threads`, `messages`, `query_cache`, `result_cache`, `sql_audit_log` | whitelisted fact tables (see `schema_catalog.py`) |

---

## 6. API endpoints

| Method + path | What it does |
|---|---|
| `POST /api/auth/login` | Log in, get a JWT token. |
| `GET /api/auth/me` | Who am I? |
| `POST /api/chat` | Send a message, get a streamed (SSE) answer. |
| `GET/POST /api/threads` | List / create chat threads. |
| `GET/PATCH/DELETE /api/threads/{id}` | Read / rename / delete a thread. |
| `POST /api/threads/{id}/title` | Auto-generate a thread title. |
| `GET/POST/DELETE /api/threads/{id}/messages` | Read / save / clear messages. |
| `GET /api/threads/{id}/export/pdf` | Download the thread as a PDF report. |
| `POST /api/transcribe` | Upload voice audio, get back text. |
| `GET /healthz`, `GET /readyz` | Is the app alive / ready? |

---

## 7. Rules to remember when changing code

- **Never trust AI output** — everything must pass `sqlsafety` then `rbac` before running.
- **Fail closed** — if a safety check is unsure, block the query. Never guess.
- **Schema facts live in `app/domain/` only** — never hardcode a table or column name elsewhere.
- **Routers stay thin** — business logic goes in a service module, not in `app/api/`.
- **Every chat turn writes exactly one audit row** — keep it that way in new pipeline paths.
- **Cached SQL is stored *before* RBAC** — user filters are added fresh each time, so one cache entry safely serves everyone. The results cache is keyed by user scope so people never see each other's rows.
- **No secrets in git** — real values go in `.env` (ignored); `.env.example` shows the shape.
- **Run `pytest -q` before pushing** — all 242 tests run offline in seconds.

---

## 8. Mini glossary (jargon decoder)

- **LLM** — Large Language Model, the AI (Claude / Llama) that writes SQL and answers.
- **JWT** — a signed token proving who the user is; sent with every request.
- **SSE** — Server-Sent Events; how the answer streams word-by-word to the browser.
- **AST** — Abstract Syntax Tree; SQL parsed into a tree so code can inspect it safely.
- **RBAC** — Role-Based Access Control; limiting rows by the user's role/region.
- **Embedding** — a list of numbers representing a sentence's *meaning*; similar questions get similar numbers (used by the L1 cache, stored with pgvector).
- **Fail closed** — when unsure, refuse. The safe default.
- **SSH tunnel** — a secure pipe through which we reach the remote MySQL server.
- **Migration (Alembic)** — a script that changes the Postgres schema step by step.

---

## 9. Where to read next

- `SETUP.md` — how to run it locally, step by step.
- `CODEBASE.md` — the detailed developer cookbook ("where do I add X?").
- `TECH_JUSTIFICATION.md` — why each technology was chosen.
- `TEST_PLAN.md` — the full catalog of test cases.
