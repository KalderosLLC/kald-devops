"""Mocked unit tests for kald_devops.postgres_util.PostgresConfigManager.

This is the class that actually opens database connections and runs SQL
against real environments -- cmd_apply_pr in particular is the highest-
consequence code path in the whole repo (it can commit real changes to a
production database). Every test here mocks psycopg2/subprocess/GitHub
fetches; nothing here opens a real connection, spawns a real psql process,
or touches the real ~/.kald-postgres-util.json (CONFIG_PATH is redirected to
a temp file by the autouse fixture below).
"""
import os
from unittest.mock import MagicMock

import psycopg2
import pytest

from kald_devops import postgres_util as pu

# Every env var any PostgresConfigManager method reads. Deleted before each
# test so a real value sitting in the developer's own shell (as happened
# with PIPELINES/AZURE_DEVOPS_EXT_PAT during the devops.py test work) can't
# leak into a test and change its outcome.
_ISOLATED_ENV_VARS = [
    "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
    "FILE", "COMMIT", "PR", "GITHUB_TOKEN", "SQL_FILE",
]

REQUIRED_ENV = {"DB_HOST": "db.example.com", "DB_USER": "svc", "DB_PASSWORD": "secret"}

CLEAN_SQL = (
    "-- Expected Results:\n"
    "-- 2 rows updated in kpay.foo\n"
    "UPDATE kpay.foo SET x = 1;\n"
)

SELF_MANAGED_TXN_SQL = "BEGIN;\nUPDATE kpay.foo SET x = 1;\nCOMMIT;\n"

PR_URL = "https://github.com/KalderosLLC/phoenix-governance/pull/1"


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(pu, "CONFIG_PATH", str(tmp_path / "kald-postgres-util.json"))
    for var in _ISOLATED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def manager():
    return pu.PostgresConfigManager()


def _store_valid_config(environment="qa"):
    store = {
        environment: {
            "DB_HOST": "h", "DB_USER": "u", "DB_PASSWORD": "p",
            "DB_NAME": "n", "DB_PORT": "5432",
        }
    }
    pu.save_config_store(store)


# ---------------------------------------------------------------------------
# _collect_from_os_env
# ---------------------------------------------------------------------------

def test_collect_from_os_env_missing_required_returns_none(manager):
    assert manager._collect_from_os_env() is None


def test_collect_from_os_env_applies_optional_defaults(monkeypatch, manager):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    config = manager._collect_from_os_env()
    assert config["DB_HOST"] == "db.example.com"
    assert config["DB_NAME"] == "citus"
    assert config["DB_PORT"] == "5432"


def test_collect_from_os_env_honors_explicit_optional_values(monkeypatch, manager):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DB_NAME", "mydb")
    monkeypatch.setenv("DB_PORT", "6432")
    config = manager._collect_from_os_env()
    assert config["DB_NAME"] == "mydb"
    assert config["DB_PORT"] == "6432"


# ---------------------------------------------------------------------------
# _validate_config
# ---------------------------------------------------------------------------

def test_validate_config_true_when_all_required_present(manager):
    assert manager._validate_config({"DB_HOST": "h", "DB_USER": "u", "DB_PASSWORD": "p"}) is True


def test_validate_config_false_when_missing_required(manager):
    assert manager._validate_config({"DB_HOST": "h", "DB_USER": "u", "DB_PASSWORD": ""}) is False


# ---------------------------------------------------------------------------
# _load_config
# ---------------------------------------------------------------------------

def test_load_config_missing_environment_returns_none(manager):
    assert manager._load_config("prod") is None


def test_load_config_returns_config_for_known_environment(manager):
    _store_valid_config("prod")
    config = manager._load_config("prod")
    assert config["DB_HOST"] == "h"
    assert config["DB_PORT"] == "5432"


def test_load_config_invalid_stored_config_returns_none(manager):
    pu.save_config_store({"prod": {"DB_HOST": "h", "DB_USER": "u"}})  # no DB_PASSWORD
    assert manager._load_config("prod") is None


# ---------------------------------------------------------------------------
# _connect
# ---------------------------------------------------------------------------

_CONN_CONFIG = {"DB_HOST": "h", "DB_NAME": "n", "DB_USER": "u", "DB_PASSWORD": "p", "DB_PORT": "5432"}


def test_connect_success_returns_connection(monkeypatch, manager):
    fake_conn = MagicMock()
    monkeypatch.setattr(pu.psycopg2, "connect", lambda **kwargs: fake_conn)
    assert manager._connect(_CONN_CONFIG) is fake_conn


def test_connect_failure_returns_none(monkeypatch, manager):
    def boom(**kwargs):
        raise psycopg2.OperationalError("could not connect")
    monkeypatch.setattr(pu.psycopg2, "connect", boom)
    assert manager._connect(_CONN_CONFIG) is None


def test_connect_passes_port_as_int(monkeypatch, manager):
    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(pu.psycopg2, "connect", fake_connect)
    manager._connect(_CONN_CONFIG)
    assert captured["port"] == 5432
    assert isinstance(captured["port"], int)


# ---------------------------------------------------------------------------
# cmd_set / cmd_get
# ---------------------------------------------------------------------------

def test_cmd_set_missing_env_returns_1(manager):
    assert manager.cmd_set("prod") == 1


def test_cmd_set_success_persists_config(monkeypatch, manager):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    assert manager.cmd_set("prod") == 0
    stored = pu.load_config_store()
    assert stored["prod"]["DB_HOST"] == "db.example.com"
    assert stored["prod"]["DB_NAME"] == "citus"


def test_cmd_get_unknown_environment_returns_1(manager):
    assert manager.cmd_get("prod") == 1


def test_cmd_get_masks_password(manager, capsys):
    pu.save_config_store({"prod": {
        "DB_HOST": "h", "DB_USER": "u", "DB_PASSWORD": "supersecret",
        "DB_NAME": "n", "DB_PORT": "5432",
    }})
    assert manager.cmd_get("prod") == 0
    out = capsys.readouterr().out
    assert "supersecret" not in out
    assert "DB_PASSWORD=********" in out
    assert "DB_HOST=h" in out


# ---------------------------------------------------------------------------
# cmd_connect
# ---------------------------------------------------------------------------

def test_cmd_connect_missing_config_returns_1(manager):
    assert manager.cmd_connect("prod") == 1


def test_cmd_connect_connection_failure_returns_1(monkeypatch, manager):
    _store_valid_config("prod")
    monkeypatch.setattr(manager, "_connect", lambda config: None)
    assert manager.cmd_connect("prod") == 1


def test_cmd_connect_immediate_eof_closes_cleanly(monkeypatch, manager):
    _store_valid_config("prod")
    fake_conn = MagicMock()
    monkeypatch.setattr(manager, "_connect", lambda config: fake_conn)

    def raise_eof(prompt=""):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raise_eof)
    assert manager.cmd_connect("prod") == 0
    fake_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_test
# ---------------------------------------------------------------------------

def test_cmd_test_missing_config_returns_1(manager):
    assert manager.cmd_test("prod") == 1


def test_cmd_test_success_returns_0_and_closes(monkeypatch, manager):
    _store_valid_config("prod")
    fake_cursor = MagicMock()
    fake_cursor.fetchone.return_value = ("PostgreSQL 15.2", "citus", "svc", "10.0.0.1", 5432)
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    monkeypatch.setattr(manager, "_connect", lambda config: fake_conn)

    assert manager.cmd_test("prod") == 0
    fake_conn.close.assert_called_once()


def test_cmd_test_query_failure_returns_1_and_still_closes(monkeypatch, manager):
    _store_valid_config("prod")
    fake_cursor = MagicMock()
    fake_cursor.execute.side_effect = psycopg2.Error("boom")
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
    monkeypatch.setattr(manager, "_connect", lambda config: fake_conn)

    assert manager.cmd_test("prod") == 1
    fake_conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# _run_sql_file
# ---------------------------------------------------------------------------

_RUN_FILE_CONFIG = {"DB_HOST": "h", "DB_PORT": "5432", "DB_USER": "u", "DB_NAME": "n", "DB_PASSWORD": "p"}


def test_run_sql_file_missing_psql_returns_error(monkeypatch, manager):
    monkeypatch.setattr(pu.shutil, "which", lambda cmd: None)
    rc, output = manager._run_sql_file(_RUN_FILE_CONFIG, "/tmp/x.sql", commit=False)
    assert rc == 1
    assert output == ""


class _FakeProc:
    """Stand-in for subprocess.Popen -- records the command/env it was built
    with and yields canned stdout lines."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdout = iter(self.LINES)
        self.returncode = self.RETURNCODE

    def wait(self):
        pass


def test_run_sql_file_uses_rollback_finalizer_for_dry_run(monkeypatch, manager):
    monkeypatch.setattr(pu.shutil, "which", lambda cmd: "/usr/bin/psql")
    proc_holder = {}

    class FakeProc(_FakeProc):
        LINES = ["NOTICE: did a thing\n"]
        RETURNCODE = 0

        def __init__(self, cmd, **kwargs):
            super().__init__(cmd, **kwargs)
            proc_holder["proc"] = self

    monkeypatch.setattr(pu.subprocess, "Popen", FakeProc)
    rc, output = manager._run_sql_file(_RUN_FILE_CONFIG, "/tmp/x.sql", commit=False)

    assert rc == 0
    assert "NOTICE: did a thing" in output
    assert "ROLLBACK" in proc_holder["proc"].cmd
    assert "COMMIT" not in proc_holder["proc"].cmd
    assert proc_holder["proc"].kwargs["env"]["PGPASSWORD"] == "p"


def test_run_sql_file_uses_commit_finalizer_when_committing(monkeypatch, manager):
    monkeypatch.setattr(pu.shutil, "which", lambda cmd: "/usr/bin/psql")
    proc_holder = {}

    class FakeProc(_FakeProc):
        LINES = []
        RETURNCODE = 0

        def __init__(self, cmd, **kwargs):
            super().__init__(cmd, **kwargs)
            proc_holder["proc"] = self

    monkeypatch.setattr(pu.subprocess, "Popen", FakeProc)
    manager._run_sql_file(_RUN_FILE_CONFIG, "/tmp/x.sql", commit=True)

    assert "COMMIT" in proc_holder["proc"].cmd
    assert "ROLLBACK" not in proc_holder["proc"].cmd


def test_run_sql_file_nonzero_exit_returns_error(monkeypatch, manager):
    monkeypatch.setattr(pu.shutil, "which", lambda cmd: "/usr/bin/psql")

    class FakeProc(_FakeProc):
        LINES = ["ERROR: syntax error\n"]
        RETURNCODE = 1

    monkeypatch.setattr(pu.subprocess, "Popen", FakeProc)
    rc, output = manager._run_sql_file(_RUN_FILE_CONFIG, "/tmp/x.sql", commit=True)

    assert rc == 1
    assert "ERROR: syntax error" in output


# ---------------------------------------------------------------------------
# cmd_execute
# ---------------------------------------------------------------------------

def test_cmd_execute_missing_file_env_returns_1(manager):
    assert manager.cmd_execute("prod") == 1


def test_cmd_execute_nonexistent_file_returns_1(monkeypatch, manager, tmp_path):
    monkeypatch.setenv("FILE", str(tmp_path / "does-not-exist.sql"))
    assert manager.cmd_execute("prod") == 1


def test_cmd_execute_missing_config_returns_1(monkeypatch, manager, tmp_path):
    sql_file = tmp_path / "script.sql"
    sql_file.write_text("SELECT 1;")
    monkeypatch.setenv("FILE", str(sql_file))
    assert manager.cmd_execute("prod") == 1


def test_cmd_execute_passes_commit_flag_through(monkeypatch, manager, tmp_path):
    sql_file = tmp_path / "script.sql"
    sql_file.write_text("SELECT 1;")
    monkeypatch.setenv("FILE", str(sql_file))
    monkeypatch.setenv("COMMIT", "true")
    _store_valid_config("prod")

    calls = []

    def fake_run_sql_file(config, file_path, commit):
        calls.append(commit)
        return 0, ""

    monkeypatch.setattr(manager, "_run_sql_file", fake_run_sql_file)

    assert manager.cmd_execute("prod") == 0
    assert calls == [True]


def test_cmd_execute_defaults_to_dry_run_when_commit_unset(monkeypatch, manager, tmp_path):
    sql_file = tmp_path / "script.sql"
    sql_file.write_text("SELECT 1;")
    monkeypatch.setenv("FILE", str(sql_file))
    _store_valid_config("prod")

    calls = []

    def fake_run_sql_file(config, file_path, commit):
        calls.append(commit)
        return 0, ""

    monkeypatch.setattr(manager, "_run_sql_file", fake_run_sql_file)

    assert manager.cmd_execute("prod") == 0
    assert calls == [False]


# ---------------------------------------------------------------------------
# cmd_apply_pr -- the highest-consequence code path in the repo
# ---------------------------------------------------------------------------

def test_apply_pr_missing_pr_env_returns_1(manager):
    assert manager.cmd_apply_pr("qa") == 1


def test_apply_pr_invalid_pr_url_returns_1(monkeypatch, manager):
    monkeypatch.setenv("PR", "not-a-url")
    assert manager.cmd_apply_pr("qa") == 1


def test_apply_pr_missing_github_token_returns_1(monkeypatch, manager):
    monkeypatch.setenv("PR", PR_URL)
    assert manager.cmd_apply_pr("qa") == 1


def test_apply_pr_missing_stored_config_returns_1(monkeypatch, manager):
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert manager.cmd_apply_pr("qa") == 1


def test_apply_pr_fetch_failure_returns_1(monkeypatch, manager):
    _store_valid_config("qa")
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(pu, "fetch_pr_sql_file", lambda *a, **k: None)
    assert manager.cmd_apply_pr("qa") == 1


def test_apply_pr_environment_mismatch_refuses_without_running_sql(monkeypatch, manager):
    _store_valid_config("qa")
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(pu, "fetch_pr_sql_file", lambda *a, **k: ("prod/script.sql", CLEAN_SQL))
    run_sql_file = MagicMock()
    monkeypatch.setattr(manager, "_run_sql_file", run_sql_file)

    assert manager.cmd_apply_pr("qa") == 1
    run_sql_file.assert_not_called()


def test_apply_pr_lint_failure_refuses_without_running_sql(monkeypatch, manager):
    _store_valid_config("qa")
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(pu, "fetch_pr_sql_file", lambda *a, **k: ("qa/script.sql", SELF_MANAGED_TXN_SQL))
    run_sql_file = MagicMock()
    monkeypatch.setattr(manager, "_run_sql_file", run_sql_file)

    assert manager.cmd_apply_pr("qa") == 1
    run_sql_file.assert_not_called()


def test_apply_pr_dry_run_success_does_not_commit(monkeypatch, manager):
    _store_valid_config("qa")
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(pu, "fetch_pr_sql_file", lambda *a, **k: ("qa/script.sql", CLEAN_SQL))

    calls = []

    def fake_run_sql_file(config, file_path, commit):
        calls.append(commit)
        return 0, "NOTICE: 2 rows updated in kpay.foo\n"

    monkeypatch.setattr(manager, "_run_sql_file", fake_run_sql_file)

    assert manager.cmd_apply_pr("qa") == 0
    assert calls == [False]


def test_apply_pr_dry_run_rollback_failure_returns_1(monkeypatch, manager):
    _store_valid_config("qa")
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(pu, "fetch_pr_sql_file", lambda *a, **k: ("qa/script.sql", CLEAN_SQL))
    monkeypatch.setattr(manager, "_run_sql_file", lambda config, file_path, commit: (1, "ERROR: boom"))

    assert manager.cmd_apply_pr("qa") == 1


def test_apply_pr_verification_mismatch_blocks_commit(monkeypatch, manager):
    """Critical safety behavior: if the rollback-only run's actual row counts
    don't match the script's declared 'Expected Results', apply_pr must refuse
    to proceed to a real commit run even when COMMIT=True."""
    _store_valid_config("qa")
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("COMMIT", "true")
    monkeypatch.setattr(pu, "fetch_pr_sql_file", lambda *a, **k: ("qa/script.sql", CLEAN_SQL))

    calls = []

    def fake_run_sql_file(config, file_path, commit):
        calls.append(commit)
        # Declares 2 rows updated, but actual output reports only 1 -- mismatch.
        return 0, "NOTICE: 1 rows updated in kpay.foo\n"

    monkeypatch.setattr(manager, "_run_sql_file", fake_run_sql_file)

    assert manager.cmd_apply_pr("qa") == 1
    assert calls == [False]


def test_apply_pr_commit_true_runs_both_rollback_and_commit(monkeypatch, manager):
    _store_valid_config("qa")
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("COMMIT", "true")
    monkeypatch.setattr(pu, "fetch_pr_sql_file", lambda *a, **k: ("qa/script.sql", CLEAN_SQL))

    calls = []

    def fake_run_sql_file(config, file_path, commit):
        calls.append(commit)
        return 0, "NOTICE: 2 rows updated in kpay.foo\n"

    monkeypatch.setattr(manager, "_run_sql_file", fake_run_sql_file)

    assert manager.cmd_apply_pr("qa") == 0
    assert calls == [False, True]


def test_apply_pr_commit_run_failure_propagates_return_code(monkeypatch, manager):
    _store_valid_config("qa")
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("COMMIT", "true")
    monkeypatch.setattr(pu, "fetch_pr_sql_file", lambda *a, **k: ("qa/script.sql", CLEAN_SQL))

    calls = []

    def fake_run_sql_file(config, file_path, commit):
        calls.append(commit)
        if commit:
            return 1, "ERROR: something went wrong mid-commit"
        return 0, "NOTICE: 2 rows updated in kpay.foo\n"

    monkeypatch.setattr(manager, "_run_sql_file", fake_run_sql_file)

    assert manager.cmd_apply_pr("qa") == 1
    assert calls == [False, True]


def test_apply_pr_commit_succeeds_even_if_post_commit_verification_mismatches(monkeypatch, manager):
    """Documents existing (intentional) behavior: once the commit run itself has
    succeeded, apply_pr returns its exit code (0) even if the post-commit row
    counts don't match expectations -- the change already happened and can't be
    undone, so this is logged as an error but doesn't flip the return code."""
    _store_valid_config("qa")
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("COMMIT", "true")
    monkeypatch.setattr(pu, "fetch_pr_sql_file", lambda *a, **k: ("qa/script.sql", CLEAN_SQL))

    calls = []

    def fake_run_sql_file(config, file_path, commit):
        calls.append(commit)
        if commit:
            return 0, "NOTICE: 1 rows updated in kpay.foo\n"  # mismatched vs. expected 2
        return 0, "NOTICE: 2 rows updated in kpay.foo\n"

    monkeypatch.setattr(manager, "_run_sql_file", fake_run_sql_file)

    assert manager.cmd_apply_pr("qa") == 0
    assert calls == [False, True]


def test_apply_pr_removes_temp_file_after_run(monkeypatch, manager):
    """The script writes the PR's SQL content to a NamedTemporaryFile and must
    always clean it up, even on the success path."""
    _store_valid_config("qa")
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(pu, "fetch_pr_sql_file", lambda *a, **k: ("qa/script.sql", CLEAN_SQL))

    seen_paths = []

    def fake_run_sql_file(config, file_path, commit):
        seen_paths.append(file_path)
        return 0, "NOTICE: 2 rows updated in kpay.foo\n"

    monkeypatch.setattr(manager, "_run_sql_file", fake_run_sql_file)

    assert manager.cmd_apply_pr("qa") == 0
    assert len(seen_paths) == 1
    assert not os.path.exists(seen_paths[0])


# ---------------------------------------------------------------------------
# cmd_lint_pr
# ---------------------------------------------------------------------------

def test_lint_pr_missing_pr_env_returns_1(manager):
    assert manager.cmd_lint_pr() == 1


def test_lint_pr_invalid_url_returns_1(monkeypatch, manager):
    monkeypatch.setenv("PR", "not-a-url")
    assert manager.cmd_lint_pr() == 1


def test_lint_pr_missing_token_returns_1(monkeypatch, manager):
    monkeypatch.setenv("PR", PR_URL)
    assert manager.cmd_lint_pr() == 1


def test_lint_pr_no_sql_files_returns_1(monkeypatch, manager):
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(pu, "list_pr_sql_files", lambda *a, **k: [])
    assert manager.cmd_lint_pr() == 1


def test_lint_pr_all_clean_returns_0(monkeypatch, manager):
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(pu, "list_pr_sql_files", lambda *a, **k: [{"filename": "qa/a.sql", "contents_url": "u1"}])
    monkeypatch.setattr(pu, "fetch_file_content", lambda *a, **k: CLEAN_SQL)
    assert manager.cmd_lint_pr() == 0


def test_lint_pr_any_failure_returns_1(monkeypatch, manager):
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(pu, "list_pr_sql_files", lambda *a, **k: [
        {"filename": "qa/good.sql", "contents_url": "u1"},
        {"filename": "qa/bad.sql", "contents_url": "u2"},
    ])

    def fake_fetch(url, token):
        return CLEAN_SQL if url == "u1" else SELF_MANAGED_TXN_SQL

    monkeypatch.setattr(pu, "fetch_file_content", fake_fetch)
    assert manager.cmd_lint_pr() == 1


def test_lint_pr_unfetchable_file_counts_as_issue(monkeypatch, manager):
    monkeypatch.setenv("PR", PR_URL)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(pu, "list_pr_sql_files", lambda *a, **k: [{"filename": "qa/a.sql", "contents_url": "u1"}])
    monkeypatch.setattr(pu, "fetch_file_content", lambda *a, **k: None)
    assert manager.cmd_lint_pr() == 1
