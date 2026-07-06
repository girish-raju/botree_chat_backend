"""The chat pipeline: orchestrates the whole NL->SQL->answer flow.

`ChatPipeline.run` is an async generator that emits `PipelineEvent`s as work
progresses, so the streaming endpoint can relay live text/tool frames to the
client. The flow (see `run`) is: greeting short-circuit -> follow-up rewrite ->
L0 (exact) cache -> L1 (semantic) cache -> LLM generation -> SQL safety guard ->
RBAC scoping -> L2 result cache / MySQL execution -> deterministic facts ->
streamed answer. Every terminal path writes exactly one `SqlAuditLog` row, and
any unexpected error is caught and turned into a friendly message rather than
leaking internals.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.embeddings import Embedder
from app.cache.normalizer import extract_temporal_intent, normalize_question
from app.cache.results import ResultCache, jsonable_rows, result_cache_key
from app.cache.semantic import QueryCache
from app.cache.templater import bind_template, parameterize_sql
from app.chat.answerer import build_facts, stream_answer_text
from app.chat.rewriter import maybe_rewrite
from app.config import Settings
from app.db.analytics import AnalyticsDB, get_analytics
from app.db.models import SqlAuditLog, User
from app.domain.formatting import GREETING_REPLY, is_greeting
from app.errors import RBACError, SQLSafetyError
from app.llm.base import LLMProvider, SQLPlan, Turn
from app.rbac.hierarchy import get_subtree_for
from app.rbac.injector import apply_scope
from app.rbac.profiles import profile_from_user, rbac_fingerprint
from app.sqlsafety.fixer import fix_column_aliases
from app.sqlsafety.guard import assert_safe
from app.sqlsafety.limiter import enforce_limit, tree_to_sql

logger = structlog.get_logger(__name__)

# Friendly, non-leaking user-facing messages.
_MSG_BLOCKED_SQL = (
    "I wasn't able to run that safely. Could you try rephrasing your question?"
)
_MSG_BLOCKED_RBAC = (
    "I can't show that data under your current access permissions."
)
_MSG_DB_ERROR = (
    "I couldn't reach the database, or the query took too long. "
    "Please try again in a moment."
)
_MSG_GENERIC_ERROR = (
    "Sorry, something went wrong while answering that. Please try again."
)


@dataclass
class TextDelta:
    """A chunk of natural-language answer text."""

    text: str


@dataclass
class ToolSQL:
    """The (safety-checked) SQL about to be executed."""

    sql: str


@dataclass
class ToolResult:
    """The executed query's result payload (columns/rows/etc.)."""

    payload: dict[str, Any]


@dataclass
class Done:
    """Terminal marker; the run has finished emitting events."""


PipelineEvent = TextDelta | ToolSQL | ToolResult | Done


class ChatPipeline:
    """Orchestrates cache lookups, SQL safety, RBAC, execution and answering."""

    def __init__(
        self,
        settings: Settings,
        provider: LLMProvider,
        query_cache: QueryCache,
        result_cache: ResultCache,
        embedder: Embedder,
        analytics: AnalyticsDB | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.query_cache = query_cache
        self.result_cache = result_cache
        self.embedder = embedder
        self._analytics = analytics

    def _get_analytics(self) -> AnalyticsDB:
        return self._analytics if self._analytics is not None else get_analytics()

    # -- SQL validation hook handed to the LLM for self-correction ----------
    @staticmethod
    def _validate(sql: str) -> str | None:
        """Guard-only validation hook: returns an error string, or None if ok.

        RBAC needs the user's subtree (resolved later), so the hook only runs
        the alias fixer + parse-time safety guard; full RBAC happens at the
        scoping step.
        """
        try:
            assert_safe(fix_column_aliases(sql))
            return None
        except SQLSafetyError as exc:
            return exc.message
        except Exception as exc:  # pragma: no cover - defensive
            return str(exc)

    async def run(
        self,
        *,
        user: User,
        thread_id: str | None,
        question: str,
        history: list[Turn],
        session: AsyncSession,
    ) -> AsyncIterator[PipelineEvent]:
        """Run the full pipeline, yielding `PipelineEvent`s as work proceeds."""
        started = time.monotonic()
        profile = profile_from_user(user)
        fingerprint = rbac_fingerprint(profile)

        rewritten = question
        cache_level: str | None = None
        raw_sql: str | None = None
        final_sql: str | None = None
        plan: SQLPlan | None = None

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            # 2. Greeting short-circuit — no LLM, no SQL.
            if is_greeting(question):
                yield TextDelta(GREETING_REPLY)
                await self._audit(
                    session, user=user, thread_id=thread_id, question=question,
                    status="ok", duration_ms=elapsed_ms(),
                )
                yield Done()
                return

            # 3. Resolve follow-up questions against history.
            rewritten, _was_rewritten = await maybe_rewrite(
                self.provider, history, question
            )

            # 4. Normalize + temporal intent (off the resolved question).
            normalized = normalize_question(rewritten)
            temporal = extract_temporal_intent(rewritten)

            template: str | None = None
            params: dict[str, Any] | None = None
            from_cache = False
            question_embedding: list[float] | None = None

            # 5. L0 exact cache.
            entry = await self.query_cache.lookup_exact(session, normalized)
            if entry is not None:
                template = entry.sql_template
                params = entry.params_spec
                cache_level = "L0"
                from_cache = True
            else:
                # 6. L1 semantic cache.
                question_embedding = await self.embedder.encode(rewritten)
                hit = await self.query_cache.lookup_semantic(
                    session, question_embedding, temporal, self.settings.semantic_threshold
                )
                if hit is not None:
                    sem_entry, _similarity = hit
                    template = sem_entry.sql_template
                    params = sem_entry.params_spec
                    await self.query_cache.add_alias(session, normalized, sem_entry)
                    cache_level = "L1"
                    from_cache = True
                else:
                    # 7. MISS -> LLM generation.
                    plan = await self.provider.generate_sql(
                        rewritten, history, validate=self._validate
                    )
                    if plan.mode == "general":
                        yield TextDelta(plan.answer or "")
                        await self._audit(
                            session, user=user, thread_id=thread_id, question=question,
                            rewritten=rewritten, status="ok", cache_level="llm",
                            tokens_in=plan.tokens_in, tokens_out=plan.tokens_out,
                            duration_ms=elapsed_ms(),
                        )
                        yield Done()
                        return
                    raw_sql = plan.sql
                    cache_level = "llm"

            # 8. Bind + safety.
            if from_cache:
                sql = bind_template(template or "", params, rewritten)
            else:
                sql = raw_sql or ""
            sql = fix_column_aliases(sql)
            try:
                tree = assert_safe(sql)
                tree = enforce_limit(tree, self.settings.sql_row_cap)
                safe_sql = tree_to_sql(tree)
            except SQLSafetyError as exc:
                yield TextDelta(_MSG_BLOCKED_SQL)
                await self._audit(
                    session, user=user, thread_id=thread_id, question=question,
                    rewritten=rewritten, generated_sql=raw_sql or sql,
                    cache_level=cache_level, status="blocked", error=exc.message,
                    duration_ms=elapsed_ms(),
                )
                yield Done()
                return

            yield ToolSQL(safe_sql)

            # 9. RBAC scoping.
            try:
                subtree = None if profile.is_unrestricted else await get_subtree_for(profile)
                final_sql = apply_scope(safe_sql, profile, subtree)
            except RBACError as exc:
                yield TextDelta(_MSG_BLOCKED_RBAC)
                await self._audit(
                    session, user=user, thread_id=thread_id, question=question,
                    rewritten=rewritten, generated_sql=raw_sql, cache_level=cache_level,
                    status="blocked", error=exc.message, duration_ms=elapsed_ms(),
                )
                yield Done()
                return

            # 10. Result cache -> execute.
            key = result_cache_key(final_sql, fingerprint)
            cached = await self.result_cache.get(session, key)
            if cached is not None:
                columns = list(cached.columns)
                rows = list(cached.rows)
                served_cached = True
            else:
                try:
                    qr = await self._get_analytics().execute_readonly(final_sql)
                except Exception as exc:
                    logger.warning("analytics_execute_failed", error=str(exc))
                    yield TextDelta(_MSG_DB_ERROR)
                    await self._audit(
                        session, user=user, thread_id=thread_id, question=question,
                        rewritten=rewritten, generated_sql=raw_sql, final_sql=final_sql,
                        cache_level=cache_level, status="error", error=str(exc),
                        duration_ms=elapsed_ms(),
                    )
                    yield Done()
                    return

                columns = qr.columns
                rows = qr.rows
                served_cached = False
                try:
                    await self.result_cache.put(
                        session, key, columns, rows, qr.row_count,
                        self.settings.result_cache_ttl_s,
                    )
                except Exception:
                    logger.warning("result_cache_put_failed", exc_info=True)
                    await _safe_rollback(session)

                # Flywheel: cache a successfully-executed LLM-generated query —
                # but ONLY when it returned at least one row. A query that
                # returns 0 rows is either genuinely empty OR subtly wrong (e.g.
                # a hallucinated filter); caching it would lock in an answer of
                # "no data" for everyone and survive across retries. Skipping it
                # means the next ask regenerates and gets another chance.
                if raw_sql is not None and len(rows) > 0:
                    try:
                        template_sql, spec = parameterize_sql(raw_sql)
                        if question_embedding is None:
                            question_embedding = await self.embedder.encode(rewritten)
                        await self.query_cache.store(
                            session,
                            normalized_q=normalized,
                            embedding=question_embedding,
                            sql_template=template_sql,
                            params_spec=spec,
                            temporal_intent=temporal,
                            created_by=user.id,
                        )
                    except Exception:
                        logger.warning("query_cache_store_failed", exc_info=True)
                        await _safe_rollback(session)

            # 11. Emit the tool result payload.
            yield ToolResult(
                {
                    "sql": final_sql,
                    "columns": columns,
                    "rows": jsonable_rows(rows)[:200],
                    "row_count": len(rows),
                    "cached": served_cached,
                }
            )

            # 12. Deterministic facts + streamed natural-language answer.
            facts = await build_facts(question, columns, rows)
            async for delta in stream_answer_text(
                self.provider, rewritten, facts, columns, rows
            ):
                yield TextDelta(delta)

            # 13. Done + audit.
            await self._audit(
                session, user=user, thread_id=thread_id, question=question,
                rewritten=rewritten, generated_sql=raw_sql, final_sql=final_sql,
                cache_level=cache_level, row_count=len(rows), status="ok",
                tokens_in=plan.tokens_in if plan else None,
                tokens_out=plan.tokens_out if plan else None,
                duration_ms=elapsed_ms(),
            )
            yield Done()

        except Exception as exc:  # any unexpected failure -> friendly message
            logger.exception("pipeline_unexpected_error")
            yield TextDelta(_MSG_GENERIC_ERROR)
            try:
                await self._audit(
                    session, user=user, thread_id=thread_id, question=question,
                    rewritten=rewritten, generated_sql=raw_sql, final_sql=final_sql,
                    cache_level=cache_level, status="error", error=str(exc),
                    duration_ms=elapsed_ms(),
                )
            except Exception:  # pragma: no cover - audit must never mask the error
                logger.warning("audit_write_failed", exc_info=True)
            yield Done()

    async def _audit(
        self,
        session: AsyncSession,
        *,
        user: User,
        thread_id: str | None,
        question: str,
        rewritten: str | None = None,
        generated_sql: str | None = None,
        final_sql: str | None = None,
        cache_level: str | None = None,
        row_count: int | None = None,
        duration_ms: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        status: str,
        error: str | None = None,
    ) -> None:
        """Write exactly one `SqlAuditLog` row for this run. Never raises."""
        try:
            session.add(
                SqlAuditLog(
                    user_id=user.id,
                    thread_id=_as_uuid(thread_id),
                    question=question,
                    rewritten_question=rewritten if rewritten != question else None,
                    generated_sql=generated_sql,
                    final_sql=final_sql,
                    cache_level=cache_level,
                    row_count=row_count,
                    duration_ms=duration_ms,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    status=status,
                    error=error,
                )
            )
            await session.flush()
        except Exception:  # pragma: no cover - auditing must not fail the request
            logger.warning("audit_flush_failed", exc_info=True)
            await _safe_rollback(session)


async def _safe_rollback(session: AsyncSession) -> None:
    """Roll back the session, swallowing any error. Used to recover a session
    left in a failed-flush state by a best-effort cache/audit write so that
    later writes (and the request-scoped commit) still succeed."""
    try:
        await session.rollback()
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.warning("pipeline_rollback_failed", exc_info=True)


def _as_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


__all__ = [
    "ChatPipeline",
    "PipelineEvent",
    "TextDelta",
    "ToolSQL",
    "ToolResult",
    "Done",
]
