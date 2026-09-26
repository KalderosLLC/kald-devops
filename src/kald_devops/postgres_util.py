#!/usr/bin/env python3

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile

import psycopg2
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
log = logging.getLogger(__name__)

REQUIRED_VARS = ["DB_HOST", "DB_USER", "DB_PASSWORD"]
# Azure Cosmos DB for PostgreSQL (Citus) clusters provision a database named
# "citus" (alongside the always-present "postgres" maintenance DB) -- the
# kpay schema and its tables live there, not in "postgres". Confirmed live
# against Prod: SELECT datname FROM pg_database only returns citus/postgres.
OPTIONAL_VARS = {"DB_NAME": "citus", "DB_PORT": "5432"}

# All connection info is consolidated into a single JSON store, keyed by
# environment. (Longer-term this moves to Azure Key Vault; this file is the
# local-storage stand-in until then.)
CONFIG_PATH = os.path.expanduser("~/.kald-postgres-util.json")

# Per phoenix-governance/README.md's "Environments" section -- these are the
# only valid targets for a governance SQL script, and thus the only
# ENVIRONMENT values this tool accepts.
ALLOWED_ENVIRONMENTS = ["qa", "preview", "sit", "uat", "prod"]

GITHUB_API = "https://api.github.com"
PR_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$")


def normalize_environment(raw: str | None) -> str | None:
    """Case-fold and validate ENVIRONMENT against phoenix-governance's environment
    list. Returns the canonical lowercase name, or None if raw is missing or
    not one of ALLOWED_ENVIRONMENTS."""
    if not raw:
        return None
    normalized = raw.strip().lower()
    return normalized if normalized in ALLOWED_ENVIRONMENTS else None


def load_config_store() -> dict:
    """Load the consolidated connection-info store. Returns {} if missing/empty/corrupt."""
    if not os.path.isfile(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Failed to read '%s': %s", CONFIG_PATH, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_config_store(store: dict) -> bool:
    """Write the consolidated connection-info store with owner-only permissions."""
    try:
        fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(store, f, indent=2, sort_keys=True)
            f.write("\n")
    except OSError as exc:
        log.error("Failed to write '%s': %s", CONFIG_PATH, exc)
        return False
    return True


def parse_pr_url(pr_url: str) -> tuple[str, str, int] | None:
    m = PR_URL_RE.match(pr_url.strip())
    if not m:
        return None
    owner, repo, number = m.groups()
    return owner, repo, int(number)


def environment_matches_path(path: str, environment: str) -> bool:
    """The phoenix-governance convention (per its README) is that a script's
    top-level folder name IS its intended target environment (qa/, preview/,
    sit/, uat/, prod/, ...). Comparison is case-insensitive and exact -- no
    aliases (e.g. Stage != sit) are assumed."""
    top_level = path.split("/", 1)[0]
    return top_level.lower() == environment.lower()


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def list_pr_sql_files(owner: str, repo: str, number: int, github_token: str) -> list[dict] | None:
    """Return the .sql file entries (GitHub pulls-files API shape) changed in a PR,
    or None (after logging the reason) on error. May be an empty list."""
    headers = _gh_headers(github_token)
    sql_files = []
    page = 1
    while True:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/files?per_page=100&page={page}"
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            log.error("Failed to list files for PR #%d: %s - %s", number, resp.status_code, body.get("message", resp.text))
            return None
        page_files = resp.json()
        if not page_files:
            break
        sql_files.extend(f for f in page_files if f.get("filename", "").lower().endswith(".sql"))
        page += 1
        if len(page_files) < 100:
            break
    return sql_files


def fetch_file_content(contents_url: str, github_token: str) -> str | None:
    """Fetch a single file's raw content via its GitHub contents_url (already ref-pinned)."""
    headers = {**_gh_headers(github_token), "Accept": "application/vnd.github.raw+json"}
    resp = requests.get(contents_url, headers=headers)
    if resp.status_code != 200:
        log.error("Failed to fetch contents from '%s': %s - %s", contents_url, resp.status_code, resp.text)
        return None
    return resp.text


def fetch_pr_sql_file(owner: str, repo: str, number: int, github_token: str,
                       sql_file_hint: str | None) -> tuple[str, str] | None:
    """Find the single .sql file changed in a PR and return (path, content).
    Returns None (after logging the reason) if zero or multiple candidates are found."""
    sql_files = list_pr_sql_files(owner, repo, number, github_token)
    if sql_files is None:
        return None

    if not sql_files:
        log.error("No .sql file found among the changed files in PR #%d.", number)
        return None

    if len(sql_files) > 1:
        if sql_file_hint:
            matches = [f for f in sql_files if sql_file_hint in f["filename"]]
            if len(matches) == 1:
                sql_files = matches
            else:
                log.error("SQL_FILE=%r matched %d of %d candidate .sql files in PR #%d: %s",
                          sql_file_hint, len(matches), len(sql_files), number,
                          ", ".join(f["filename"] for f in sql_files))
                return None
        else:
            log.error(
                "PR #%d has %d .sql files; re-run with SQL_FILE set to a distinguishing "
                "substring of the intended path: %s",
                number, len(sql_files), ", ".join(f["filename"] for f in sql_files),
            )
            return None

    target = sql_files[0]
    path = target["filename"]
    content = fetch_file_content(target["contents_url"], github_token)
    if content is None:
        return None
    return path, content


# ----------------------------------------------------------------------
# Self-managed transaction control
#
# apply_pr tests a script by wrapping it in an outer BEGIN ... ROLLBACK.
# That only works if the script itself never issues BEGIN/COMMIT/ROLLBACK --
# many governance scripts do (a bare top-level "BEGIN;" ... "COMMIT;" pair,
# meant for the operator to paste directly into psql). A script's own COMMIT
# takes effect immediately regardless of our wrapper, and the outer ROLLBACK
# we append afterward then has no open transaction left to roll back -- so a
# "rollback-only" run of such a script would actually commit for real.
# ----------------------------------------------------------------------

BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
LINE_COMMENT_RE = re.compile(r'--.*?$', re.MULTILINE)
# Requires the trailing ';' so this only matches a real transaction-control
# statement (e.g. "BEGIN;"), not the bare "BEGIN" that opens a DO $$ ... $$
# block (which is followed by DECLARE/statements, never a ';' on its own).
SELF_TXN_RE = re.compile(r'\b(BEGIN|COMMIT|ROLLBACK)\s*;', re.IGNORECASE)


def find_self_managed_transaction_control(sql_text: str) -> str | None:
    """Return the keyword (BEGIN/COMMIT/ROLLBACK) of the first self-managed
    transaction-control statement found in the script, or None if there isn't one."""
    stripped = BLOCK_COMMENT_RE.sub("", sql_text)
    stripped = LINE_COMMENT_RE.sub("", stripped)
    m = SELF_TXN_RE.search(stripped)
    return m.group(1).upper() if m else None


# ----------------------------------------------------------------------
# Expected-results verification
#
# Kalderos governance SQL scripts document their intended impact in an
# "-- Expected Results:" header comment, e.g.:
#   -- Expected Results:
#   -- 2 rows updated in kpay.client_file_subscription
#   -- 8 new rows in kpay.client_file_subscription_group
# and report actual counts at runtime via RAISE NOTICE + GET DIAGNOSTICS.
# ----------------------------------------------------------------------

EXPECTED_RESULTS_HEADER_RE = re.compile(r'^\s*--\s*expected\s+results?\s*:?\s*(.*)$', re.IGNORECASE)
COMMENT_LINE_RE = re.compile(r'^\s*--\s*(.*)$')
NEW_ROWS_RE = re.compile(r'(\d+)\s+new\s+rows?\b', re.IGNORECASE)
ROWS_VERB_RE = re.compile(
    r'(\d+)\s+rows?\s+(?:\w+\s+){0,2}?(inserted|created|added|updated|modified|deleted|removed)\b',
    re.IGNORECASE,
)
NOTICE_LINE_RE = re.compile(r'NOTICE:\s*(.*)$')
# The number must sit directly next to the verb -- e.g. "inserted 8 rows" or
# "8 rows updated" -- so an unrelated digit elsewhere in the message (a ticket
# number, an "Option 2" label, an id) can't be mistaken for a row count.
NOTICE_VERB_NUM_RE = re.compile(
    r'\b(inserted|created|added|updated|modified|deleted|removed)\b\s+(\d+)\b', re.IGNORECASE,
)
NOTICE_NUM_ROWS_VERB_RE = re.compile(
    r'\b(\d+)\b\s+rows?\s+(?:\w+\s+){0,2}?(inserted|created|added|updated|modified|deleted|removed)\b',
    re.IGNORECASE,
)

VERB_CATEGORY = {
    "inserted": "inserted", "created": "inserted", "added": "inserted",
    "updated": "updated", "modified": "updated",
    "deleted": "deleted", "removed": "deleted",
}


def parse_expected_counts(sql_text: str) -> dict[str, int] | None:
    """Parse the '-- Expected Results:' header comment out of a SQL script.
    Returns None if no such header is present at all; returns {} if the header
    is present but contains no machine-parseable row counts (e.g. free text)."""
    header_lines = []
    collecting = False
    for line in sql_text.splitlines():
        m = EXPECTED_RESULTS_HEADER_RE.match(line)
        if m:
            collecting = True
            first = m.group(1).strip()
            if first:
                header_lines.append(first)
            continue
        if collecting:
            m2 = COMMENT_LINE_RE.match(line)
            if not m2:
                break
            header_lines.append(m2.group(1).strip())
    if not collecting:
        return None

    text = " ".join(header_lines)
    counts: dict[str, int] = {}
    for m in NEW_ROWS_RE.finditer(text):
        counts["inserted"] = counts.get("inserted", 0) + int(m.group(1))
    remaining = NEW_ROWS_RE.sub("", text)
    for m in ROWS_VERB_RE.finditer(remaining):
        category = VERB_CATEGORY[m.group(2).lower()]
        counts[category] = counts.get(category, 0) + int(m.group(1))
    return counts


def parse_actual_counts(psql_output: str) -> dict[str, int]:
    """Parse row counts out of RAISE NOTICE lines in captured psql output."""
    counts: dict[str, int] = {}
    for line in psql_output.splitlines():
        m = NOTICE_LINE_RE.search(line)
        if not m:
            continue
        message = m.group(1)
        vn = NOTICE_VERB_NUM_RE.search(message)
        if vn:
            category = VERB_CATEGORY[vn.group(1).lower()]
            counts[category] = counts.get(category, 0) + int(vn.group(2))
            continue
        nv = NOTICE_NUM_ROWS_VERB_RE.search(message)
        if nv:
            category = VERB_CATEGORY[nv.group(2).lower()]
            counts[category] = counts.get(category, 0) + int(nv.group(1))
    return counts


def verify_expected_results(expected: dict[str, int] | None, actual: dict[str, int]) -> tuple[bool, str]:
    """Compare parsed expected vs. actual row counts. This is a best-effort,
    text-scraping check -- it cannot see counts a script never RAISE NOTICEs,
    and cannot verify a header with no numeric counts at all. Either case is
    treated as a failure, not just a numeric mismatch."""
    if expected is None:
        return False, "No 'Expected Results' header found in the SQL file - cannot verify row counts."
    if not expected:
        return False, "'Expected Results' header has no numeric row counts to verify against."
    mismatches = [
        f"{category}: expected {expected_count}, found {actual.get(category, 0)} in NOTICE output"
        for category, expected_count in expected.items()
        if actual.get(category, 0) != expected_count
    ]
    if mismatches:
        return False, "Row count mismatch - " + "; ".join(mismatches)
    return True, f"Row counts match expected results: {expected}"


def lint_sql_script(content: str) -> list[dict]:
    """Static, pre-flight checks shared by lint_pr and apply_pr: things that can be
    determined from the script's text alone, before ever touching a database.
    Returns one result per check: {"name", "passed", "detail"}."""
    checks = []

    self_txn = find_self_managed_transaction_control(content)
    if self_txn:
        checks.append({
            "name": "self-managed transaction control",
            "passed": False,
            "detail": (
                f"issues its own {self_txn}; statement - apply_pr's outer BEGIN ... ROLLBACK "
                "wrapper cannot safely test this script; a self-COMMIT would commit for real "
                "even without COMMIT=True."
            ),
        })
    else:
        checks.append({
            "name": "self-managed transaction control",
            "passed": True,
            "detail": "no top-level BEGIN;/COMMIT;/ROLLBACK; found -- safe to wrap in an outer BEGIN ... ROLLBACK.",
        })

    expected_counts = parse_expected_counts(content)
    if expected_counts is None:
        checks.append({
            "name": "'Expected Results' header",
            "passed": False,
            "detail": "no 'Expected Results' header found -- apply_pr cannot verify row counts against it.",
        })
    elif not expected_counts:
        checks.append({
            "name": "'Expected Results' header",
            "passed": False,
            "detail": "header present but has no numeric row counts -- apply_pr cannot verify against it.",
        })
    else:
        checks.append({
            "name": "'Expected Results' header",
            "passed": True,
            "detail": f"parsed numeric expectations: {expected_counts}.",
        })

    return checks


def _print_table(cols: list, rows: list) -> None:
    widths = [len(c) for c in cols]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len("NULL" if val is None else str(val)))
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    print(sep)
    print("| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)) + " |")
    print(sep)
    for row in rows:
        cells = [("NULL" if v is None else str(v)).ljust(widths[i]) for i, v in enumerate(row)]
        print("| " + " | ".join(cells) + " |")
    print(sep)


class PostgresConfigManager:

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_from_os_env(self) -> dict | None:
        config = {}
        missing = []

        for var in REQUIRED_VARS:
            value = os.environ.get(var)
            if not value:
                missing.append(var)
            else:
                config[var] = value

        if missing:
            log.error(
                "Missing required environment variable(s): %s", ", ".join(missing)
            )
            return None

        for var, default in OPTIONAL_VARS.items():
            value = os.environ.get(var, default)
            log.info(
                "Optional var %s resolved to: %s%s",
                var,
                value,
                " (default)" if var not in os.environ else "",
            )
            config[var] = value

        return config

    def _validate_config(self, config: dict) -> bool:
        missing = [v for v in REQUIRED_VARS if not config.get(v)]
        if missing:
            log.error(
                "Config is missing required key(s): %s", ", ".join(missing)
            )
            return False
        return True

    def _load_config(self, environment: str) -> dict | None:
        store = load_config_store()
        env_config = store.get(environment)
        if env_config is None:
            log.error(
                "No configuration found for environment '%s' in '%s'. Run 'set' to store it.",
                environment, CONFIG_PATH,
            )
            return None
        config = {}
        for var in REQUIRED_VARS:
            config[var] = env_config.get(var, "")
        for var, default in OPTIONAL_VARS.items():
            config[var] = env_config.get(var, default)
        if not self._validate_config(config):
            return None
        return config

    def _connect(self, config: dict):
        log.info(
            "Connecting to host=%s db=%s user=%s port=%s",
            config["DB_HOST"], config["DB_NAME"], config["DB_USER"], config["DB_PORT"],
        )
        try:
            return psycopg2.connect(
                host=config["DB_HOST"],
                dbname=config["DB_NAME"],
                user=config["DB_USER"],
                password=config["DB_PASSWORD"],
                port=int(config["DB_PORT"]),
            )
        except psycopg2.OperationalError as exc:
            log.error("Connection failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Subcommand: set
    # ------------------------------------------------------------------

    def cmd_set(self, environment: str) -> int:
        log.info("Running 'set' command for environment='%s'", environment)

        config = self._collect_from_os_env()
        if config is None:
            log.error("Aborting: one or more required variables are not set.")
            return 1

        store = load_config_store()
        store[environment] = config
        if not save_config_store(store):
            return 1

        log.info("Configuration for '%s' saved to '%s'", environment, CONFIG_PATH)
        return 0

    # ------------------------------------------------------------------
    # Subcommand: get
    # ------------------------------------------------------------------

    def cmd_get(self, environment: str) -> int:
        store = load_config_store()
        env_config = store.get(environment)
        if env_config is None:
            log.error("No configuration found for environment '%s' in '%s'.", environment, CONFIG_PATH)
            return 1

        for var in REQUIRED_VARS + list(OPTIONAL_VARS):
            value = env_config.get(var, OPTIONAL_VARS.get(var, ""))
            if var == "DB_PASSWORD" and value:
                value = "*" * 8
            print(f"{var}={value}")
        return 0

    # ------------------------------------------------------------------
    # Subcommand: connect
    # ------------------------------------------------------------------

    def cmd_connect(self, environment: str) -> int:
        log.info("Connecting to environment='%s'", environment)

        config = self._load_config(environment)
        if config is None:
            return 1

        conn = self._connect(config)
        if conn is None:
            return 1

        # Mirror psql default: each statement auto-commits unless the user
        # issues an explicit BEGIN/COMMIT block.
        conn.autocommit = True
        log.info("Connected. Type SQL ending with ';' to execute. Type \\q or quit to exit.")

        try:
            import readline  # noqa: F401 - enables arrow-key editing and history
        except ImportError:
            pass

        buf: list[str] = []
        while True:
            prompt = "sql> " if not buf else "  -> "
            try:
                line = input(prompt)
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                buf.clear()
                continue

            cmd = line.strip().lower()
            if not buf and cmd in ("\\q", "quit", "exit"):
                break
            if not buf and not line.strip():
                continue

            buf.append(line)

            if line.rstrip().endswith(";"):
                sql = "\n".join(buf).strip()
                buf.clear()
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql)
                        if cur.description:
                            cols = [d[0] for d in cur.description]
                            rows = cur.fetchall()
                            _print_table(cols, rows)
                            n = len(rows)
                            print(f"({n} {'row' if n == 1 else 'rows'})")
                        else:
                            n = cur.rowcount
                            print(f"({'?' if n < 0 else n} {'row' if n == 1 else 'rows'} affected)")
                except psycopg2.Error as exc:
                    log.error("%s", exc)
                    if not conn.autocommit:
                        conn.rollback()

        conn.close()
        log.info("Connection closed.")
        return 0

    # ------------------------------------------------------------------
    # Subcommand: test
    # ------------------------------------------------------------------

    def cmd_test(self, environment: str) -> int:
        log.info("Running 'test' command for environment='%s'", environment)

        config = self._load_config(environment)
        if config is None:
            return 1

        conn = self._connect(config)
        if conn is None:
            return 1

        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version(), current_database(), current_user, "
                    "inet_server_addr(), inet_server_port();"
                )
                row = cur.fetchone()
            version, database, user, addr, port = row
            log.info("Connection successful.")
            log.info("  Server version : %s", version)
            log.info("  Database       : %s", database)
            log.info("  User           : %s", user)
            log.info("  Server address : %s:%s", addr, port)
        except psycopg2.Error as exc:
            log.error("Test query failed: %s", exc)
            return 1
        finally:
            conn.close()
            log.info("Connection closed.")

        return 0

    # ------------------------------------------------------------------
    # Subcommand: execute
    # ------------------------------------------------------------------

    def _run_sql_file(self, config: dict, file_path: str, commit: bool) -> tuple[int, str]:
        if not shutil.which("psql"):
            log.error("'psql' not found on PATH - install the PostgreSQL client tools.")
            return 1, ""

        finalizer = "COMMIT" if commit else "ROLLBACK"

        cmd = [
            "psql",
            "-h", config["DB_HOST"],
            "-p", config["DB_PORT"],
            "-U", config["DB_USER"],
            "-d", config["DB_NAME"],
            "--echo-all",
            "-v", "ON_ERROR_STOP=1",
            "-c", "BEGIN",
            "-f", file_path,
            "-c", finalizer,
        ]

        env = os.environ.copy()
        env["PGPASSWORD"] = config["DB_PASSWORD"]

        if commit:
            log.info("Executing '%s' - changes WILL be committed.", file_path)
        else:
            log.info("Dry run: executing '%s' - changes will be rolled back.", file_path)

        # Capture combined stdout/stderr (psql sends RAISE NOTICE output to stderr)
        # while still streaming it to the terminal live, so callers can parse it
        # (e.g. to verify row counts) without losing the real-time --echo-all view.
        proc = subprocess.Popen(cmd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        lines = []
        for line in proc.stdout:
            print(line, end="")
            lines.append(line)
        proc.wait()
        output = "".join(lines)

        if proc.returncode != 0:
            log.error("psql exited with code %d - transaction was rolled back.", proc.returncode)
            return 1, output

        return 0, output

    def cmd_execute(self, environment: str) -> int:
        file_path = os.environ.get("FILE")
        if not file_path:
            log.error("Missing required environment variable: FILE")
            return 1
        if not os.path.isfile(file_path):
            log.error("FILE '%s' does not exist or is not a regular file.", file_path)
            return 1

        commit = os.environ.get("COMMIT", "").strip().lower() == "true"

        config = self._load_config(environment)
        if config is None:
            return 1

        rc, _ = self._run_sql_file(config, file_path, commit)
        if rc == 0:
            if commit:
                log.info("Execution complete - changes committed.")
            else:
                log.info("Dry run complete - transaction was rolled back, no changes committed.")
        return rc

    # ------------------------------------------------------------------
    # Subcommand: apply_pr
    # ------------------------------------------------------------------

    def cmd_apply_pr(self, environment: str) -> int:
        pr_url = os.environ.get("PR")
        if not pr_url:
            log.error("Missing required environment variable: PR")
            return 1

        parsed = parse_pr_url(pr_url)
        if parsed is None:
            log.error("PR '%s' is not a GitHub pull request URL (e.g. https://github.com/org/repo/pull/123)", pr_url)
            return 1
        owner, repo, number = parsed

        github_token = os.environ.get("GITHUB_TOKEN")
        if not github_token:
            log.error("Missing required environment variable: GITHUB_TOKEN")
            return 1

        commit = os.environ.get("COMMIT", "").strip().lower() == "true"
        sql_file_hint = os.environ.get("SQL_FILE")

        config = self._load_config(environment)
        if config is None:
            return 1

        found = fetch_pr_sql_file(owner, repo, number, github_token, sql_file_hint)
        if found is None:
            return 1
        path, content = found
        log.info("Found SQL file '%s' in %s/%s PR #%d", path, owner, repo, number)

        if not environment_matches_path(path, environment):
            top_level = path.split("/", 1)[0]
            log.error(
                "'%s' is under the '%s/' folder, but ENVIRONMENT='%s' was requested. "
                "Per phoenix-governance's convention, a script's top-level folder is its "
                "intended target environment -- refusing to run it against a mismatched "
                "environment.",
                path, top_level, environment,
            )
            return 1

        checks = lint_sql_script(content)
        for check in checks:
            log.info("  [%s] %s: %s", "PASS" if check["passed"] else "FAIL", check["name"], check["detail"])
        failed = [c for c in checks if not c["passed"]]
        if failed:
            log.error("'%s' failed pre-flight checks; refusing to run it automatically.", path)
            log.error("Run 'lint_pr' for details, fix the script, or test it manually.")
            return 1

        expected_counts = parse_expected_counts(content)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", prefix="apply_pr_", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            log.info("Rollback-only test run of '%s' against environment='%s'", path, environment)
            rc, output = self._run_sql_file(config, tmp_path, commit=False)
            if rc != 0:
                log.error("Rollback-only test run failed - not attempting a commit.")
                return 1
            log.info("Dry run complete - transaction was rolled back, no changes committed.")

            actual_counts = parse_actual_counts(output)
            verified, message = verify_expected_results(expected_counts, actual_counts)
            if not verified:
                log.error("Verification against the SQL file's 'Expected Results' failed: %s", message)
                log.error("Not proceeding - the rollback test succeeded, but its results could not be "
                          "confirmed against the script's declared expectations.")
                return 1
            log.info("Verification passed: %s", message)

            if not commit:
                log.info("Rollback-only test run succeeded and verified. Set COMMIT=True to apply for real.")
                return 0

            log.info("Rollback-only test run succeeded and verified, and COMMIT=True - applying '%s' for real.", path)
            rc, commit_output = self._run_sql_file(config, tmp_path, commit=True)
            if rc != 0:
                return rc

            commit_actual_counts = parse_actual_counts(commit_output)
            commit_verified, commit_message = verify_expected_results(expected_counts, commit_actual_counts)
            log.info("Expected results (from '%s' header): %s", path, expected_counts)
            log.info("Actual results from the commit run: %s", commit_actual_counts)
            if commit_verified:
                log.info("Verification passed: %s", commit_message)
            else:
                log.error(
                    "Verification against the SQL file's 'Expected Results' does not match the "
                    "commit run: %s. The change has already been committed -- this cannot be undone "
                    "by this tool; investigate manually.",
                    commit_message,
                )

            log.info("Execution complete - changes committed.")
            return rc
        finally:
            os.remove(tmp_path)

    # ------------------------------------------------------------------
    # Subcommand: lint_pr
    # ------------------------------------------------------------------

    def cmd_lint_pr(self) -> int:
        pr_url = os.environ.get("PR")
        if not pr_url:
            log.error("Missing required environment variable: PR")
            return 1

        parsed = parse_pr_url(pr_url)
        if parsed is None:
            log.error("PR '%s' is not a GitHub pull request URL (e.g. https://github.com/org/repo/pull/123)", pr_url)
            return 1
        owner, repo, number = parsed

        github_token = os.environ.get("GITHUB_TOKEN")
        if not github_token:
            log.error("Missing required environment variable: GITHUB_TOKEN")
            return 1

        sql_files = list_pr_sql_files(owner, repo, number, github_token)
        if sql_files is None:
            return 1
        if not sql_files:
            log.error("No .sql file found among the changed files in PR #%d.", number)
            return 1

        if len(sql_files) > 1:
            log.info(
                "PR #%d has %d .sql files; apply_pr requires exactly one (use SQL_FILE to disambiguate): %s",
                number, len(sql_files), ", ".join(f["filename"] for f in sql_files),
            )

        any_issues = False
        for f in sql_files:
            path = f["filename"]
            log.info("%s:", path)
            content = fetch_file_content(f["contents_url"], github_token)
            if content is None:
                any_issues = True
                continue
            checks = lint_sql_script(content)
            for check in checks:
                log.info("  [%s] %s: %s", "PASS" if check["passed"] else "FAIL", check["name"], check["detail"])
            if all(c["passed"] for c in checks):
                log.info("%s: OK", path)
            else:
                any_issues = True
                log.error("%s: FAILED", path)

        return 1 if any_issues else 0


# ----------------------------------------------------------------------
# Argument parser
# ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kald-postgres-util",
        description=(
            "Manage and connect to PostgreSQL environments via a consolidated "
            f"connection-info store ({CONFIG_PATH})."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) logging")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True

    # -- set -------------------------------------------------------------
    subparsers.add_parser(
        "set",
        help=(
            "Capture DB vars from the current environment and store them for ENVIRONMENT "
            f"in {CONFIG_PATH}."
        ),
    )

    # -- get -------------------------------------------------------------
    subparsers.add_parser(
        "get",
        help=(
            f"Print the stored DB connection info for ENVIRONMENT from {CONFIG_PATH} "
            "(DB_PASSWORD is masked)."
        ),
    )

    # -- test ------------------------------------------------------------
    subparsers.add_parser(
        "test",
        help=(
            "Load ENVIRONMENT's stored connection info, connect to PostgreSQL, "
            "and print server version, database, user, and address to confirm connectivity."
        ),
    )

    # -- connect ---------------------------------------------------------
    subparsers.add_parser(
        "connect",
        help=(
            "Load ENVIRONMENT's stored connection info, verify required vars, "
            "connect to PostgreSQL, and list members of the db_owner role."
        ),
    )

    # -- execute ---------------------------------------------------------
    subparsers.add_parser(
        "execute",
        help=(
            "Load ENVIRONMENT's stored connection info, connect to PostgreSQL, and execute "
            "the SQL file in FILE. Rolls back by default; set COMMIT=True to commit."
        ),
    )

    # -- apply_pr ----------------------------------------------------------
    subparsers.add_parser(
        "apply_pr",
        help=(
            "Fetch the .sql file changed in the GitHub PR at PR, test-apply it against "
            "ENVIRONMENT with a rollback, and commit only if that test succeeds and "
            "COMMIT=True."
        ),
    )

    # -- lint_pr -------------------------------------------------------------
    subparsers.add_parser(
        "lint_pr",
        help=(
            "Fetch the .sql file(s) changed in the GitHub PR at PR and check them for "
            "issues that would block apply_pr (self-managed transaction control, missing "
            "or non-numeric 'Expected Results' header). Needs only PR and GITHUB_TOKEN -- "
            "no ENVIRONMENT or database access."
        ),
    )

    return parser


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)
        logging.getLogger().handlers[0].setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s [%(funcName)s:%(lineno)d] %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")
        )
    log.debug("Parsed arguments: %s", args)

    manager = PostgresConfigManager()

    if args.command == "lint_pr":
        return 1 if manager.cmd_lint_pr() != 0 else 0

    raw_environment = os.environ.get("ENVIRONMENT")
    if not raw_environment:
        log.error("Missing required environment variable: ENVIRONMENT")
        return 1
    environment = normalize_environment(raw_environment)
    if environment is None:
        log.error(
            "ENVIRONMENT='%s' is not valid. Allowed values (case-insensitive, per "
            "phoenix-governance/README.md): %s",
            raw_environment, ", ".join(ALLOWED_ENVIRONMENTS),
        )
        return 1

    if args.command == "test":
        return manager.cmd_test(environment)

    if args.command == "set":
        return manager.cmd_set(environment)

    if args.command == "get":
        return manager.cmd_get(environment)

    if args.command == "connect":
        return manager.cmd_connect(environment)

    if args.command == "execute":
        return manager.cmd_execute(environment)

    if args.command == "apply_pr":
        return 1 if manager.cmd_apply_pr(environment) != 0 else 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
