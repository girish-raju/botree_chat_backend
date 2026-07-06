# Setup

From-zero instructions to get the backend running locally. See `README.md`
for what this is, `CODEBASE.md` for how it's built, `CONTRIBUTING.md` for the
dev loop once you're up and running.

## Prerequisites

- **Python 3.11+** (developed/tested on 3.12). Check with `python3 --version`.
- **Docker** (for the Postgres + pgvector container via `docker-compose.yml`).
- An **SSH private key** for the Bisk Farm MySQL analytics host — only
  required if you need live analytics data; the app boots and serves chat
  threads/auth without it (see "The MySQL SSH key" below).
- (Optional) An Anthropic API key and/or Cloudflare Workers AI account, if
  you'll exercise the real LLM path rather than mocked tests.

## 1. Clone and create a virtualenv

```bash
git clone <this-repo-url> botree_chat_backend
cd botree_chat_backend
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
```

## 2. Install dependencies

```bash
pip install -e ".[dev]"
```

This installs the runtime dependencies (FastAPI, SQLAlchemy 2.0 async,
asyncpg, alembic, pgvector, sqlglot, anthropic, httpx, sentence-transformers,
pyjwt, bcrypt, structlog, pymysql, sshtunnel, dbutils, ...) plus the dev
extras (pytest, pytest-asyncio, httpx, aiosqlite, ruff).

## 3. Configure environment variables

```bash
cp .env.example .env
```

Then edit `.env`. Every variable, grouped as in `.env.example`:

**App**
- `APP_ENV` — `dev` (console log rendering) or `staging`/`prod` (JSON logs).
- `LOG_LEVEL` — standard logging level name (`INFO`, `DEBUG`, ...).

**Auth**
- `JWT_SECRET` — long random string, HMAC signing secret. Generate one with
  `python -c "import secrets; print(secrets.token_hex(32))"`.
- `JWT_ALGORITHM` — `HS256` (don't change unless you also change the signing
  code in `app/auth/jwt.py`).
- `JWT_EXPIRY_MINUTES` — access token lifetime.

**Postgres**
- `PG_DSN` — async SQLAlchemy DSN, e.g.
  `postgresql+asyncpg://botree:botree@localhost:5432/botree_chat` (matches
  `docker-compose.yml`'s default credentials).

**LLM**
- `LLM_PROVIDER` — `anthropic` or `cloudflare`. See "Switching LLM provider" below.

**Anthropic** (only needed if `LLM_PROVIDER=anthropic`)
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL_SQL` — model used for NL→SQL generation (default `claude-sonnet-5`).
- `ANTHROPIC_MODEL_SMALL` — cheap model for rewriting/titling (default `claude-haiku-4-5`).

**Cloudflare** (only needed if `LLM_PROVIDER=cloudflare`)
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_MODEL` — Workers AI model id (default `@cf/meta/llama-3.1-8b-instruct`).

**MySQL Analytics** (the read-only Bisk Farm reporting DB)
- `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`
- `MYSQL_QUERY_TIMEOUT_S` — max seconds a generated query may run (default 15).

**SSH Tunnel** (only needed if the MySQL host is reached through a bastion)
- `SSH_TUNNEL_ENABLED` — `true`/`false`.
- `SSH_HOST`, `SSH_PORT`, `SSH_USER`
- `SSH_KEY_PATH` — path to the private key **on this machine**. See the
  gotcha below.
- `SSH_KEY_PASSWORD` — passphrase, if the key has one.

**Cache**
- `SEMANTIC_THRESHOLD` — cosine similarity cutoff for L1 cache hits (default `0.92`).
- `RESULT_CACHE_TTL_S` — L2 result cache TTL in seconds (default `300`).
- `SQL_ROW_CAP` — max rows any generated query may return (default `50`).
- `EMBEDDING_MODEL` — sentence-transformers model id (default `BAAI/bge-small-en-v1.5`).

**CORS**
- `CORS_ORIGINS` — JSON list of allowed origins, e.g. `["http://localhost:3000"]`.

**SQL Safety**
- `REQUIRE_SQL_APPROVAL` — `true` to require a human Allow/Deny gate before
  executing generated SQL (frontend feature; backend flag only).

Never commit a real `.env` — it's git-ignored, and `.env.example` must only
ever contain placeholder values.

## 4. Start Postgres

```bash
docker compose up -d db
```

This runs `pgvector/pgvector:pg16` with the credentials matching the default
`PG_DSN` above (`botree` / `botree` / db `botree_chat`), exposed on
`localhost:5432`, with a health check so dependents can wait on it. (The
compose file also defines an `api` service that builds and runs the app in a
container — for local development, running `uvicorn` directly, as below, is
usually simpler and gives you reload-on-change.)

## 5. Run migrations

```bash
alembic upgrade head
```

This creates the `vector` Postgres extension and all application tables
(`users`, `threads`, `messages`, `query_cache`, `result_cache`,
`sql_audit_log`). The DB URL comes from `Settings.pg_dsn` (i.e. your `.env`),
not from `alembic.ini` — see `alembic/env.py`.

## 6. Seed the demo users

```bash
python scripts/seed_users.py
```

Idempotent (upserts by username). Seeds the six RBAC demo roles — `vp`,
`zsm`, `rsm`, `bm`, `asm`, `so` — each with a role, sales-force code/level,
and geo scope. Password for all of them defaults to `botree123`; override
with:

```bash
SEED_PASSWORD='something-else' python scripts/seed_users.py
```

## 6b. Inspecting the Postgres database

You don't need `psql` installed locally — run it inside the container with
`docker compose exec`. All commands below assume the `db` service is up.

Open an interactive SQL shell:

```bash
docker compose exec db psql -U botree -d botree_chat
```

Or run one-off queries without entering the shell (handy for scripts/CI):

```bash
# List all application tables — expect 7 (incl. alembic_version)
docker compose exec -T db psql -U botree -d botree_chat -c "\dt"

# Confirm the pgvector extension is installed (semantic cache needs it)
docker compose exec -T db psql -U botree -d botree_chat -c \
  "SELECT extname FROM pg_extension WHERE extname='vector';"

# Confirm the 6 demo users seeded, ordered by hierarchy level (VP=100 → SO=600)
docker compose exec -T db psql -U botree -d botree_chat -c \
  "SELECT username, role, sf_level FROM users ORDER BY sf_level;"

# Which migration is applied
docker compose exec -T db psql -U botree -d botree_chat -c \
  "SELECT version_num FROM alembic_version;"
```

Once the app has served some chat traffic, these two views are the most
useful — the audit log proves what ran and how, and the cache tables show the
token-saving layers filling up:

```bash
# Recent requests: which cache level served each, row count, token spend, outcome
docker compose exec -T db psql -U botree -d botree_chat -c \
  "SELECT created_at, cache_level, row_count, tokens_in, tokens_out, status
   FROM sql_audit_log ORDER BY created_at DESC LIMIT 20;"

# How many SQL templates the L0/L1 cache has learned
docker compose exec -T db psql -U botree -d botree_chat -c \
  "SELECT count(*) AS cached_queries, sum(hit_count) AS total_hits FROM query_cache;"
```

> Note: `docker compose exec` may print `The "m" variable is not set`
> warnings — these come from Compose interpolating the compose file and are
> harmless; the query output follows below them.

### pgAdmin in the browser (recommended — nothing to install)

A web-based pgAdmin ships in `docker-compose.yml`. Start it (Postgres must be
up first):

```bash
docker compose up -d pgadmin
```

Give it ~30–60 seconds on first boot, then open:

```
http://localhost:5050
```

**Log in to pgAdmin** with:

| Field    | Value                 |
|----------|-----------------------|
| Email    | `admin@botree.co.in`  |
| Password | `admin`               |

The Postgres server is **already registered** — in the left tree, expand
**Servers → Botree Chat (local)**. The first time you expand it, pgAdmin asks
for the *database* password (separate from the pgAdmin login): enter `botree`
and tick "Save password". Then drill into
**botree_chat → Schemas → public → Tables**, right-click any table →
*View/Edit Data → All Rows* to see the contents, or open the Query Tool
(top toolbar) to run the SQL from the section above.

> The pgAdmin login (`admin@botree.co.in` / `admin`) and the database password
> (`botree`) are local dev defaults set in `docker-compose.yml`. Change
> `PGADMIN_DEFAULT_*` there for anything shared.

### Viewing the data in a desktop GUI (TablePlus / DBeaver / pgAdmin)

The Postgres container publishes port `5432` to your host, so any desktop
database client can connect to it. Use this connection URL:

```
postgresql://botree:botree@localhost:5432/botree_chat
```

Or enter the fields individually:

| Field    | Value          |
|----------|----------------|
| Host     | `localhost`    |
| Port     | `5432`         |
| Database | `botree_chat`  |
| User     | `botree`       |
| Password | `botree`       |
| SSL mode | disable / off  |

Steps (DBeaver / TablePlus are the same idea):

1. New connection → PostgreSQL.
2. Paste the URL above, or fill the fields from the table.
3. Connect → expand **botree_chat → Schemas → public → Tables**.
4. Double-click any table (`users`, `threads`, `messages`, `query_cache`,
   `result_cache`, `sql_audit_log`) to browse its rows, or open a SQL editor
   and run the queries shown above.

> These credentials are the local dev defaults from `docker-compose.yml`. If
> you change `POSTGRES_PASSWORD` there (and `PG_DSN` in `.env`), update the
> connection URL to match.

## 7. Run the app

`uvicorn` is installed **inside the virtualenv** (`.venv`), not globally, so the
venv must be active in your current terminal (or call it by its full path).

```bash
# make sure the venv is active — the prompt should show "(.venv)"
source .venv/bin/activate        # Windows: .venv\Scripts\activate
uvicorn app.main:app --reload
```

If you opened a fresh terminal (so the venv isn't active) and don't want to
activate it, run it directly from the venv instead:

```bash
.venv/bin/uvicorn app.main:app --reload
```

> **`zsh: command not found: uvicorn`** means the venv isn't active in this
> terminal — run `source .venv/bin/activate` first (from the project folder),
> or use the `.venv/bin/uvicorn ...` form above. (Your global Python has no
> `uvicorn`; all dependencies live in `.venv`.)

Serves on `http://localhost:8000` by default. On startup it initializes the
Postgres pool, the (lazy) MySQL/SSH analytics connector, warms up the local
embedding model in the background, and starts the result-cache TTL sweeper.
Make sure the databases are up first (`docker compose up -d db mysql`) or
`/readyz` will report them down.

## 8. Verify it's up

```bash
curl http://localhost:8000/healthz
# {"status":"ok"}

curl http://localhost:8000/readyz
# {"status":"ready","checks":{"postgres":true,"mysql":true}}
```

`/healthz` is a pure liveness check (always ok if the process is serving).
`/readyz` runs the registered readiness probes (Postgres connectivity, MySQL
connectivity via `SELECT 1`) and returns `503` if any fail — expect
`mysql: false` until the SSH tunnel / MySQL credentials are correctly
configured and reachable.

Then log in as a seeded user:

```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"so","password":"botree123"}'
```

## Switching LLM provider

Set `LLM_PROVIDER` in `.env` to `anthropic` or `cloudflare` and restart the
process — no code changes needed. The factory (`app/llm/factory.py`) builds
whichever provider is configured and caches it as a process-wide singleton;
there's no in-process hot-swap, so a provider change requires a restart.
Make sure the corresponding credentials (`ANTHROPIC_API_KEY` or
`CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN`) are set for whichever
provider you pick. This deployment's default is `cloudflare` (Llama 3.1) —
see the comment above `LLM_PROVIDER` in your `.env`.

## The MySQL SSH-key gotcha

The analytics MySQL host is reached through an SSH bastion
(`SSH_TUNNEL_ENABLED=true`). The private key path in an inherited/old `.env`
may point at a Windows path (e.g. `D:\Botree\...\id_rsa`) from whoever set it
up previously — that path won't exist on your machine. To fix:

1. Get the private key file (`aasim.niazi` or equivalent) onto this machine,
   e.g. `~/.ssh/aasim.niazi`.
2. Set its permissions: `chmod 600 ~/.ssh/aasim.niazi`.
3. Point `SSH_KEY_PATH` in your `.env` at that local path.
4. If the key has a passphrase, set `SSH_KEY_PASSWORD` too.

**The app boots without this.** The SSH tunnel and MySQL pool are both lazy
(`app/db/analytics.py`) — nothing connects until the first analytics query
or the `/readyz` probe runs. You can develop auth, threads, and the chat
pipeline's cache/safety/RBAC logic entirely without a working MySQL
connection; only live data queries and `mysql: true` in `/readyz` need it.

## Running tests

```bash
pytest -q
```

242 tests, all offline — no Docker, no network calls, no API keys required.
Unit tests build every table against an in-memory SQLite database (except
`query_cache`, which uses a `pgvector` column type SQLite can't compile —
see `tests/conftest.py` and `CODEBASE.md` §4). LLM calls, MySQL/SSH, and the
embedding model are all mocked in the test suite.

To run a single file or test:

```bash
pytest tests/test_rbac_injector.py -q
pytest tests/test_sqlsafety.py::test_safe_09_clamps_limit_above_cap -q
```

## Ports and how to stop everything

### Ports in use

| Service               | URL / host                | Port   | Started by                          |
|-----------------------|---------------------------|--------|-------------------------------------|
| Backend API (uvicorn) | http://localhost:8000     | `8000` | `uvicorn app.main:app`              |
| PostgreSQL (app DB)    | localhost                 | `5432` | `docker compose up -d db`           |
| pgAdmin (DB GUI)      | http://localhost:5050     | `5050` | `docker compose up -d pgadmin`      |
| Local MySQL (analytics)| localhost                 | `3307` | `docker compose up -d mysql`        |
| Frontend (Next.js)    | http://localhost:3000     | `3000` | `npm run dev` (in the frontend repo)|

### Stop the app (uvicorn / the Python process)

If you started it with `--reload` in a terminal, just press `Ctrl-C` there.
If it's running in the background, kill it by name or by port:

```bash
# by process name
pkill -f "uvicorn app.main:app"

# or by whatever is listening on port 8000
lsof -ti :8000 | xargs kill        # add -9 to force
```

### Stop the Docker services (Postgres, pgAdmin, MySQL)

```bash
# stop the containers but KEEP the data volumes (fastest; data survives)
docker compose stop

# stop AND remove the containers (data volumes still survive)
docker compose down

# stop, remove containers AND delete all data (fresh start next time —
# you'll need to re-run migrations + seed)
docker compose down -v

# stop just one service
docker compose stop mysql      # or db, or pgadmin
```

### Stop a single container / free a stuck port

```bash
docker compose ps                      # list running services + their ports
docker stop botree_chat_backend-db-1   # stop one container by name
lsof -ti :5432 | xargs kill            # kill whatever holds a port (e.g. 5432)
```

### Quit Docker Desktop entirely (macOS)

```bash
osascript -e 'quit app "Docker"'
# or from the menu-bar whale icon → Quit Docker Desktop
```

> Order matters when shutting down for the day: `Ctrl-C`/`pkill` the backend
> first, then `docker compose stop`. To wipe and start clean, use
> `docker compose down -v` and re-run steps 5–6 (migrate + seed).
