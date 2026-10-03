"""Mocked tests for kald_devops.github_actions, including the shared
trigger_and_poll_environments orchestration used by both terraform.py and
flyway.py. No real network call or real sleep happens anywhere in this file.
"""
import argparse
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from kald_devops import github_actions as ga


def _args():
    return argparse.Namespace(csv=False, json=False)


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._json_data


# ---------------------------------------------------------------------------
# fetch_triggered_run_url
# ---------------------------------------------------------------------------

def test_fetch_triggered_run_url_finds_run_created_after_trigger(monkeypatch):
    monkeypatch.setattr(ga.time, "sleep", lambda s: None)
    triggered_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = (triggered_at + timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(ga, "http_get", lambda url, headers: _FakeResponse(200, {
        "workflow_runs": [{"html_url": "http://run/1", "id": 1, "created_at": later}]
    }))
    url, run_id = ga.fetch_triggered_run_url({}, "wf.yml", triggered_at)
    assert url == "http://run/1"
    assert run_id == 1


def test_fetch_triggered_run_url_ignores_runs_created_before_trigger(monkeypatch):
    monkeypatch.setattr(ga.time, "sleep", lambda s: None)
    triggered_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    earlier = (triggered_at - timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(ga, "http_get", lambda url, headers: _FakeResponse(200, {
        "workflow_runs": [{"html_url": "http://run/old", "id": 0, "created_at": earlier}]
    }))
    assert ga.fetch_triggered_run_url({}, "wf.yml", triggered_at, retries=1) == (None, None)


def test_fetch_triggered_run_url_gives_up_after_retries_exhausted(monkeypatch):
    monkeypatch.setattr(ga.time, "sleep", lambda s: None)
    monkeypatch.setattr(ga, "http_get", lambda url, headers: _FakeResponse(200, {"workflow_runs": []}))
    assert ga.fetch_triggered_run_url({}, "wf.yml", datetime.now(timezone.utc), retries=3) == (None, None)


def test_fetch_triggered_run_url_stops_retrying_on_non_200(monkeypatch):
    monkeypatch.setattr(ga.time, "sleep", lambda s: None)
    calls = []

    def fake_get(url, headers):
        calls.append(1)
        return _FakeResponse(500)

    monkeypatch.setattr(ga, "http_get", fake_get)
    assert ga.fetch_triggered_run_url({}, "wf.yml", datetime.now(timezone.utc), retries=5) == (None, None)
    assert len(calls) == 1  # breaks out of the retry loop immediately, doesn't keep polling


# ---------------------------------------------------------------------------
# poll_gh_run
# ---------------------------------------------------------------------------

def test_poll_gh_run_success(monkeypatch):
    monkeypatch.setattr(ga.time, "sleep", lambda s: None)
    monkeypatch.setattr(ga, "http_get", lambda url, headers: _FakeResponse(200, {"status": "completed", "conclusion": "success"}))
    status, reason = ga.poll_gh_run({}, "org/repo", 123)
    assert status == "success"
    assert reason is None


def test_poll_gh_run_reports_failure_conclusion_as_reason(monkeypatch):
    monkeypatch.setattr(ga.time, "sleep", lambda s: None)
    monkeypatch.setattr(ga, "http_get", lambda url, headers: _FakeResponse(200, {"status": "completed", "conclusion": "failure"}))
    status, reason = ga.poll_gh_run({}, "org/repo", 123)
    assert status == "failure"
    assert reason == "failure"


def test_poll_gh_run_times_out_if_never_completed(monkeypatch):
    monkeypatch.setattr(ga.time, "sleep", lambda s: None)
    times = [1000.0, 1000.0, 1000.0, 1000.0]

    def fake_time():
        return times.pop(0) if times else 10000.0

    monkeypatch.setattr(ga.time, "time", fake_time)
    monkeypatch.setattr(ga, "http_get", lambda url, headers: _FakeResponse(200, {"status": "in_progress"}))
    status, reason = ga.poll_gh_run({}, "org/repo", 123, timeout=500)
    assert status == "timed_out"
    assert reason == "polling timed out"


def test_poll_gh_run_logs_and_keeps_polling_on_non_200(monkeypatch):
    monkeypatch.setattr(ga.time, "sleep", lambda s: None)
    responses = iter([_FakeResponse(500), _FakeResponse(200, {"status": "completed", "conclusion": "success"})])
    monkeypatch.setattr(ga, "http_get", lambda url, headers: next(responses))
    status, reason = ga.poll_gh_run({}, "org/repo", 123)
    assert status == "success"


# ---------------------------------------------------------------------------
# trigger_and_poll_environments
# ---------------------------------------------------------------------------

def test_trigger_and_poll_environments_all_succeed(capsys):
    calls = []

    def trigger_fn(env):
        calls.append(env)
        return env, f"http://run/{env}", len(calls)

    def poll_fn(run_id):
        return "success", None

    with pytest.raises(SystemExit) as exc_info:
        ga.trigger_and_poll_environments(
            _args(), ["qa", "stage"], trigger_fn, poll_fn,
            extra_column="pr", extra_value="123", log_tool="terraform",
        )
    assert exc_info.value.code == 0
    assert calls == ["qa", "stage"]  # triggered strictly in order
    out = capsys.readouterr().out
    assert "qa" in out and "stage" in out and "123" in out


def test_trigger_and_poll_environments_trigger_failure_still_polls_others():
    def trigger_fn(env):
        if env == "qa":
            return env, None, None
        return env, f"http://run/{env}", 1

    with pytest.raises(SystemExit) as exc_info:
        ga.trigger_and_poll_environments(
            _args(), ["qa", "stage"], trigger_fn, lambda run_id: ("success", None),
            extra_column="branch_or_tag", extra_value="main", log_tool="flyway",
        )
    # stage succeeded, but qa's trigger failure must still surface as a non-zero exit.
    assert exc_info.value.code == 1


def test_trigger_and_poll_environments_nothing_triggered_exits_1_when_errors_present():
    with pytest.raises(SystemExit) as exc_info:
        ga.trigger_and_poll_environments(
            _args(), ["qa"], lambda env: (env, None, None), lambda run_id: ("success", None),
            extra_column="pr", extra_value="1", log_tool="terraform",
        )
    assert exc_info.value.code == 1


def test_trigger_and_poll_environments_poll_failure_exits_1():
    with pytest.raises(SystemExit) as exc_info:
        ga.trigger_and_poll_environments(
            _args(), ["qa"], lambda env: (env, "http://run", 1), lambda run_id: ("failure", "boom"),
            extra_column="pr", extra_value="1", log_tool="terraform",
        )
    assert exc_info.value.code == 1


def test_trigger_and_poll_environments_missing_run_id_skips_poll_and_reports_unknown(capsys):
    poll_fn = MagicMock()
    with pytest.raises(SystemExit) as exc_info:
        ga.trigger_and_poll_environments(
            _args(), ["qa"], lambda env: (env, "http://run", None), poll_fn,
            extra_column="pr", extra_value="1", log_tool="terraform",
        )
    assert exc_info.value.code == 1
    poll_fn.assert_not_called()
    assert "unknown" in capsys.readouterr().out
