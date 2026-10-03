"""Mocked tests for kald_devops.commands.release_notes. No real network call
happens anywhere in this file."""
import argparse

import pytest

from kald_devops.commands import release_notes


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


def test_cmd_create_release_notes_missing_env_exits_1(monkeypatch):
    monkeypatch.delenv("REPOSITORY", raising=False)
    monkeypatch.delenv("TAG", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        release_notes.cmd_create_release_notes(_args())
    assert exc_info.value.code == 1


def test_cmd_create_release_notes_success_prints_url(monkeypatch, capsys):
    monkeypatch.setenv("REPOSITORY", "KalderosLLC/phoenix")
    monkeypatch.setenv("TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(release_notes, "http_post", lambda url, headers, json: _FakeResponse(201, {
        "html_url": "https://github.com/KalderosLLC/phoenix/releases/tag/v1.2.3"
    }))

    with pytest.raises(SystemExit) as exc_info:
        release_notes.cmd_create_release_notes(_args())
    assert exc_info.value.code == 0
    assert "releases/tag/v1.2.3" in capsys.readouterr().out


def test_cmd_create_release_notes_failure_exits_1(monkeypatch):
    monkeypatch.setenv("REPOSITORY", "KalderosLLC/phoenix")
    monkeypatch.setenv("TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(release_notes, "http_post", lambda url, headers, json: _FakeResponse(422, text="already exists"))

    with pytest.raises(SystemExit) as exc_info:
        release_notes.cmd_create_release_notes(_args())
    assert exc_info.value.code == 1
