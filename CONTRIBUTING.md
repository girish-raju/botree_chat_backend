# Contributing

Practical guide for working on this codebase. See `SETUP.md` to get running
first, and `CODEBASE.md` for where things live.

## Branching and PRs

- Branch off `main`; name branches descriptively (`fix/rbac-union-branches`,
  `feat/cloudflare-retry-backoff`) — there's no enforced prefix scheme, just
  be clear about what the branch does.
- Keep PRs small and focused on one change. A safety-critical change (guard,
  RBAC, cache correctness) should be its own PR, reviewed on its own —
  don't bundle it with unrelated refactoring.
- Every PR should be reviewed before merging. If you touch
  `app/sqlsafety/`, `app/rbac/`, or the cache-keying logic in `app/cache/`,
  call that out explicitly in the PR description — these are the modules
  where a subtle regression is a security bug, not just a bug.
- **Secrets never get committed.** `.env` is git-ignored (see `.gitignore`);
  only `.env.example` — with placeholder values, never real credentials — is
  tracked. Before pushing, check `git status`/`git diff` for anything that
  looks like a real API key, SSH key, or DB password, even in files that
  look innocuous (a stray `.env.local`, a pasted curl command in a comment).

## Local dev loop

```bash
source .venv/bin/activate
uvicorn app.main:app --reload      # terminal 1: the app, reload-on-change
pytest -q                          # terminal 2: fast feedback loop
ruff check . && ruff format --check .
```

Most iteration should be possible against the offline test suite alone —
you generally don't need Docker/Postgres/MySQL running just to add a unit
test for a pure function in `app/domain/`, `app/sqlsafety/`, or `app/rbac/`.

## Testing expectations

- **Unit tests must pass fully offline** — no external services, no network
  calls, no API keys. `pytest -q` (242 tests today) should always be green
  with nothing running except the test process itself.
- This works because `tests/conftest.py` builds every test's Postgres access
  against an **in-memory SQLite** engine (`Base.metadata.create_all` over
  every table except `query_cache`, whose `pgvector` column type doesn't
  compile on SQLite — see the docstring in `app/db/models.py` and
  `tests/conftest.py`). Anything that needs real pgvector nearest-neighbor
  search (`app.cache.semantic.QueryCache.lookup_semantic`) is instead tested
  against a **mocked `AsyncSession`**, verifying the SQL-building and
  threshold/temporal-veto decision logic rather than pgvector itself.
- LLM calls, MySQL/SSH, and the sentence-transformers embedding model are
  always faked/mocked in unit tests (see `tests/test_llm_providers.py`,
  `tests/test_analytics.py`) — never make a real network call from a test
  that runs by default.
- **Every new safety or RBAC rule needs a table-driven or golden test.**
  `tests/test_sqlsafety.py` and `tests/test_rbac_injector.py` are the
  reference style: parametrized cases for guard rules, golden-SQL
  comparisons (normalized through sqlglot so formatting differences don't
  cause spurious failures) for RBAC scoping. If you add a check to
  `app/sqlsafety/guard.py` or a scoping rule to `app/rbac/injector.py`, add
  the corresponding case(s) in the same style, including the case that
  should still be *allowed* (a safety/RBAC test suite that only tests
  rejections can hide an accidental over-block regression too).
- See `TEST_PLAN.md` for the full case catalog (by ID, e.g. `SAFE-08`,
  `RBAC-03`) across auth, threads, SQL safety, RBAC, caching, LLM providers,
  streaming, security, and performance — useful both as a reference for
  existing coverage and as a checklist when adding a feature in one of these
  areas.

## Code style

- `ruff check .` and `ruff format --check .` before committing (line length
  100, target `py311`, config in `pyproject.toml`). Run `ruff format .` to
  auto-fix formatting and `ruff check --fix .` for auto-fixable lint issues.
- Type hints everywhere; the codebase uses `from __future__ import
  annotations` and modern syntax (`str | None`, not `Optional[str]`).
- SQLAlchemy 2.0 `Mapped[...]` / `mapped_column(...)` declarative style for
  every model (see `app/db/models.py`) — not the legacy `Column(...)` style.
- Use `structlog.get_logger(__name__)` for logging, never `print`. Log with
  structured kwargs (`logger.warning("analytics_execute_failed", error=str(exc))`),
  not f-string messages, so log lines stay machine-parseable.

## The fail-closed rule

Anything touching SQL safety (`app/sqlsafety/`) or RBAC (`app/rbac/`) must
**fail closed**: when a check can't prove something is safe/correctly
scoped, it must block the query and raise (`SQLSafetyError` / `RBACError`),
never fall through to a permissive default. This is deliberate in the
existing code — e.g. `app/rbac/injector.py`'s `_scope_table` raises
`RBACError` rather than silently omitting a predicate it can't enforce.
When in doubt, block + let the audit log record why (every terminal path in
`app/chat/pipeline.py` writes a `SqlAuditLog` row with the block reason) —
never guess towards "probably fine."

## Adding a migration

1. Change the ORM model(s) in `app/db/models.py`.
2. Generate a migration:
   ```bash
   alembic revision --autogenerate -m "add whatever_column to whatever_table"
   ```
3. **Read the generated file in `alembic/versions/` before committing it.**
   Autogenerate is a starting point, not a guarantee — check that:
   - New non-nullable columns on existing tables have a `server_default` (or
     a data migration step), or the upgrade will fail against real data.
   - Index/constraint names match the project's existing convention (see
     `alembic/versions/0001_initial.py`, e.g. `ix_<table>_<columns>`).
   - The `downgrade()` actually reverses the `upgrade()`.
4. Test it locally: `alembic upgrade head` against your dev Postgres, then
   `alembic downgrade -1` to confirm the downgrade path works too.

## Commit message hygiene

- Concise, imperative subject line describing *why*, not just *what*
  ("fix RBAC fail-open on unioned subqueries", not "update injector.py").
- Reference the `TEST_PLAN.md` case ID when a commit closes one out (e.g.
  "implement RBAC-06: scope every UNION branch").
- Keep unrelated changes out of the commit — if you touched formatting in a
  file you weren't otherwise editing, that's a separate commit.
