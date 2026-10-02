"""Unit tests for kald_devops.devops -- the CLI entry point.

devops.py itself holds only argument parsing and dispatch (HANDLERS + main());
the actual command logic lives in kald_devops.commands.* and is tested there,
and the declarative subcommand metadata lives in kald_devops.common and is
tested in test_common.py. These tests cover the wiring between the two:
every HANDLERS entry matches a real SUBCOMMAND_SPECS entry, and main()'s
auth-gating logic behaves correctly for both no-auth and Azure commands.
"""
import pytest

from kald_devops import devops
from kald_devops import common


# ---------------------------------------------------------------------------
# HANDLERS <-> SUBCOMMAND_SPECS integrity
# ---------------------------------------------------------------------------

def test_every_handler_is_callable():
    for name, handler in devops.HANDLERS.items():
        assert callable(handler), name


def test_every_handler_has_a_matching_spec():
    spec_names = {s["name"] for s in common.SUBCOMMAND_SPECS}
    for name in devops.HANDLERS:
        assert name in spec_names, name


def test_every_spec_except_usage_has_a_handler():
    for s in common.SUBCOMMAND_SPECS:
        if s["name"] == "usage":
            continue
        assert s["name"] in devops.HANDLERS, s["name"]


# ---------------------------------------------------------------------------
# main() dispatch -- no-auth commands must not require AZURE_DEVOPS_EXT_PAT
# ---------------------------------------------------------------------------

def test_main_dispatches_no_auth_command_without_requiring_pat(monkeypatch):
    """Regression test: before the subcommand-dispatch refactor, a no-auth
    command whose handler never calls sys.exit() on success (e.g.
    tag_repository) would fall through into main()'s AZURE_DEVOPS_EXT_PAT
    check and could exit(1) with a spurious error even after succeeding."""
    called = {}

    def fake_handler(args):
        called["ran"] = True

    monkeypatch.setitem(devops.HANDLERS, "tag_repository", fake_handler)
    monkeypatch.setattr(devops.sys, "argv", ["kald-devops", "tag_repository"])
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)

    devops.main()

    assert called.get("ran") is True


def test_main_requires_pat_for_azure_command(monkeypatch, capsys):
    monkeypatch.setattr(devops.sys, "argv", ["kald-devops", "list_environments"])
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        devops.main()

    assert exc_info.value.code == 1
    assert "AZURE_DEVOPS_EXT_PAT" in capsys.readouterr().err


def test_main_passes_headers_to_azure_command(monkeypatch):
    captured = {}

    def fake_handler(args, headers):
        captured["headers"] = headers

    monkeypatch.setitem(devops.HANDLERS, "list_environments", fake_handler)
    monkeypatch.setattr(devops.sys, "argv", ["kald-devops", "list_environments"])
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "fake-pat")

    devops.main()

    assert captured["headers"]["Authorization"].startswith("Basic ")


def test_main_shows_usage_with_no_command(monkeypatch, capsys):
    monkeypatch.setattr(devops.sys, "argv", ["kald-devops"])

    with pytest.raises(SystemExit) as exc_info:
        devops.main()

    assert exc_info.value.code == 0
    assert "subcommands:" in capsys.readouterr().out
