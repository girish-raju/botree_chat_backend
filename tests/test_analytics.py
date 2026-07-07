"""Tests for the MySQL analytics access layer (`app/db/analytics.py`).

Everything here runs fully offline: no real SSH or MySQL connection is ever
attempted. `SSHTunnelForwarder`, `PooledDB`/`pymysql` connections are all
mocked out.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pymysql

from app.config import Settings
from app.db.analytics import AnalyticsDB, QueryResult, SSHTunnelManager


def _settings(**overrides: object) -> Settings:
    base = {
        "mysql_host": "mysql.internal",
        "mysql_port": 3306,
        "mysql_user": "u",
        "mysql_password": "p",
        "mysql_database": "analytics",
        "mysql_query_timeout_s": 15,
        "ssh_host": "",
        "ssh_port": 22,
        "ssh_user": "sshuser",
        "ssh_key_path": "/tmp/does-not-exist.pem",
        "ssh_key_password": "",
    }
    base.update(overrides)
    return Settings(**base)


# --- SSHTunnelManager -------------------------------------------------------


def test_no_ssh_host_returns_mysql_host_directly() -> None:
    settings = _settings(ssh_host="")
    manager = SSHTunnelManager(settings)

    with patch("app.db.analytics.SSHTunnelForwarder") as mock_forwarder_cls:
        host, port = manager.ensure_started()

    assert (host, port) == ("mysql.internal", 3306)
    mock_forwarder_cls.assert_not_called()


def test_ssh_host_set_lazy_starts_tunnel_once() -> None:
    settings = _settings(ssh_host="bastion.internal")
    manager = SSHTunnelManager(settings)

    mock_instance = MagicMock()
    mock_instance.is_active = True
    mock_instance.ssh_transport.is_active.return_value = True
    mock_instance.local_bind_port = 54321

    with patch(
        "app.db.analytics.SSHTunnelForwarder", return_value=mock_instance
    ) as mock_cls:
        host1, port1 = manager.ensure_started()
        host2, port2 = manager.ensure_started()

    assert (host1, port1) == ("127.0.0.1", 54321)
    assert (host2, port2) == ("127.0.0.1", 54321)
    mock_cls.assert_called_once()
    mock_instance.start.assert_called_once()


def test_tunnel_restarts_when_dead() -> None:
    settings = _settings(ssh_host="bastion.internal")
    manager = SSHTunnelManager(settings)

    dead_instance = MagicMock()
    dead_instance.is_active = False
    dead_instance.local_bind_port = 11111

    alive_instance = MagicMock()
    alive_instance.is_active = True
    alive_instance.ssh_transport.is_active.return_value = True
    alive_instance.local_bind_port = 22222

    with patch(
        "app.db.analytics.SSHTunnelForwarder",
        side_effect=[dead_instance, alive_instance],
    ) as mock_cls:
        host1, port1 = manager.ensure_started()
        # Simulate the tunnel dying between calls.
        dead_instance.is_active = False
        host2, port2 = manager.ensure_started()

    assert (host1, port1) == ("127.0.0.1", 11111)
    assert (host2, port2) == ("127.0.0.1", 22222)
    assert mock_cls.call_count == 2
    dead_instance.stop.assert_called_once()


# --- AnalyticsDB.execute_readonly_sync --------------------------------------


class _FakeCursor:
    """Minimal DictCursor-like fake cursor."""

    def __init__(self, description: list[tuple], rows: list[dict], executed: list[str]) -> None:
        self.description = description
        self._rows = rows
        self._executed = executed
        self.fail_on_set_session = False

    def execute(self, sql: str, *args: object, **kwargs: object) -> None:
        self._executed.append(sql)
        if self.fail_on_set_session and sql.startswith("SET SESSION"):
            raise pymysql.err.OperationalError(1193, "Unknown system variable")

    def fetchall(self) -> list[dict]:
        return self._rows

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def _analytics_with_fake_pool(rows: list[dict], columns: list[str]) -> tuple[AnalyticsDB, list[str]]:
    settings = _settings()
    db = AnalyticsDB(settings)

    executed: list[str] = []
    description = [(c, None, None, None, None, None, None) for c in columns]
    cursor = _FakeCursor(description, rows, executed)
    conn = _FakeConnection(cursor)

    fake_pool = MagicMock()
    fake_pool.connection.return_value = conn
    db._pool = fake_pool
    db._pool_target = ("mysql.internal", 3306)

    return db, executed


def test_execute_readonly_sync_returns_columns_rows_and_timing() -> None:
    rows = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    db, executed = _analytics_with_fake_pool(rows, ["id", "name"])

    result = db.execute_readonly_sync("SELECT id, name FROM t", timeout_s=7)

    assert isinstance(result, QueryResult)
    assert result.columns == ["id", "name"]
    assert result.rows == rows
    assert result.row_count == 2
    assert result.duration_ms >= 0
    assert any("MAX_EXECUTION_TIME=7000" in stmt for stmt in executed)
    assert any("TRANSACTION READ ONLY" in stmt for stmt in executed)


def test_execute_readonly_sync_ignores_set_session_failure() -> None:
    db, _ = _analytics_with_fake_pool([{"x": 1}], ["x"])
    db._pool.connection.return_value.cursor().fail_on_set_session = True

    result = db.execute_readonly_sync("SELECT x", timeout_s=5)

    assert result.rows == [{"x": 1}]


def test_execute_readonly_sync_retries_once_on_dead_connection() -> None:
    settings = _settings()
    db = AnalyticsDB(settings)

    good_rows = [{"ok": 1}]
    good_description = [("ok", None, None, None, None, None, None)]

    call_count = {"n": 0}

    def fake_ensure_pool() -> MagicMock:
        call_count["n"] += 1
        if call_count["n"] == 1:
            pool = MagicMock()
            conn = MagicMock()
            cursor = MagicMock()
            cursor.__enter__.return_value = cursor
            cursor.__exit__.return_value = False
            cursor.execute.side_effect = pymysql.err.OperationalError(2013, "Lost connection")
            conn.cursor.return_value = cursor
            pool.connection.return_value = conn
            return pool
        pool = MagicMock()
        conn = MagicMock()
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.__exit__.return_value = False
        cursor.description = good_description
        cursor.fetchall.return_value = good_rows
        conn.cursor.return_value = cursor
        pool.connection.return_value = conn
        return pool

    db._ensure_pool = fake_ensure_pool  # type: ignore[method-assign]

    result = db.execute_readonly_sync("SELECT 1", timeout_s=5)

    assert result.rows == good_rows
    assert call_count["n"] == 2


# --- AnalyticsDB.ready -------------------------------------------------------


async def test_ready_true_when_query_succeeds() -> None:
    db, _ = _analytics_with_fake_pool([{"1": 1}], ["1"])
    assert await db.ready() is True


async def test_ready_false_when_query_raises() -> None:
    settings = _settings()
    db = AnalyticsDB(settings)

    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    db.execute_readonly_sync = _raise  # type: ignore[method-assign]

    assert await db.ready() is False


# --- App boot without MySQL --------------------------------------------------


def test_app_imports_and_boots_without_mysql() -> None:
    """Importing/creating the app must not touch the network (all lazy)."""
    from app.main import create_app

    app = create_app()
    assert app is not None
