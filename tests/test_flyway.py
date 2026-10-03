"""Mocked tests for kald_devops.commands.flyway. No real network call
happens anywhere in this file."""
import argparse

import pytest

from kald_devops.commands import flyway


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


def _set_required_env(monkeypatch, envs="qa", token="tok"):
    monkeypatch.setenv("ENVIRONMENTS", envs)
    monkeypatch.setenv("GITHUB_TOKEN", token)


def test_cmd_apply_flyway_missing_env_exits_1(monkeypatch):
    monkeypatch.delenv("ENVIRONMENTS", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        flyway.cmd_apply_flyway(_args())
    assert exc_info.value.code == 1


def test_cmd_apply_flyway_defaults_branch_or_tag_to_main(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("BRANCH_OR_TAG", raising=False)
    monkeypatch.setattr(flyway, "http_get", lambda url, headers: _FakeResponse(200))
    captured = {}
    monkeypatch.setattr(flyway, "trigger_and_poll_environments",
                         lambda *a, extra_value, **k: captured.setdefault("extra_value", extra_value))
    flyway.cmd_apply_flyway(_args())
    assert captured["extra_value"] == "main"


def test_cmd_apply_flyway_missing_ref_exits_1_without_triggering(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setattr(flyway, "http_get", lambda url, headers: _FakeResponse(404))
    called = []
    monkeypatch.setattr(flyway, "trigger_and_poll_environments", lambda *a, **k: called.append(1))

    with pytest.raises(SystemExit) as exc_info:
        flyway.cmd_apply_flyway(_args())
    assert exc_info.value.code == 1
    assert called == []  # never reached the trigger/poll stage


def test_cmd_apply_flyway_treats_semver_branch_or_tag_as_a_tag_lookup(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BRANCH_OR_TAG", "v1.2.3")
    seen_urls = []

    def fake_get(url, headers):
        seen_urls.append(url)
        return _FakeResponse(200)

    monkeypatch.setattr(flyway, "http_get", fake_get)
    monkeypatch.setattr(flyway, "trigger_and_poll_environments", lambda *a, **k: None)
    flyway.cmd_apply_flyway(_args())
    assert "git/ref/tags/v1.2.3" in seen_urls[0]


def test_cmd_apply_flyway_treats_non_semver_branch_or_tag_as_a_branch_lookup(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BRANCH_OR_TAG", "my-feature-branch")
    seen_urls = []

    def fake_get(url, headers):
        seen_urls.append(url)
        return _FakeResponse(200)

    monkeypatch.setattr(flyway, "http_get", fake_get)
    monkeypatch.setattr(flyway, "trigger_and_poll_environments", lambda *a, **k: None)
    flyway.cmd_apply_flyway(_args())
    assert "branches/my-feature-branch" in seen_urls[0]


def test_cmd_apply_flyway_wires_trigger_and_poll_correctly(monkeypatch):
    _set_required_env(monkeypatch, envs="qa,stage")
    monkeypatch.setenv("BRANCH_OR_TAG", "main")
    monkeypatch.setattr(flyway, "http_get", lambda url, headers: _FakeResponse(200))

    captured = {}

    def fake_trigger_and_poll(args, environments_list, trigger_fn, poll_fn, extra_column, extra_value, log_tool):
        captured["environments_list"] = environments_list
        captured["trigger_fn"] = trigger_fn
        captured["poll_fn"] = poll_fn
        captured["extra_column"] = extra_column
        captured["extra_value"] = extra_value
        captured["log_tool"] = log_tool

    monkeypatch.setattr(flyway, "trigger_and_poll_environments", fake_trigger_and_poll)
    flyway.cmd_apply_flyway(_args())  # must not raise -- the real dispatch is mocked out

    assert captured["environments_list"] == ["qa", "stage"]
    assert captured["extra_column"] == "branch_or_tag"
    assert captured["extra_value"] == "main"
    assert captured["log_tool"] == "flyway"

    trigger_calls = []
    monkeypatch.setattr(flyway, "_trigger_flyway_env", lambda env, branch_or_tag, gh_headers: trigger_calls.append((env, branch_or_tag)))
    captured["trigger_fn"]("qa")
    assert trigger_calls == [("qa", "main")]

    poll_calls = []
    monkeypatch.setattr(flyway, "poll_gh_run", lambda gh_headers, repo, run_id: poll_calls.append((repo, run_id)))
    captured["poll_fn"](456)
    assert poll_calls == [("KalderosLLC/phoenix-data-gateway", 456)]
