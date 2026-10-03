"""Mocked tests for kald_devops.commands.teams. No real network call
happens anywhere in this file."""
import argparse

import pytest

from kald_devops.commands import teams


def _args():
    return argparse.Namespace(csv=False, json=False)


def test_cmd_teams_release_missing_env_exits_1(monkeypatch):
    monkeypatch.delenv("ENVIRONMENTS", raising=False)
    monkeypatch.delenv("BRANCH_OR_TAG", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        teams.cmd_teams_release(_args())
    assert exc_info.value.code == 1


def test_cmd_teams_release_without_webhook_just_prints(monkeypatch, capsys):
    monkeypatch.setenv("ENVIRONMENTS", "qa,stage")
    monkeypatch.setenv("BRANCH_OR_TAG", "v1.2.3")
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        teams.cmd_teams_release(_args())
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "v1.2.3" in out
    assert "qa,stage" in out
    assert "releases/tag/v1.2.3" in out  # semver tag -> /releases/tag/ URL


def test_cmd_teams_release_non_semver_branch_uses_tree_url(monkeypatch, capsys):
    monkeypatch.setenv("ENVIRONMENTS", "qa")
    monkeypatch.setenv("BRANCH_OR_TAG", "my-feature-branch")
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)

    with pytest.raises(SystemExit):
        teams.cmd_teams_release(_args())
    out = capsys.readouterr().out
    assert "tree/my-feature-branch" in out


def test_cmd_teams_release_posts_to_webhook_when_set(monkeypatch):
    monkeypatch.setenv("ENVIRONMENTS", "qa")
    monkeypatch.setenv("BRANCH_OR_TAG", "v1.2.3")
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://example.com/webhook")

    calls = []
    monkeypatch.setattr(teams, "post_to_teams_webhook", lambda url, title, message: calls.append((url, title)) or True)

    with pytest.raises(SystemExit) as exc_info:
        teams.cmd_teams_release(_args())
    assert exc_info.value.code == 0
    assert calls == [("https://example.com/webhook", "Truzo Release")]


def test_cmd_teams_release_webhook_failure_exits_1(monkeypatch):
    monkeypatch.setenv("ENVIRONMENTS", "qa")
    monkeypatch.setenv("BRANCH_OR_TAG", "v1.2.3")
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://example.com/webhook")
    monkeypatch.setattr(teams, "post_to_teams_webhook", lambda url, title, message: False)

    with pytest.raises(SystemExit) as exc_info:
        teams.cmd_teams_release(_args())
    assert exc_info.value.code == 1
