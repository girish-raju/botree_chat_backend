"""Tests for `app/config.py` settings loading."""

from __future__ import annotations

from app.config import Settings


def test_mysql_settings_accept_legacy_db_env_names(monkeypatch) -> None:
    """The AWS server's pre-existing .env uses DB_* names (v13/v15 bots)."""
    monkeypatch.setenv("DB_HOST", "10.164.143.20")
    monkeypatch.setenv("DB_PORT", "3308")
    monkeypatch.setenv("DB_USER", "aasim_niazi")
    monkeypatch.setenv("DB_PASSWORD", "secret")
    monkeypatch.setenv("DB_NAME", "biskfarm_report_pp3")

    settings = Settings(_env_file=None)

    assert settings.mysql_host == "10.164.143.20"
    assert settings.mysql_port == 3308
    assert settings.mysql_user == "aasim_niazi"
    assert settings.mysql_password == "secret"
    assert settings.mysql_database == "biskfarm_report_pp3"


def test_mysql_names_win_over_legacy_db_names(monkeypatch) -> None:
    monkeypatch.setenv("DB_HOST", "legacy.host")
    monkeypatch.setenv("MYSQL_HOST", "explicit.host")

    settings = Settings(_env_file=None)

    assert settings.mysql_host == "explicit.host"


def test_mysql_defaults_without_any_env(monkeypatch) -> None:
    for name in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.mysql_host == "127.0.0.1"
    assert settings.mysql_port == 3306
