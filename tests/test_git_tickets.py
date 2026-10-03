"""Mocked tests for kald_devops.commands.git_tickets. No real network call
happens anywhere in this file."""
import argparse

import pytest

from kald_devops.commands import git_tickets


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


def _set_required_env(monkeypatch, repository="KalderosLLC/phoenix", tag="v1.5.0", token="tok"):
    monkeypatch.setenv("REPOSITORY", repository)
    monkeypatch.setenv("TAG", tag)
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.delenv("FROM_TAG", raising=False)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)


# ---------------------------------------------------------------------------
# resolve_from_tag
# ---------------------------------------------------------------------------

def test_resolve_from_tag_finds_highest_matching_patch(monkeypatch):
    monkeypatch.setattr(git_tickets, "http_get", lambda url, headers: _FakeResponse(200, [
        {"name": "v1.4.2"}, {"name": "v1.4.10"}, {"name": "v1.4.1"}, {"name": "v1.5.0"},
    ]))
    tag, err = git_tickets.resolve_from_tag({}, "org/repo", "v", 1, 5)
    assert err is None
    assert tag == "v1.4.10"


def test_resolve_from_tag_no_match_returns_error(monkeypatch):
    monkeypatch.setattr(git_tickets, "http_get", lambda url, headers: _FakeResponse(200, []))
    tag, err = git_tickets.resolve_from_tag({}, "org/repo", "v", 1, 5)
    assert tag is None
    assert "No tags found" in err


def test_resolve_from_tag_fetch_failure_returns_error(monkeypatch):
    monkeypatch.setattr(git_tickets, "http_get", lambda url, headers: _FakeResponse(500, text="boom"))
    tag, err = git_tickets.resolve_from_tag({}, "org/repo", "v", 1, 5)
    assert tag is None
    assert "Failed to fetch tags" in err


# ---------------------------------------------------------------------------
# fetch_compare_commits
# ---------------------------------------------------------------------------

def test_fetch_compare_commits_returns_commits_when_not_truncated(monkeypatch):
    commits = [{"sha": "a"}, {"sha": "b"}]
    monkeypatch.setattr(git_tickets, "http_get", lambda url, headers: _FakeResponse(200, {
        "commits": commits, "total_commits": 2,
    }))
    result, err = git_tickets.fetch_compare_commits({}, "org/repo", "v1.0", "v1.1")
    assert err is None
    assert result == commits


def test_fetch_compare_commits_failure_returns_error(monkeypatch):
    monkeypatch.setattr(git_tickets, "http_get", lambda url, headers: _FakeResponse(404, text="not found"))
    result, err = git_tickets.fetch_compare_commits({}, "org/repo", "v1.0", "v1.1")
    assert result is None
    assert "404" in err


def test_fetch_compare_commits_paginates_when_truncated(monkeypatch):
    compare_resp = _FakeResponse(200, {
        "commits": [{"sha": "newest"}],
        "total_commits": 3,
        "merge_base_commit": {"sha": "base"},
    })
    list_resp = _FakeResponse(200, [{"sha": "c2"}, {"sha": "c1"}, {"sha": "base"}])

    calls = {"n": 0}

    def fake_get(url, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            return compare_resp
        return list_resp

    monkeypatch.setattr(git_tickets, "http_get", fake_get)
    result, err = git_tickets.fetch_compare_commits({}, "org/repo", "v1.0", "v1.1")
    assert err is None
    # Walks the commit list until it reaches merge_base_sha, excluding it.
    assert result == [{"sha": "c2"}, {"sha": "c1"}]


# ---------------------------------------------------------------------------
# cmd_git_tickets
# ---------------------------------------------------------------------------

def test_cmd_git_tickets_missing_env_exits_1(monkeypatch):
    monkeypatch.delenv("REPOSITORY", raising=False)
    monkeypatch.delenv("TAG", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        git_tickets.cmd_git_tickets(_args())
    assert exc_info.value.code == 1


def test_cmd_git_tickets_rejects_malformed_repository(monkeypatch):
    _set_required_env(monkeypatch, repository="not-a-repo-path")
    with pytest.raises(SystemExit) as exc_info:
        git_tickets.cmd_git_tickets(_args())
    assert exc_info.value.code == 1


def test_cmd_git_tickets_rejects_non_semver_tag(monkeypatch):
    _set_required_env(monkeypatch, tag="not-a-version")
    with pytest.raises(SystemExit) as exc_info:
        git_tickets.cmd_git_tickets(_args())
    assert exc_info.value.code == 1


def test_cmd_git_tickets_minor_zero_without_from_tag_exits_1(monkeypatch):
    _set_required_env(monkeypatch, tag="v1.0.0")
    with pytest.raises(SystemExit) as exc_info:
        git_tickets.cmd_git_tickets(_args())
    assert exc_info.value.code == 1


def test_cmd_git_tickets_rejects_non_semver_from_tag(monkeypatch):
    _set_required_env(monkeypatch, tag="v1.5.0")
    monkeypatch.setenv("FROM_TAG", "not-a-version")
    with pytest.raises(SystemExit) as exc_info:
        git_tickets.cmd_git_tickets(_args())
    assert exc_info.value.code == 1


def test_cmd_git_tickets_auto_resolves_from_tag_when_unset(monkeypatch):
    _set_required_env(monkeypatch, tag="v1.5.0")
    monkeypatch.setattr(git_tickets, "resolve_from_tag", lambda *a, **k: ("v1.4.3", None))
    monkeypatch.setattr(git_tickets, "fetch_compare_commits", lambda *a, **k: ([], None))

    with pytest.raises(SystemExit) as exc_info:
        git_tickets.cmd_git_tickets(_args())
    assert exc_info.value.code == 0  # no merge commits found -> clean exit(0)


def test_cmd_git_tickets_from_tag_resolution_failure_exits_1(monkeypatch):
    _set_required_env(monkeypatch, tag="v1.5.0")
    monkeypatch.setattr(git_tickets, "resolve_from_tag", lambda *a, **k: (None, "no tags found"))
    with pytest.raises(SystemExit) as exc_info:
        git_tickets.cmd_git_tickets(_args())
    assert exc_info.value.code == 1


def test_cmd_git_tickets_compare_failure_exits_1(monkeypatch):
    _set_required_env(monkeypatch, tag="v1.5.0")
    monkeypatch.setenv("FROM_TAG", "v1.4.0")
    monkeypatch.setattr(git_tickets, "fetch_compare_commits", lambda *a, **k: (None, "404 - not found"))
    with pytest.raises(SystemExit) as exc_info:
        git_tickets.cmd_git_tickets(_args())
    assert exc_info.value.code == 1


def test_cmd_git_tickets_no_merge_commits_exits_0(monkeypatch):
    _set_required_env(monkeypatch, tag="v1.5.0")
    monkeypatch.setenv("FROM_TAG", "v1.4.0")
    # Non-merge commit (single parent) -- must be skipped entirely.
    monkeypatch.setattr(git_tickets, "fetch_compare_commits", lambda *a, **k: (
        [{"parents": [{"sha": "p1"}], "commit": {"message": "CES-1 fix"}}], None
    ))
    with pytest.raises(SystemExit) as exc_info:
        git_tickets.cmd_git_tickets(_args())
    assert exc_info.value.code == 0


def test_cmd_git_tickets_extracts_jira_ids_from_merge_commits(monkeypatch, capsys):
    _set_required_env(monkeypatch, tag="v1.5.0")
    monkeypatch.setenv("FROM_TAG", "v1.4.0")
    commits = [
        {
            "parents": [{"sha": "p1"}, {"sha": "p2"}],  # merge commit (2 parents)
            "commit": {"message": "Merge PR: fixes CES-123 and T340B-9", "author": {"name": "Alice"}},
        },
        {
            "parents": [{"sha": "p3"}],  # not a merge commit -- ignored
            "commit": {"message": "CES-999 should not appear"},
        },
    ]
    monkeypatch.setattr(git_tickets, "fetch_compare_commits", lambda *a, **k: (commits, None))

    with pytest.raises(SystemExit) as exc_info:
        git_tickets.cmd_git_tickets(_args())
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "CES-123" in out
    assert "T340B-9" in out
    assert "CES-999" not in out
    assert "Alice" in out
