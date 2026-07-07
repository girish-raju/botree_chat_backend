"""MySQL analytics access layer, reached through an SSH tunnel when configured.

`init_analytics` / `dispose_analytics` are called from the FastAPI lifespan
(see `app/main.py`), mirroring `app/db/postgres.py`. Everything is lazy: no
network connection (SSH or MySQL) is attempted until the first query, so the
app can boot even when the analytics host is unreachable. `analytics_ready`
(the `AnalyticsDB.ready` bound method) is registered into
`app.api.health.readiness_probes` as the "mysql" check.

SSH handling mirrors the original conversational bot: if `SSH_HOST` is set,
a single lazily-started tunnel (key + optional passphrase, keepalive) is
reused while alive and restarted if it dies; if `SSH_HOST` is empty, pymysql
connects to `MYSQL_HOST:MYSQL_PORT` directly. There is no separate
enable/disable flag.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
import paramiko
import pymysql
import structlog
from dbutils.pooled_db import PooledDB

# Compatibility shim: paramiko >= 4 removed the deprecated `DSSKey` (DSA keys),
# but sshtunnel 0.4.0 still references `paramiko.DSSKey` at import time, which
# raises AttributeError. Alias it to RSAKey so sshtunnel imports; we never use
# DSA keys (they're insecure and unsupported), so this only satisfies the
# attribute lookup and changes no runtime behavior for RSA/Ed25519 keys.
if not hasattr(paramiko, "DSSKey"):
    paramiko.DSSKey = paramiko.RSAKey  # type: ignore[attr-defined]

from sshtunnel import SSHTunnelForwarder  # noqa: E402  (import after the shim)

from app.config import Settings

logger = structlog.get_logger(__name__)

#: pymysql/MySQL error codes that indicate a dead connection or dead tunnel,
#: worth invalidating the pool and retrying once.
_DEAD_CONNECTION_ERROR_CODES = frozenset({2003, 2006, 2013})


@dataclass
class QueryResult:
    """Result of a read-only analytics query."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    duration_ms: int


@dataclass
class SSHTunnelManager:
    """Lazily starts a single `sshtunnel.SSHTunnelForwarder` on demand.

    Active whenever `settings.ssh_host` is set: the tunnel is started on the
    first call, reused while alive, and restarted if it has died — the same
    pattern as `start_ssh_tunnel()` in the original conversational bot. When
    no SSH host is configured, `ensure_started` hands back the MySQL
    host/port directly with no tunnel involved.
    """

    settings: Settings
    _forwarder: SSHTunnelForwarder | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _is_alive(self) -> bool:
        forwarder = self._forwarder
        if forwarder is None:
            return False
        try:
            if not forwarder.is_active:
                return False
            transport = forwarder.ssh_transport
            return bool(transport is not None and transport.is_active())
        except Exception:
            return False

    def _start_forwarder(self) -> SSHTunnelForwarder:
        settings = self.settings
        logger.info("ssh_tunnel_starting", ssh_host=settings.ssh_host)
        forwarder = SSHTunnelForwarder(
            (settings.ssh_host, settings.ssh_port),
            ssh_username=settings.ssh_user,
            ssh_pkey=settings.ssh_key_path or None,
            ssh_private_key_password=settings.ssh_key_password or None,
            remote_bind_address=(settings.mysql_host, settings.mysql_port),
            local_bind_address=("127.0.0.1", 0),
            set_keepalive=10,
        )
        forwarder.start()
        logger.info(
            "ssh_tunnel_started",
            local_bind_port=forwarder.local_bind_port,
            ssh_host=settings.ssh_host,
        )
        return forwarder

    def ensure_started(self) -> tuple[str, int]:
        """Return the (host, port) MySQL should connect to.

        If `ssh_host` is configured, lazily starts the forwarder on first
        call and restarts it if a previously-started tunnel has died. If not,
        returns the configured MySQL host/port unchanged.
        """
        if not self.settings.ssh_host:
            return self.settings.mysql_host, self.settings.mysql_port

        with self._lock:
            if not self._is_alive():
                if self._forwarder is not None:
                    logger.warning("ssh_tunnel_dead_restarting")
                    self._stop_locked()
                self._forwarder = self._start_forwarder()

            assert self._forwarder is not None
            return "127.0.0.1", self._forwarder.local_bind_port

    def _stop_locked(self) -> None:
        if self._forwarder is not None:
            try:
                self._forwarder.stop()
            except Exception:
                logger.exception("ssh_tunnel_stop_failed")
            self._forwarder = None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()


class AnalyticsDB:
    """Owns the SSH tunnel (if any) and a pooled set of PyMySQL connections."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tunnel = SSHTunnelManager(settings)
        self._pool: PooledDB | None = None
        self._pool_target: tuple[str, int] | None = None
        self._pool_lock = threading.Lock()

    def _build_pool(self, host: str, port: int) -> PooledDB:
        settings = self._settings
        return PooledDB(
            creator=pymysql,
            mincached=0,
            maxcached=4,
            maxconnections=8,
            blocking=True,
            host=host,
            port=port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
            autocommit=True,
        )

    def _ensure_pool(self) -> PooledDB:
        host, port = self._tunnel.ensure_started()
        with self._pool_lock:
            if self._pool is None or self._pool_target != (host, port):
                self._pool = self._build_pool(host, port)
                self._pool_target = (host, port)
                logger.info("analytics_pool_built", host=host, port=port)
            return self._pool

    def _invalidate_pool(self) -> None:
        with self._pool_lock:
            self._pool = None
            self._pool_target = None
        self._tunnel.stop()

    def execute_readonly_sync(self, sql: str, timeout_s: int) -> QueryResult:
        """Run a read-only query against the analytics MySQL database.

        Retries once, rebuilding the pool and tunnel, if the connection or
        tunnel appears to have died mid-flight.
        """
        try:
            return self._execute_once(sql, timeout_s)
        except (pymysql.err.OperationalError, OSError) as exc:
            if not self._is_dead_connection_error(exc):
                raise
            logger.warning("analytics_connection_dead_retrying", error=str(exc))
            self._invalidate_pool()
            return self._execute_once(sql, timeout_s)

    @staticmethod
    def _is_dead_connection_error(exc: Exception) -> bool:
        if isinstance(exc, pymysql.err.OperationalError):
            code = exc.args[0] if exc.args else None
            if code in _DEAD_CONNECTION_ERROR_CODES:
                return True
        return isinstance(exc, OSError)

    def _execute_once(self, sql: str, timeout_s: int) -> QueryResult:
        pool = self._ensure_pool()
        conn = pool.connection()
        try:
            with conn.cursor() as cursor:
                try:
                    cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={int(timeout_s * 1000)}")
                except Exception:
                    logger.debug("max_execution_time_unsupported", exc_info=True)

                try:
                    cursor.execute("SET SESSION TRANSACTION READ ONLY")
                except Exception:
                    logger.debug("transaction_read_only_unsupported", exc_info=True)

                start = time.monotonic()
                cursor.execute(sql)
                rows = cursor.fetchall()
                duration_ms = int((time.monotonic() - start) * 1000)

                columns = [col[0] for col in cursor.description] if cursor.description else []
                return QueryResult(
                    columns=columns,
                    rows=list(rows),
                    row_count=len(rows),
                    duration_ms=duration_ms,
                )
        finally:
            conn.close()

    async def execute_readonly(self, sql: str, timeout_s: int | None = None) -> QueryResult:
        """Async wrapper around `execute_readonly_sync`, run in a worker thread."""
        effective_timeout = timeout_s if timeout_s is not None else self._settings.mysql_query_timeout_s
        return await anyio.to_thread.run_sync(self.execute_readonly_sync, sql, effective_timeout)

    async def ready(self) -> bool:
        """Readiness probe: returns True if a trivial query succeeds."""
        try:
            await self.execute_readonly("SELECT 1", timeout_s=5)
            return True
        except Exception:
            logger.exception("analytics_readiness_check_failed")
            return False

    def close(self) -> None:
        """Close the pool (if any) and stop the SSH tunnel (if any)."""
        with self._pool_lock:
            pool = self._pool
            self._pool = None
            self._pool_target = None
        if pool is not None:
            try:
                pool.close()
            except Exception:
                logger.exception("analytics_pool_close_failed")
        self._tunnel.stop()


_analytics: AnalyticsDB | None = None


def init_analytics(settings: Settings) -> AnalyticsDB:
    """Create the module-level `AnalyticsDB`. Does not eagerly connect."""
    global _analytics

    _analytics = AnalyticsDB(settings)
    logger.info("analytics_db_initialized")
    return _analytics


def dispose_analytics() -> None:
    """Close the module-level `AnalyticsDB`, if initialized."""
    global _analytics

    if _analytics is not None:
        _analytics.close()
        logger.info("analytics_db_disposed")
    _analytics = None


def get_analytics() -> AnalyticsDB:
    """Return the module-level `AnalyticsDB`, raising if not initialized."""
    if _analytics is None:
        raise RuntimeError("Analytics DB not initialized. Call init_analytics() first.")
    return _analytics


__all__ = [
    "AnalyticsDB",
    "QueryResult",
    "SSHTunnelManager",
    "init_analytics",
    "dispose_analytics",
    "get_analytics",
]
