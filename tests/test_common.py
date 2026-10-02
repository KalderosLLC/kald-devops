"""Unit tests for kald_devops.common.

These cover the pure/deterministic shared infrastructure -- string formatting,
header building, env-var parsing, the HTTP retry wrapper, the shared pipeline-
token resolver, and the subcommand metadata used for --help/usage text --
without making any real HTTP calls.
"""
import argparse

import pytest

from kald_devops import common


# ---------------------------------------------------------------------------
# trunc / flex_width
# ---------------------------------------------------------------------------

def test_trunc_leaves_short_text_untouched():
    assert common.trunc("short", 20) == "short"


def test_trunc_truncates_long_text_with_ellipsis():
    result = common.trunc("a very long pipeline name", 10)
    assert len(result) == 10
    assert result.endswith("...")


def test_trunc_handles_none_and_empty():
    assert common.trunc(None, 10) == ""
    assert common.trunc("", 10) == ""


def test_flex_width_never_goes_below_floor(monkeypatch):
    # Patch common's own _term_width() wrapper rather than the shared shutil
    # module -- shutil.get_terminal_size() is also used by pytest's own
    # terminal-output code mid-run, so patching it globally corrupts test
    # output for the rest of the session instead of just this one call.
    monkeypatch.setattr(common, "_term_width", lambda: 40)
    # Overhead + fixed widths deliberately exceed the terminal width.
    assert common.flex_width(5, 100, 100) == 10


# ---------------------------------------------------------------------------
# extract_error
# ---------------------------------------------------------------------------

def test_extract_error_pulls_html_title():
    html = "<html><head><title>Azure DevOps Services | Sign In</title></head></html>"
    assert common.extract_error(html) == "Azure DevOps Services | Sign In"


def test_extract_error_falls_back_when_no_title():
    assert common.extract_error("plain text error body") == "HTTP error (see response for details)"


# ---------------------------------------------------------------------------
# make_headers / make_gh_headers
# ---------------------------------------------------------------------------

def test_make_headers_base64_encodes_pat():
    import base64
    headers = common.make_headers("secret-pat")
    assert headers["Content-Type"] == "application/json"
    scheme, token = headers["Authorization"].split(" ", 1)
    assert scheme == "Basic"
    assert base64.b64decode(token).decode() == ":secret-pat"


def test_make_gh_headers_shape():
    headers = common.make_gh_headers("ghp_token")
    assert headers["Authorization"] == "Bearer ghp_token"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


# ---------------------------------------------------------------------------
# parse_pipelines_env
# ---------------------------------------------------------------------------

def test_parse_pipelines_env_splits_and_strips(monkeypatch):
    monkeypatch.setenv("PIPELINES", " 1, KalderosLLC/phoenix ,2 ")
    assert common.parse_pipelines_env() == ["1", "KalderosLLC/phoenix", "2"]


def test_parse_pipelines_env_missing_returns_none(monkeypatch):
    monkeypatch.delenv("PIPELINES", raising=False)
    assert common.parse_pipelines_env() is None


# ---------------------------------------------------------------------------
# env_sort_key / parse_semver
# ---------------------------------------------------------------------------

def test_env_sort_key_orders_by_precedence():
    assert common.env_sort_key("dev") < common.env_sort_key("qa")
    assert common.env_sort_key("QA") == common.env_sort_key("qa")
    assert common.env_sort_key("prod") > common.env_sort_key("stage")


def test_env_sort_key_unknown_env_sorts_last():
    assert common.env_sort_key("some-custom-env") == len(common.ENV_PRECEDENCE)


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("v1.21.0", ("v", 1, 21, 0)),
        ("1.21.0", ("", 1, 21, 0)),
        ("v1.21.0-rc1", ("v", 1, 21, 0)),
        ("main", None),
        ("latest", None),
    ],
)
def test_parse_semver(tag, expected):
    assert common.parse_semver(tag) == expected


# ---------------------------------------------------------------------------
# resolve_pipeline_tokens
# ---------------------------------------------------------------------------

ALL_DEFS = [
    {"id": 1, "name": "PipelineOne"},
    {"id": 2, "name": "PipelineTwo"},
    {"id": 3, "name": "PipelineThree"},
]

REPO_BY_ID = {1: "KalderosLLC/one", 2: "KalderosLLC/two", 3: "KalderosLLC/three"}


def _fetch_repo(headers, definition_id):
    return REPO_BY_ID.get(definition_id, "-")


def test_resolve_pipeline_tokens_by_id_only_never_fetches_detail():
    def _boom(headers, definition_id):
        raise AssertionError("fetch_detail should not be called when every token is numeric")

    defs, detail_map = common.resolve_pipeline_tokens(
        None, ["2"], ALL_DEFS, _boom, "pipeline"
    )
    assert [d["id"] for d in defs] == [2]
    assert detail_map == {}


def test_resolve_pipeline_tokens_by_repo_name_resolves_all_defs():
    defs, detail_map = common.resolve_pipeline_tokens(
        None, ["kalderosllc/two"], ALL_DEFS, _fetch_repo, "pipeline"
    )
    assert [d["id"] for d in defs] == [2]
    assert detail_map == REPO_BY_ID


def test_resolve_pipeline_tokens_dedupes_when_matched_twice():
    defs, _ = common.resolve_pipeline_tokens(
        None, ["1", "KalderosLLC/one"], ALL_DEFS, _fetch_repo, "pipeline"
    )
    assert [d["id"] for d in defs] == [1]


def test_resolve_pipeline_tokens_unmatched_token_yields_no_defs(caplog):
    defs, _ = common.resolve_pipeline_tokens(
        None, ["no/such/repo"], ALL_DEFS, _fetch_repo, "pipeline"
    )
    assert defs == []


def test_resolve_pipeline_tokens_get_repo_unpacks_tuple_results():
    def _fetch_repo_and_stages(headers, definition_id):
        return REPO_BY_ID.get(definition_id, "-"), ["dev", "qa"]

    defs, detail_map = common.resolve_pipeline_tokens(
        None, ["KalderosLLC/two"], ALL_DEFS, _fetch_repo_and_stages,
        "release pipeline", get_repo=lambda v: v[0],
    )
    assert [d["id"] for d in defs] == [2]
    assert detail_map[2] == ("KalderosLLC/two", ["dev", "qa"])


# ---------------------------------------------------------------------------
# SUBCOMMAND_SPECS -- the single source of truth for --help/usage metadata
# ---------------------------------------------------------------------------

def test_subcommand_names_are_unique():
    names = [s["name"] for s in common.SUBCOMMAND_SPECS]
    assert len(names) == len(set(names))


def test_every_needs_azure_subcommand_documents_the_pat():
    for s in common.SUBCOMMAND_SPECS:
        if s["needs_azure"]:
            var_names = [v[0] for v in s["env_vars"]]
            assert "AZURE_DEVOPS_EXT_PAT" in var_names, s["name"]


def test_every_env_var_requirement_is_valid():
    for s in common.SUBCOMMAND_SPECS:
        for var, req, desc in s["env_vars"]:
            assert req in ("required", "optional"), (s["name"], var, req)
            assert desc, (s["name"], var)


def test_add_environment_to_pipelines_does_not_reference_stale_env_var_name():
    """Regression test for a real bug: the argparse help text once said
    ENVIRONMENT_NAME after the code had already been renamed to ENVIRONMENT."""
    sub = common._SUBCOMMAND_SPECS_BY_NAME["add_environment_to_pipelines"]
    assert "ENVIRONMENT_NAME" not in sub["argparse_help"]
    assert any(v[0] == "ENVIRONMENT" for v in sub["env_vars"])
    assert any(v[0] == "PIPELINES" for v in sub["env_vars"])


def test_create_releases_from_artifact_is_documented():
    """Regression test: this subcommand previously had no env-var help entries
    at all because it was missing from the (now-removed) separate metadata
    lists that main()/cmd_usage used to rely on."""
    sub = common._SUBCOMMAND_SPECS_BY_NAME["create_releases_from_artifact"]
    var_names = [v[0] for v in sub["env_vars"]]
    assert "PIPELINES" in var_names
    assert "SOURCE_RELEASE" in var_names


def test_cmd_usage_exits_zero_and_lists_every_subcommand(capsys):
    with pytest.raises(SystemExit) as exc_info:
        common.cmd_usage(argparse.Namespace())
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for s in common.SUBCOMMAND_SPECS:
        assert s["name"] in out


def test_print_subcommand_usage_includes_env_vars(capsys):
    common.print_subcommand_usage("deploy_pipelines")
    err = capsys.readouterr().err
    assert "BRANCH_OR_TAG" in err
    assert "ENVIRONMENTS" in err


def test_print_subcommand_usage_unknown_subcommand_does_not_raise(capsys):
    common.print_subcommand_usage("not_a_real_subcommand")
    err = capsys.readouterr().err
    assert "usage:" in err


# ---------------------------------------------------------------------------
# _with_retry
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def test_with_retry_returns_immediately_on_success(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda s: pytest.fail("should not sleep"))
    calls = []

    def fn():
        calls.append(1)
        return _FakeResponse(200)

    wrapped = common._with_retry(fn)
    assert wrapped().status_code == 200
    assert len(calls) == 1


def test_with_retry_retries_retryable_status_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(common.time, "sleep", lambda s: sleeps.append(s))

    responses = iter([_FakeResponse(429, {"Retry-After": "2"}), _FakeResponse(200)])

    def fn():
        return next(responses)

    wrapped = common._with_retry(fn)
    result = wrapped()
    assert result.status_code == 200
    assert sleeps == [2]


def test_with_retry_gives_up_after_max_retry_seconds(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    # First call to time.time() is "started"; the second (after the first
    # failed attempt) reports elapsed time already past MAX_RETRY_SECONDS.
    # time.time() is a process-wide clock (also used internally by the
    # logging module for record timestamps), so this keeps returning the
    # final value for any calls beyond the two the retry logic itself makes,
    # instead of raising StopIteration on an exhausted iterator.
    times = [1000.0, 1000.0 + common.MAX_RETRY_SECONDS + 1]

    def fake_time():
        return times.pop(0) if times else 1000.0 + common.MAX_RETRY_SECONDS + 1

    monkeypatch.setattr(common.time, "time", fake_time)

    def fn():
        return _FakeResponse(503)

    wrapped = common._with_retry(fn)
    result = wrapped()
    assert result.status_code == 503
