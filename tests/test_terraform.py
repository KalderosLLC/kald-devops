"""Mocked tests for kald_devops.commands.terraform. No real network call
happens anywhere in this file."""
import argparse

import pytest

from kald_devops.commands import terraform


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
# fetch_workflow_runs
# ---------------------------------------------------------------------------

def test_fetch_workflow_runs_success(monkeypatch):
    monkeypatch.setattr(terraform, "http_get", lambda url, headers: _FakeResponse(200, {"workflow_runs": [{"id": 1}]}))
    assert terraform.fetch_workflow_runs({}, "wf.yml") == [{"id": 1}]


def test_fetch_workflow_runs_failure_returns_none(monkeypatch):
    monkeypatch.setattr(terraform, "http_get", lambda url, headers: _FakeResponse(500, text="boom"))
    assert terraform.fetch_workflow_runs({}, "wf.yml") is None


# ---------------------------------------------------------------------------
# _pr_number_from_run
# ---------------------------------------------------------------------------

def test_pr_number_from_run_uses_pull_requests_array():
    assert terraform._pr_number_from_run({"pull_requests": [{"number": 42}]}) == "42"


def test_pr_number_from_run_falls_back_to_referenced_workflows():
    run = {"pull_requests": [], "referenced_workflows": [{"ref": "refs/pull/99/merge"}]}
    assert terraform._pr_number_from_run(run) == "99"


def test_pr_number_from_run_returns_none_when_unresolvable():
    assert terraform._pr_number_from_run({}) is None


# ---------------------------------------------------------------------------
# find_pr_with_successful_plan
# ---------------------------------------------------------------------------

def test_find_pr_with_successful_plan_skips_incomplete_and_failed_runs():
    runs = [
        {"status": "in_progress", "conclusion": None},
        {"status": "completed", "conclusion": "failure", "pull_requests": [{"number": 1}]},
        {"status": "completed", "conclusion": "success", "pull_requests": [{"number": 2}]},
    ]
    assert terraform.find_pr_with_successful_plan(runs) == "2"


def test_find_pr_with_successful_plan_no_qualifying_run_returns_none():
    assert terraform.find_pr_with_successful_plan([{"status": "completed", "conclusion": "failure"}]) is None


# ---------------------------------------------------------------------------
# resolve_pr_from_terraform_plan
# ---------------------------------------------------------------------------

def test_resolve_pr_from_terraform_plan_fetch_failure(monkeypatch):
    monkeypatch.setattr(terraform, "fetch_workflow_runs", lambda *a, **k: None)
    pr, runs = terraform.resolve_pr_from_terraform_plan({})
    assert pr is None
    assert runs == []


def test_resolve_pr_from_terraform_plan_success(monkeypatch):
    runs = [{"status": "completed", "conclusion": "success", "pull_requests": [{"number": 7}]}]
    monkeypatch.setattr(terraform, "fetch_workflow_runs", lambda *a, **k: runs)
    pr, returned_runs = terraform.resolve_pr_from_terraform_plan({})
    assert pr == "7"
    assert returned_runs == runs


# ---------------------------------------------------------------------------
# cmd_calc_pr
# ---------------------------------------------------------------------------

def test_cmd_calc_pr_missing_token_exits_1(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        terraform.cmd_calc_pr(_args())
    assert exc_info.value.code == 1


def test_cmd_calc_pr_prints_resolved_pr(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    runs = [{
        "id": 1, "status": "completed", "conclusion": "success",
        "pull_requests": [{"number": 7}], "html_url": "http://r/1",
        "created_at": "2026-01-01T00:00:00Z", "head_branch": "main",
    }]
    monkeypatch.setattr(terraform, "resolve_pr_from_terraform_plan", lambda gh_headers: ("7", runs))
    with pytest.raises(SystemExit) as exc_info:
        terraform.cmd_calc_pr(_args())
    assert exc_info.value.code == 0
    assert "7" in capsys.readouterr().out


def test_cmd_calc_pr_no_pr_found_exits_1(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(terraform, "resolve_pr_from_terraform_plan", lambda gh_headers: (None, []))
    with pytest.raises(SystemExit) as exc_info:
        terraform.cmd_calc_pr(_args())
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_apply_terraform
# ---------------------------------------------------------------------------

def test_cmd_apply_terraform_missing_env_exits_1(monkeypatch):
    monkeypatch.delenv("PR", raising=False)
    monkeypatch.delenv("ENVIRONMENTS", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        terraform.cmd_apply_terraform(_args())
    assert exc_info.value.code == 1


def test_cmd_apply_terraform_wires_trigger_and_poll_correctly(monkeypatch):
    monkeypatch.setenv("PR", "42")
    monkeypatch.setenv("ENVIRONMENTS", "qa,stage")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    captured = {}

    def fake_trigger_and_poll(args, environments_list, trigger_fn, poll_fn, extra_column, extra_value, log_tool):
        captured["environments_list"] = environments_list
        captured["trigger_fn"] = trigger_fn
        captured["poll_fn"] = poll_fn
        captured["extra_column"] = extra_column
        captured["extra_value"] = extra_value
        captured["log_tool"] = log_tool

    monkeypatch.setattr(terraform, "trigger_and_poll_environments", fake_trigger_and_poll)
    terraform.cmd_apply_terraform(_args())  # must not raise -- the real dispatch is mocked out

    assert captured["environments_list"] == ["qa", "stage"]
    assert captured["extra_column"] == "pr"
    assert captured["extra_value"] == "42"
    assert captured["log_tool"] == "terraform"

    # The bound trigger_fn/poll_fn closures must delegate with the right arguments.
    trigger_calls = []
    monkeypatch.setattr(terraform, "_trigger_terraform_env", lambda env, pr, gh_headers: trigger_calls.append((env, pr)))
    captured["trigger_fn"]("qa")
    assert trigger_calls == [("qa", "42")]

    poll_calls = []
    monkeypatch.setattr(terraform, "poll_gh_run", lambda gh_headers, repo, run_id: poll_calls.append((repo, run_id)))
    captured["poll_fn"](123)
    assert poll_calls == [("KalderosLLC/phoenix", 123)]
