"""Unit tests for kald_devops.sqlserver_util.

Covers Application's pure env-var and connection-string helpers. No real
pyodbc connection is made anywhere in this file.
"""
import argparse

import pytest

from kald_devops import sqlserver_util


def _make_app(**json_flag):
    args = argparse.Namespace(json=json_flag.get("json", False))
    return sqlserver_util.Application(args)


def test_require_env_returns_value_when_set(monkeypatch):
    monkeypatch.setenv("DB_SERVER", "db.example.com")
    app = _make_app()
    assert app._require_env(app.ENV_DB_SERVER) == "db.example.com"


def test_require_env_exits_when_missing(monkeypatch):
    monkeypatch.delenv("DB_SERVER", raising=False)
    app = _make_app()
    with pytest.raises(SystemExit) as exc_info:
        app._require_env(app.ENV_DB_SERVER)
    assert exc_info.value.code == 1


def test_require_env_exits_when_empty(monkeypatch):
    monkeypatch.setenv("DB_SERVER", "")
    app = _make_app()
    with pytest.raises(SystemExit):
        app._require_env(app.ENV_DB_SERVER)


def test_optional_env_returns_value_when_set(monkeypatch):
    monkeypatch.setenv("DB_PORT", "1434")
    app = _make_app()
    assert app._optional_env(app.ENV_DB_PORT, "1433") == "1434"


def test_optional_env_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("DB_PORT", raising=False)
    app = _make_app()
    assert app._optional_env(app.ENV_DB_PORT, "1433") == "1433"


def test_build_connection_string_uses_required_and_default_values(monkeypatch):
    monkeypatch.setenv("DB_SERVER", "db.example.com")
    monkeypatch.setenv("DB_NAME", "kpay")
    monkeypatch.setenv("DB_USER", "svc-account")
    monkeypatch.setenv("DB_PASSWORD", "super-secret")
    monkeypatch.delenv("DB_PORT", raising=False)
    monkeypatch.delenv("DB_DRIVER", raising=False)

    app = _make_app()
    conn_str = app._build_connection_string()

    assert "SERVER=db.example.com,1433;" in conn_str
    assert "DATABASE=kpay;" in conn_str
    assert "UID=svc-account;" in conn_str
    assert "PWD=super-secret;" in conn_str
    assert f"DRIVER={{{app.DEFAULT_DRIVER}}};" in conn_str


def test_build_connection_string_honors_overrides(monkeypatch):
    monkeypatch.setenv("DB_SERVER", "db.example.com")
    monkeypatch.setenv("DB_PORT", "1500")
    monkeypatch.setenv("DB_NAME", "kpay")
    monkeypatch.setenv("DB_USER", "svc-account")
    monkeypatch.setenv("DB_PASSWORD", "super-secret")
    monkeypatch.setenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")

    app = _make_app()
    conn_str = app._build_connection_string()

    assert "SERVER=db.example.com,1500;" in conn_str
    assert "DRIVER={ODBC Driver 17 for SQL Server};" in conn_str


def test_build_connection_string_exits_when_a_required_var_is_missing(monkeypatch):
    monkeypatch.setenv("DB_SERVER", "db.example.com")
    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.setenv("DB_USER", "svc-account")
    monkeypatch.setenv("DB_PASSWORD", "super-secret")

    app = _make_app()
    with pytest.raises(SystemExit):
        app._build_connection_string()


def test_run_dispatches_to_known_subcommand(monkeypatch):
    called = {}

    def fake_cmd_connect():
        called["ran"] = True
        return 0

    args = argparse.Namespace(command="connect", json=False)
    app = sqlserver_util.Application(args)
    monkeypatch.setattr(app, "cmd_connect", fake_cmd_connect)
    assert app.run() == 0
    assert called.get("ran") is True


def test_run_returns_error_for_unknown_subcommand():
    args = argparse.Namespace(command="not_a_real_command", json=False)
    app = sqlserver_util.Application(args)
    assert app.run() == 1
