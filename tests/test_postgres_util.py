"""Unit tests for kald_devops.postgres_util.

Focused entirely on the pure, text/data-processing helpers that back lint_pr
and apply_pr's pre-flight checks -- no database connection or GitHub API call
is made anywhere in this file.
"""
from kald_devops import postgres_util as pu


# ---------------------------------------------------------------------------
# normalize_environment
# ---------------------------------------------------------------------------

def test_normalize_environment_accepts_known_values_case_insensitively():
    assert pu.normalize_environment("QA") == "qa"
    assert pu.normalize_environment(" Prod ") == "prod"


def test_normalize_environment_rejects_unknown_values():
    assert pu.normalize_environment("staging") is None


def test_normalize_environment_handles_missing_input():
    assert pu.normalize_environment(None) is None
    assert pu.normalize_environment("") is None


# ---------------------------------------------------------------------------
# parse_pr_url
# ---------------------------------------------------------------------------

def test_parse_pr_url_extracts_owner_repo_number():
    assert pu.parse_pr_url("https://github.com/KalderosLLC/phoenix/pull/123") == (
        "KalderosLLC", "phoenix", 123,
    )


def test_parse_pr_url_tolerates_trailing_slash_and_whitespace():
    assert pu.parse_pr_url("  https://github.com/KalderosLLC/phoenix/pull/123/  ") == (
        "KalderosLLC", "phoenix", 123,
    )


def test_parse_pr_url_rejects_non_matching_strings():
    assert pu.parse_pr_url("https://github.com/KalderosLLC/phoenix") is None
    assert pu.parse_pr_url("not a url") is None


# ---------------------------------------------------------------------------
# environment_matches_path
# ---------------------------------------------------------------------------

def test_environment_matches_path_is_case_insensitive_top_level_match():
    assert pu.environment_matches_path("qa/some/script.sql", "QA") is True


def test_environment_matches_path_rejects_different_environment():
    assert pu.environment_matches_path("prod/some/script.sql", "qa") is False


def test_environment_matches_path_does_not_alias_stage_and_sit():
    assert pu.environment_matches_path("sit/some/script.sql", "stage") is False


# ---------------------------------------------------------------------------
# _gh_headers
# ---------------------------------------------------------------------------

def test_gh_headers_shape():
    headers = pu._gh_headers("ghp_token")
    assert headers["Authorization"] == "Bearer ghp_token"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


# ---------------------------------------------------------------------------
# find_self_managed_transaction_control
# ---------------------------------------------------------------------------

def test_find_self_managed_transaction_control_detects_top_level_begin():
    sql = "BEGIN;\nUPDATE kpay.foo SET x = 1;\nCOMMIT;\n"
    assert pu.find_self_managed_transaction_control(sql) == "BEGIN"


def test_find_self_managed_transaction_control_ignores_do_block_begin():
    # A bare BEGIN (no trailing ';') opening a DO $$ ... $$ block is not
    # self-managed transaction control.
    sql = "DO $$\nBEGIN\n  RAISE NOTICE 'hi';\nEND $$;\n"
    assert pu.find_self_managed_transaction_control(sql) is None


def test_find_self_managed_transaction_control_ignores_commented_out_statements():
    sql = "-- COMMIT;\n/* ROLLBACK; */\nUPDATE kpay.foo SET x = 1;\n"
    assert pu.find_self_managed_transaction_control(sql) is None


def test_find_self_managed_transaction_control_detects_commit():
    sql = "UPDATE kpay.foo SET x = 1;\nCOMMIT;\n"
    assert pu.find_self_managed_transaction_control(sql) == "COMMIT"


# ---------------------------------------------------------------------------
# parse_expected_counts
# ---------------------------------------------------------------------------

def test_parse_expected_counts_returns_none_without_header():
    sql = "UPDATE kpay.foo SET x = 1;\n"
    assert pu.parse_expected_counts(sql) is None


def test_parse_expected_counts_parses_new_rows_and_verb_counts():
    sql = (
        "-- Expected Results:\n"
        "-- 2 rows updated in kpay.client_file_subscription\n"
        "-- 8 new rows in kpay.client_file_subscription_group\n"
        "UPDATE kpay.client_file_subscription SET active = true;\n"
    )
    assert pu.parse_expected_counts(sql) == {"updated": 2, "inserted": 8}


def test_parse_expected_counts_present_but_unparseable_returns_empty_dict():
    sql = "-- Expected Results:\n-- see ticket for details\nUPDATE kpay.foo SET x = 1;\n"
    assert pu.parse_expected_counts(sql) == {}


# ---------------------------------------------------------------------------
# parse_actual_counts
# ---------------------------------------------------------------------------

def test_parse_actual_counts_handles_verb_then_number():
    output = "psql:script.sql:12: NOTICE:  inserted 5 rows into kpay.foo\n"
    assert pu.parse_actual_counts(output) == {"inserted": 5}


def test_parse_actual_counts_handles_number_then_verb():
    output = "psql:script.sql:12: NOTICE:  3 rows updated in kpay.foo\n"
    assert pu.parse_actual_counts(output) == {"updated": 3}


def test_parse_actual_counts_sums_multiple_notices():
    output = (
        "NOTICE:  2 rows updated in kpay.foo\n"
        "NOTICE:  inserted 3 rows into kpay.foo\n"
        "NOTICE:  updated 1 rows in kpay.bar\n"
    )
    assert pu.parse_actual_counts(output) == {"updated": 3, "inserted": 3}


# ---------------------------------------------------------------------------
# verify_expected_results
# ---------------------------------------------------------------------------

def test_verify_expected_results_no_header_fails():
    ok, msg = pu.verify_expected_results(None, {"updated": 2})
    assert ok is False
    assert "No 'Expected Results' header" in msg


def test_verify_expected_results_unparseable_header_fails():
    ok, msg = pu.verify_expected_results({}, {"updated": 2})
    assert ok is False
    assert "no numeric row counts" in msg


def test_verify_expected_results_matching_counts_passes():
    ok, msg = pu.verify_expected_results({"updated": 2}, {"updated": 2})
    assert ok is True
    assert "match" in msg


def test_verify_expected_results_mismatched_counts_fails():
    ok, msg = pu.verify_expected_results({"updated": 2}, {"updated": 1})
    assert ok is False
    assert "Row count mismatch" in msg


# ---------------------------------------------------------------------------
# lint_sql_script
# ---------------------------------------------------------------------------

def test_lint_sql_script_flags_self_managed_transaction_and_missing_header():
    sql = "BEGIN;\nUPDATE kpay.foo SET x = 1;\nCOMMIT;\n"
    checks = pu.lint_sql_script(sql)
    by_name = {c["name"]: c for c in checks}
    assert by_name["self-managed transaction control"]["passed"] is False
    assert by_name["'Expected Results' header"]["passed"] is False


def test_lint_sql_script_passes_a_clean_script():
    sql = (
        "-- Expected Results:\n"
        "-- 2 rows updated in kpay.foo\n"
        "UPDATE kpay.foo SET x = 1;\n"
    )
    checks = pu.lint_sql_script(sql)
    assert all(c["passed"] for c in checks)
