"""Mocked tests for kald_devops.commands.repositories. No real network call
happens anywhere in this file."""
import argparse
from datetime import datetime, timedelta, timezone

import pytest

from kald_devops.commands import repositories

# Computed relative to "now" rather than hardcoded, so these stay valid
# regardless of when the test actually runs (the cutoff is "30 days ago").
_RECENT = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
_STALE = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _args():
    return argparse.Namespace(csv=False, json=False)


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json_data


def test_cmd_list_repositories_missing_token_exits_1(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        repositories.cmd_list_repositories(_args())
    assert exc_info.value.code == 1


def test_cmd_list_repositories_lists_recently_active_repos(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    page1 = [
        {"name": "phoenix", "pushed_at": _RECENT, "visibility": "private", "description": "Main repo"},
    ]
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        return _FakeResponse(200, page1 if len(calls) == 1 else [])

    monkeypatch.setattr(repositories, "http_get", fake_get)

    with pytest.raises(SystemExit) as exc_info:
        repositories.cmd_list_repositories(_args())
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "KalderosLLC/phoenix" in out
    assert "Main repo" in out


def test_cmd_list_repositories_stops_at_cutoff(monkeypatch, capsys):
    """Repos not pushed to in the last 30 days must be excluded, and the scan
    must stop there rather than paginating further."""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    page1 = [
        {"name": "active-repo", "pushed_at": _RECENT, "visibility": "public", "description": "fresh"},
        {"name": "stale-repo", "pushed_at": _STALE, "visibility": "public", "description": "old"},
    ]
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        return _FakeResponse(200, page1 if len(calls) == 1 else [])

    monkeypatch.setattr(repositories, "http_get", fake_get)
    with pytest.raises(SystemExit):
        repositories.cmd_list_repositories(_args())
    out = capsys.readouterr().out
    assert "active-repo" in out
    assert "stale-repo" not in out
    assert len(calls) == 1  # stopped after the cutoff was hit on the first page


def test_cmd_list_repositories_fetch_failure_exits_1(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(repositories, "http_get", lambda url, headers: _FakeResponse(500, text="boom"))
    with pytest.raises(SystemExit) as exc_info:
        repositories.cmd_list_repositories(_args())
    assert exc_info.value.code == 1


def test_cmd_list_repositories_saml_error_includes_guidance(monkeypatch, caplog):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(repositories, "http_get", lambda url, headers: _FakeResponse(
        403,
        json_data={"message": "Resource protected by SAML enforcement."},
        text='{"message": "Resource protected by SAML enforcement."}',
        headers={"content-type": "application/json"},
    ))
    with pytest.raises(SystemExit) as exc_info:
        repositories.cmd_list_repositories(_args())
    assert exc_info.value.code == 1
    assert "Configure SSO" in caplog.text
