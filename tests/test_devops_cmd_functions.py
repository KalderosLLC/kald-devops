"""Mocked-HTTP tests for three representative cmd_* functions, one per
command module, chosen to cover three distinct orchestration patterns:

- build.cmd_build:   simple trigger-then-poll (Azure DevOps build pipelines)
- release.cmd_deploy: two-phase safety gate -- preflight every pipeline
              (read-only) before triggering any of them (mutating)
- tag.cmd_tag:       GitHub tagging with a special-case cross-repo submodule
              update and strict abort-on-first-failure sequencing

No real network call is made anywhere in this file. Each test mocks at the
HTTP boundary (http_post, imported into each command module from
kald_devops.common) where that boundary is itself the interesting logic
(e.g. a build trigger's 200-vs-error response handling), and mocks already-
isolated helper functions (poll_ado_build, _deploy_preflight/_deploy_trigger,
_gh_resolve_*/_gh_create_tag/_gh_update_submodule) where going through raw
HTTP would mean re-simulating a polling loop or a chain of GitHub calls that
isn't what these tests are about -- the same seam-mocking approach used for
PostgresConfigManager. Mocks are applied to each function where it's
actually called from (e.g. build.fetch_build_pipelines, not
common.fetch_build_pipelines), matching how Python resolves names imported
via `from module import name`.
"""
import argparse
from unittest.mock import MagicMock

import pytest

from kald_devops.commands import build, release, tag


def _args():
    return argparse.Namespace(csv=False, json=False)


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json_data


# ---------------------------------------------------------------------------
# build.cmd_build
# ---------------------------------------------------------------------------

BUILD_DEFS = [
    {"id": 1, "name": "PipelineOne"},
    {"id": 2, "name": "PipelineTwo"},
]


def test_cmd_build_missing_pipelines_exits_1(monkeypatch):
    monkeypatch.delenv("PIPELINES", raising=False)
    monkeypatch.setattr(build, "fetch_build_pipelines", lambda headers: BUILD_DEFS)

    with pytest.raises(SystemExit) as exc_info:
        build.cmd_build(_args(), {})
    assert exc_info.value.code == 1


def test_cmd_build_triggers_and_reports_success(monkeypatch, capsys):
    monkeypatch.setenv("PIPELINES", "1,2")
    monkeypatch.setattr(build, "fetch_build_pipelines", lambda headers: BUILD_DEFS)
    monkeypatch.setattr(build, "http_post", lambda url, json, headers: _FakeResponse(200, {"id": 999}))
    monkeypatch.setattr(build, "poll_ado_build", lambda headers, build_id, **kw: ("succeeded", None, "0:01:00"))

    # Should not raise -- both pipelines succeed.
    build.cmd_build(_args(), {})
    out = capsys.readouterr().out
    assert "PipelineOne" in out
    assert "PipelineTwo" in out
    assert "succeeded" in out


def test_cmd_build_trigger_failure_still_polls_the_others(monkeypatch, capsys):
    monkeypatch.setenv("PIPELINES", "1,2")
    monkeypatch.setattr(build, "fetch_build_pipelines", lambda headers: BUILD_DEFS)

    def fake_http_post(url, json, headers):
        if json["definition"]["id"] == 1:
            return _FakeResponse(400, text="<title>Bad Request</title>")
        return _FakeResponse(200, {"id": 999})

    monkeypatch.setattr(build, "http_post", fake_http_post)
    monkeypatch.setattr(build, "poll_ado_build", lambda headers, build_id, **kw: ("succeeded", None, "0:01:00"))

    with pytest.raises(SystemExit) as exc_info:
        build.cmd_build(_args(), {})
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    # PipelineTwo still got triggered and polled despite PipelineOne's failed trigger.
    assert "PipelineTwo" in out


def test_cmd_build_all_triggers_fail_exits_1_without_polling(monkeypatch):
    monkeypatch.setenv("PIPELINES", "1,2")
    monkeypatch.setattr(build, "fetch_build_pipelines", lambda headers: BUILD_DEFS)
    monkeypatch.setattr(build, "http_post", lambda url, json, headers: _FakeResponse(400))
    poll = MagicMock()
    monkeypatch.setattr(build, "poll_ado_build", poll)

    with pytest.raises(SystemExit) as exc_info:
        build.cmd_build(_args(), {})
    assert exc_info.value.code == 1
    poll.assert_not_called()


def test_cmd_build_poll_failure_exits_1(monkeypatch):
    monkeypatch.setenv("PIPELINES", "1")
    monkeypatch.setattr(build, "fetch_build_pipelines", lambda headers: BUILD_DEFS)
    monkeypatch.setattr(build, "http_post", lambda url, json, headers: _FakeResponse(200, {"id": 999}))
    monkeypatch.setattr(build, "poll_ado_build", lambda headers, build_id, **kw: ("failed", "compilationError", "0:00:30"))

    with pytest.raises(SystemExit) as exc_info:
        build.cmd_build(_args(), {})
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# release.cmd_deploy -- the two-phase safety gate
# ---------------------------------------------------------------------------

RELEASE_DEFS = [
    {"id": 10, "name": "ReleaseOne"},
    {"id": 20, "name": "ReleaseTwo"},
]


def _patch_deploy_fetches(monkeypatch, stages=("dev", "qa", "prod")):
    monkeypatch.setattr(release, "fetch_release_definitions", lambda headers: RELEASE_DEFS)
    monkeypatch.setattr(release, "fetch_pipeline_stages", lambda headers, definition_id: list(stages))


def test_cmd_deploy_missing_required_env_exits_1(monkeypatch):
    monkeypatch.delenv("BRANCH_OR_TAG", raising=False)
    monkeypatch.delenv("ENVIRONMENTS", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        release.cmd_deploy(_args(), {})
    assert exc_info.value.code == 1


def test_cmd_deploy_invalid_environment_exits_1_before_any_preflight(monkeypatch):
    monkeypatch.setenv("BRANCH_OR_TAG", "main")
    monkeypatch.setenv("ENVIRONMENTS", "staging")  # not in stages below
    monkeypatch.setenv("PIPELINES", "10,20")
    _patch_deploy_fetches(monkeypatch, stages=("dev", "qa", "prod"))
    preflight = MagicMock()
    monkeypatch.setattr(release, "_deploy_preflight", preflight)

    with pytest.raises(SystemExit) as exc_info:
        release.cmd_deploy(_args(), {})
    assert exc_info.value.code == 1
    preflight.assert_not_called()


def test_cmd_deploy_one_preflight_failure_blocks_all_triggers(monkeypatch):
    """The core safety invariant: if any pipeline fails its (read-only)
    pre-flight check, _deploy_trigger must never be called for ANY pipeline --
    not even the ones whose pre-flight succeeded."""
    monkeypatch.setenv("BRANCH_OR_TAG", "v1.2.3")
    monkeypatch.setenv("ENVIRONMENTS", "qa")
    monkeypatch.setenv("PIPELINES", "10,20")
    _patch_deploy_fetches(monkeypatch)

    def fake_preflight(rd, source_ref, branch, environments_list, headers):
        if rd["id"] == 10:
            return rd["id"], rd["name"], "No release found built from 'refs/tags/v1.2.3'", None
        return rd["id"], rd["name"], None, {
            "rid": 555, "rname": "Release-5", "release_url": "http://example/5",
            "release_env_map": {"qa": 1}, "valid_environments": ["qa"],
        }

    monkeypatch.setattr(release, "_deploy_preflight", fake_preflight)
    trigger = MagicMock()
    monkeypatch.setattr(release, "_deploy_trigger", trigger)

    with pytest.raises(SystemExit) as exc_info:
        release.cmd_deploy(_args(), {})
    assert exc_info.value.code == 1
    trigger.assert_not_called()


def test_cmd_deploy_all_preflight_pass_triggers_and_reports_success(monkeypatch, capsys):
    monkeypatch.setenv("BRANCH_OR_TAG", "v1.2.3")
    monkeypatch.setenv("ENVIRONMENTS", "qa")
    monkeypatch.setenv("PIPELINES", "10,20")
    _patch_deploy_fetches(monkeypatch)

    def fake_preflight(rd, source_ref, branch, environments_list, headers):
        return rd["id"], rd["name"], None, {
            "rid": rd["id"] * 100, "rname": f"Release-{rd['id']}", "release_url": "http://example",
            "release_env_map": {"qa": 1}, "valid_environments": ["qa"],
        }

    def fake_trigger(definition_id, definition_name, release_info, branch, headers):
        row = (definition_id, definition_name, branch, release_info["rname"], "qa",
               release_info["release_url"], release_info["rid"], 1)
        return [row], []

    monkeypatch.setattr(release, "_deploy_preflight", fake_preflight)
    monkeypatch.setattr(release, "_deploy_trigger", fake_trigger)
    monkeypatch.setattr(release, "poll_ado_environment", lambda headers, rid, env_id, **kw: ("succeeded", None))
    monkeypatch.setattr(release, "get_current_version", lambda headers, pid, environment: ("v1.2.3", "2026-01-01 00:00 UTC"))

    release.cmd_deploy(_args(), {})  # must not raise
    out = capsys.readouterr().out
    assert "ReleaseOne" in out
    assert "ReleaseTwo" in out
    assert "succeeded" in out


def test_cmd_deploy_trigger_error_after_preflight_still_exits_1(monkeypatch):
    monkeypatch.setenv("BRANCH_OR_TAG", "v1.2.3")
    monkeypatch.setenv("ENVIRONMENTS", "qa")
    monkeypatch.setenv("PIPELINES", "10,20")
    _patch_deploy_fetches(monkeypatch)

    def fake_preflight(rd, source_ref, branch, environments_list, headers):
        return rd["id"], rd["name"], None, {
            "rid": rd["id"] * 100, "rname": f"Release-{rd['id']}", "release_url": "http://example",
            "release_env_map": {"qa": 1}, "valid_environments": ["qa"],
        }

    def fake_trigger(definition_id, definition_name, release_info, branch, headers):
        # Every pipeline fails to actually queue, despite passing pre-flight.
        return [], [(definition_id, definition_name, "HTTP 409 - conflict")]

    monkeypatch.setattr(release, "_deploy_preflight", fake_preflight)
    monkeypatch.setattr(release, "_deploy_trigger", fake_trigger)

    with pytest.raises(SystemExit) as exc_info:
        release.cmd_deploy(_args(), {})
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# tag.cmd_tag -- GitHub tagging with the phoenix/phoenix-data-gateway special case
# ---------------------------------------------------------------------------

PHOENIX = "KalderosLLC/phoenix"
PHOENIX_PDG = "KalderosLLC/phoenix-data-gateway"


def test_cmd_tag_missing_required_env_exits_1(monkeypatch):
    monkeypatch.delenv("TAG", raising=False)
    monkeypatch.delenv("REPOSITORIES", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        tag.cmd_tag(_args())
    assert exc_info.value.code == 1


def test_cmd_tag_empty_repositories_exits_1(monkeypatch):
    monkeypatch.setenv("TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("REPOSITORIES", "  ,  ")
    with pytest.raises(SystemExit) as exc_info:
        tag.cmd_tag(_args())
    assert exc_info.value.code == 1


def test_cmd_tag_plain_repo_resolves_then_creates_tag(monkeypatch):
    monkeypatch.setenv("TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("REPOSITORIES", "KalderosLLC/phoenix-snowflake-gateway")

    resolve_branch = MagicMock(return_value="abc123")
    create_tag = MagicMock(return_value="abc123")
    monkeypatch.setattr(tag, "_gh_resolve_branch", resolve_branch)
    monkeypatch.setattr(tag, "_gh_create_tag", create_tag)

    tag.cmd_tag(_args())  # must not raise

    resolve_branch.assert_called_once_with({"Authorization": "Bearer tok", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, "KalderosLLC/phoenix-snowflake-gateway", "main")
    create_tag.assert_called_once()


def test_cmd_tag_plain_repo_resolve_failure_exits_1_before_create(monkeypatch):
    monkeypatch.setenv("TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("REPOSITORIES", "KalderosLLC/phoenix-snowflake-gateway")

    monkeypatch.setattr(tag, "_gh_resolve_branch", lambda *a, **k: None)
    create_tag = MagicMock()
    monkeypatch.setattr(tag, "_gh_create_tag", create_tag)

    with pytest.raises(SystemExit) as exc_info:
        tag.cmd_tag(_args())
    assert exc_info.value.code == 1
    create_tag.assert_not_called()


def test_cmd_tag_stops_at_first_repo_failure_never_reaches_second(monkeypatch):
    """Documents the existing (non-obvious) behavior: cmd_tag aborts the whole
    process on the first repository that fails, rather than continuing on to
    tag the remaining repositories."""
    monkeypatch.setenv("TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("REPOSITORIES", "KalderosLLC/repo-one,KalderosLLC/repo-two")

    resolve_branch = MagicMock(return_value=None)  # repo-one fails immediately
    create_tag = MagicMock()
    monkeypatch.setattr(tag, "_gh_resolve_branch", resolve_branch)
    monkeypatch.setattr(tag, "_gh_create_tag", create_tag)

    with pytest.raises(SystemExit):
        tag.cmd_tag(_args())

    resolve_branch.assert_called_once()  # repo-two's resolve was never attempted
    create_tag.assert_not_called()


def test_cmd_tag_phoenix_runs_pdg_tag_submodule_update_then_phoenix_tag_in_order(monkeypatch):
    monkeypatch.setenv("TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("REPOSITORIES", PHOENIX)

    calls = []

    def fake_resolve_tag_or_branch(gh_headers, repo, name):
        calls.append(("resolve_tag_or_branch", repo, name))
        return "pdg-sha", "branch"

    def fake_create_tag(gh_headers, repo, tag, sha):
        calls.append(("create_tag", repo, tag, sha))
        return f"{sha}-tagged"

    def fake_update_submodule(gh_headers, repo, branch, submodule_path, submodule_sha, tag):
        calls.append(("update_submodule", repo, branch, submodule_sha))
        return "phoenix-new-sha"

    monkeypatch.setattr(tag, "_gh_resolve_tag_or_branch", fake_resolve_tag_or_branch)
    monkeypatch.setattr(tag, "_gh_create_tag", fake_create_tag)
    monkeypatch.setattr(tag, "_gh_update_submodule", fake_update_submodule)

    tag.cmd_tag(_args())  # must not raise

    # Exact order matters: PDG resolve -> PDG tag -> submodule update -> phoenix tag.
    assert calls == [
        ("resolve_tag_or_branch", PHOENIX_PDG, "main"),
        ("create_tag", PHOENIX_PDG, "v1.2.3", "pdg-sha"),
        ("update_submodule", PHOENIX, "main", "pdg-sha-tagged"),
        ("create_tag", PHOENIX, "v1.2.3", "phoenix-new-sha"),
    ]


def test_cmd_tag_phoenix_and_pdg_both_listed_tags_pdg_only_once(monkeypatch):
    """PDG is handled implicitly when phoenix is tagged -- listing it
    explicitly alongside phoenix must not cause it to be tagged twice."""
    monkeypatch.setenv("TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("REPOSITORIES", f"{PHOENIX},{PHOENIX_PDG}")

    create_tag_calls = []
    monkeypatch.setattr(tag, "_gh_resolve_tag_or_branch", lambda *a, **k: ("pdg-sha", "branch"))
    monkeypatch.setattr(tag, "_gh_update_submodule", lambda *a, **k: "phoenix-new-sha")

    def fake_create_tag(gh_headers, repo, tag_name, sha):
        create_tag_calls.append(repo)
        return f"{sha}-tagged"

    monkeypatch.setattr(tag, "_gh_create_tag", fake_create_tag)
    resolve_branch = MagicMock()
    monkeypatch.setattr(tag, "_gh_resolve_branch", resolve_branch)

    tag.cmd_tag(_args())  # must not raise

    assert create_tag_calls == [PHOENIX_PDG, PHOENIX]
    resolve_branch.assert_not_called()  # PDG never goes through the "remaining" plain-repo path


def test_cmd_tag_pdg_resolve_failure_aborts_before_submodule_update(monkeypatch):
    monkeypatch.setenv("TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("REPOSITORIES", PHOENIX)

    monkeypatch.setattr(tag, "_gh_resolve_tag_or_branch", lambda *a, **k: (None, None))
    update_submodule = MagicMock()
    create_tag = MagicMock()
    monkeypatch.setattr(tag, "_gh_update_submodule", update_submodule)
    monkeypatch.setattr(tag, "_gh_create_tag", create_tag)

    with pytest.raises(SystemExit) as exc_info:
        tag.cmd_tag(_args())
    assert exc_info.value.code == 1
    update_submodule.assert_not_called()
    create_tag.assert_not_called()


def test_cmd_tag_submodule_update_failure_aborts_before_phoenix_tag(monkeypatch):
    monkeypatch.setenv("TAG", "v1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("REPOSITORIES", PHOENIX)

    monkeypatch.setattr(tag, "_gh_resolve_tag_or_branch", lambda *a, **k: ("pdg-sha", "branch"))
    create_tag_calls = []
    monkeypatch.setattr(tag, "_gh_create_tag", lambda gh_headers, repo, tag_name, sha: create_tag_calls.append(repo) or f"{sha}-tagged")
    monkeypatch.setattr(tag, "_gh_update_submodule", lambda *a, **k: None)

    with pytest.raises(SystemExit) as exc_info:
        tag.cmd_tag(_args())
    assert exc_info.value.code == 1
    # PDG was tagged before the submodule update was attempted, but phoenix itself never was.
    assert create_tag_calls == [PHOENIX_PDG]
