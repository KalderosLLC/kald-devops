#!/usr/bin/env python3

import argparse
import json
import logging
import os
import sys

# ---------------------------------------------------------------------------
# Logging – single root logger; all output goes to stdout via StreamHandler
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application class
# ---------------------------------------------------------------------------
class Application:
    """Top-level application object.  Each sub-command maps to a method."""

    # Environment-variable names used by the connect sub-command
    ENV_DB_SERVER   = "DB_SERVER"
    ENV_DB_PORT     = "DB_PORT"
    ENV_DB_NAME     = "DB_NAME"
    ENV_DB_USER     = "DB_USER"
    ENV_DB_PASSWORD = "DB_PASSWORD"
    ENV_DB_DRIVER   = "DB_DRIVER"

    DEFAULT_PORT   = 1433
    DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"

    def __init__(self, args: argparse.Namespace) -> None:
        self.log = logging.getLogger(self.__class__.__name__)
        self.args = args
        self.log.debug("Application initialised with args: %s", vars(args))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _require_env(self, name: str) -> str:
        """Return the value of *name* from the environment or abort."""
        value = os.environ.get(name)
        if not value:
            self.log.error(
                "Required environment variable '%s' is not set or is empty.", name
            )
            sys.exit(1)
        self.log.debug("Environment variable '%s' resolved successfully.", name)
        return value

    def _optional_env(self, name: str, default: str) -> str:
        """Return the value of *name* from the environment, falling back to *default*."""
        value = os.environ.get(name, default)
        source = "environment" if name in os.environ else "default"
        self.log.debug(
            "Environment variable '%s' = '%s' (source: %s).", name, value, source
        )
        return value

    def _build_connection_string(self) -> str:
        """Assemble and return a pyodbc-compatible connection string."""
        self.log.debug("Reading database connection parameters from environment.")

        server   = self._require_env(self.ENV_DB_SERVER)
        port     = self._optional_env(self.ENV_DB_PORT,   str(self.DEFAULT_PORT))
        database = self._require_env(self.ENV_DB_NAME)
        user     = self._require_env(self.ENV_DB_USER)
        password = self._require_env(self.ENV_DB_PASSWORD)
        driver   = self._optional_env(self.ENV_DB_DRIVER, self.DEFAULT_DRIVER)

        self.log.info(
            "Connection target  →  server=%s  port=%s  database=%s  user=%s  driver=%s",
            server, port, database, user, driver,
        )

        conn_str = (
            f"DRIVER={{{driver}}};"
            f"SERVER={server},{port};"
            f"DATABASE={database};"
            f"UID={user};"
            f"PWD={password};"
            "Authentication=ActiveDirectoryPassword;"
            "Encrypt=yes;"
            "TrustServerCertificate=no;"
            "Connection Timeout=30;"
        )
        self.log.debug("Connection string built (password redacted).")
        return conn_str

    # ------------------------------------------------------------------
    # Helpers – output
    # ------------------------------------------------------------------
    def _make_table(self, field_names: list[str]):
        """Return a PrettyTable configured to never wrap cell content."""
        from prettytable import PrettyTable  # noqa: PLC0415
        import shutil                        # noqa: PLC0415

        terminal_width = shutil.get_terminal_size(fallback=(220, 24)).columns
        self.log.debug("Terminal width detected as %d columns.", terminal_width)

        t = PrettyTable()
        t.field_names = field_names
        for name in field_names:
            t.align[name] = "l"

        # Disable PrettyTable's internal wrapping entirely; size each column
        # to its longest value so no content is folded or truncated.
        col_max = terminal_width // len(field_names)
        for name in field_names:
            t.max_width[name] = col_max

        return t

    # ------------------------------------------------------------------
    # Sub-command: connect
    # ------------------------------------------------------------------
    def cmd_connect(self) -> int:
        """Connect to a SQL Server database and validate the connection."""
        self.log.info("Executing sub-command: connect")

        try:
            import pyodbc  # noqa: PLC0415  (local import keeps startup fast)
        except ModuleNotFoundError:
            self.log.error(
                "The 'pyodbc' package is not installed.  "
                "Install it with:  pip install pyodbc"
            )
            return 1

        conn_str = self._build_connection_string()

        self.log.info("Attempting to open connection to SQL Server …")
        try:
            conn = pyodbc.connect(conn_str, autocommit=False)
            self.log.info("Connection established successfully.")
        except pyodbc.Error as exc:
            self.log.exception("Failed to connect to SQL Server: %s", exc)
            return 1

        try:
            cursor = conn.cursor()
            self.log.debug("Cursor created; executing validation query.")
            cursor.execute("SELECT @@VERSION AS server_version;")
            row = cursor.fetchone()
            if row:
                self.log.info("SQL Server version: %s", row.server_version.splitlines()[0])
            else:
                self.log.warning("Validation query returned no rows.")
            cursor.close()
            self.log.debug("Cursor closed.")
        except pyodbc.Error as exc:
            self.log.exception("Error while executing validation query: %s", exc)
            return 1
        finally:
            self.log.debug("Closing database connection.")
            conn.close()
            self.log.info("Connection closed.")

        self.log.info("sub-command 'connect' completed successfully.")
        return 0

    # ------------------------------------------------------------------
    # Sub-command: auth
    # ------------------------------------------------------------------
    def cmd_auth(self) -> int:
        """Query and display database role memberships as two PrettyTables."""
        self.log.info("Executing sub-command: auth")

        try:
            import pyodbc  # noqa: PLC0415
        except ModuleNotFoundError:
            self.log.error(
                "The 'pyodbc' package is not installed.  "
                "Install it with:  pip install pyodbc"
            )
            return 1

        try:
            from prettytable import PrettyTable  # noqa: PLC0415
        except ModuleNotFoundError:
            self.log.error(
                "The 'prettytable' package is not installed.  "
                "Install it with:  pip install prettytable"
            )
            return 1

        conn_str = self._build_connection_string()

        self.log.info("Attempting to open connection to SQL Server …")
        try:
            conn = pyodbc.connect(conn_str, autocommit=False)
            self.log.info("Connection established successfully.")
        except pyodbc.Error as exc:
            self.log.exception("Failed to connect to SQL Server: %s", exc)
            return 1

        # SQL that returns every (member, role) pair in the current database
        ROLE_QUERY = """
            SELECT
                dp_member.name  AS member_name,
                dp_member.type_desc AS member_type,
                dp_role.name    AS role_name
            FROM
                sys.database_role_members drm
                INNER JOIN sys.database_principals dp_role
                    ON dp_role.principal_id = drm.role_principal_id
                INNER JOIN sys.database_principals dp_member
                    ON dp_member.principal_id = drm.member_principal_id
            ORDER BY
                dp_member.name,
                dp_role.name;
        """

        try:
            cursor = conn.cursor()
            self.log.debug("Cursor created; executing role membership query.")
            cursor.execute(ROLE_QUERY)
            rows = cursor.fetchall()
            self.log.info("Role membership query returned %d row(s).", len(rows))
            cursor.close()
            self.log.debug("Cursor closed.")
        except pyodbc.Error as exc:
            self.log.exception("Error while executing role membership query: %s", exc)
            conn.close()
            return 1
        finally:
            self.log.debug("Closing database connection.")
            conn.close()
            self.log.info("Connection closed.")

        if not rows:
            self.log.warning("No role memberships found in this database.")
            return 0

        # ------------------------------------------------------------------
        # Aggregate raw rows into both mappings
        # ------------------------------------------------------------------
        self.log.debug("Aggregating role membership data.")

        member_to_roles: dict[tuple[str, str], list[str]] = {}
        role_to_members: dict[str, list[tuple[str, str]]] = {}

        for member_name, member_type, role_name in rows:
            member_key = (member_name, member_type)
            member_to_roles.setdefault(member_key, []).append(role_name)
            role_to_members.setdefault(role_name, []).append((member_name, member_type))

        self.log.debug(
            "Aggregation complete: %d unique member(s), %d unique role(s).",
            len(member_to_roles), len(role_to_members),
        )

        # ------------------------------------------------------------------
        # JSON output
        # ------------------------------------------------------------------
        if self.args.json:
            self.log.debug("JSON output requested; serialising results.")
            output = {
                "member_to_roles": [
                    {
                        "member":      member_name,
                        "member_type": member_type,
                        "roles":       sorted(role_list),
                    }
                    for (member_name, member_type), role_list
                    in sorted(member_to_roles.items())
                ],
                "role_to_members": [
                    {
                        "role":    role_name,
                        "members": [
                            {"member": m, "member_type": t}
                            for m, t in sorted(member_list)
                        ],
                    }
                    for role_name, member_list
                    in sorted(role_to_members.items())
                ],
            }
            print(json.dumps(output, indent=2))
            self.log.info("sub-command 'auth' completed successfully (JSON).")
            return 0

        # ------------------------------------------------------------------
        # Table 1 – Member → Roles
        # ------------------------------------------------------------------
        self.log.debug("Building Table 1: member → roles.")
        t1 = self._make_table(["Member", "Member Type", "Roles"])

        for (member_name, member_type), role_list in sorted(member_to_roles.items()):
            t1.add_row([member_name, member_type, ", ".join(sorted(role_list))])

        self.log.debug("Table 1 built with %d row(s).", len(member_to_roles))
        print(t1)

        # ------------------------------------------------------------------
        # Table 2 – Role → Members
        # ------------------------------------------------------------------
        self.log.debug("Building Table 2: role → members.")
        t2 = self._make_table(["Role", "Members"])

        for role_name, member_list in sorted(role_to_members.items()):
            members_str = ", ".join(
                f"{m} ({t})" for m, t in sorted(member_list)
            )
            t2.add_row([role_name, members_str])

        self.log.debug("Table 2 built with %d row(s).", len(role_to_members))
        print(t2)

        self.log.info("sub-command 'auth' completed successfully.")
        return 0

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def run(self) -> int:
        """Dispatch to the appropriate sub-command handler."""
        self.log.debug("Dispatching sub-command: '%s'", self.args.command)
        dispatch = {
            "connect": self.cmd_connect,
            "auth":    self.cmd_auth,
        }
        handler = dispatch.get(self.args.command)
        if handler is None:
            self.log.error("Unknown sub-command: '%s'", self.args.command)
            return 1
        return handler()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    logger.debug("Building argument parser.")

    parser = argparse.ArgumentParser(
        prog="app",
        description="SQL Server utility – standard subparser implementation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the console log level (default: INFO).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit command output as JSON instead of formatted tables.",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        metavar="<command>",
        help="Available sub-commands.",
    )
    subparsers.required = True

    # -- connect -----------------------------------------------------------
    connect_parser = subparsers.add_parser(
        "connect",
        help="Connect to a SQL Server database using environment variables.",
        description=(
            "Opens a connection to a SQL Server database.  All parameters are "
            "read from the environment:\n\n"
            f"  {Application.ENV_DB_SERVER}    (required) hostname or IP of the SQL Server instance\n"
            f"  {Application.ENV_DB_PORT}      (optional, default {Application.DEFAULT_PORT}) TCP port\n"
            f"  {Application.ENV_DB_NAME}      (required) target database name\n"
            f"  {Application.ENV_DB_USER}      (required) login username\n"
            f"  {Application.ENV_DB_PASSWORD}  (required) login password\n"
            f"  {Application.ENV_DB_DRIVER}    (optional) ODBC driver name\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # connect has no extra CLI flags – all config comes from the environment.
    connect_parser.set_defaults(command="connect")

    # -- auth --------------------------------------------------------------
    auth_parser = subparsers.add_parser(
        "auth",
        help="Display database role memberships as two formatted tables.",
        description=(
            "Queries sys.database_role_members and displays two tables:\n\n"
            "  Table 1 - each member and the roles it belongs to.\n"
            "  Table 2 - each role and the members it contains.\n\n"
            "Connection parameters are read from the same environment variables\n"
            "as the 'connect' sub-command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    auth_parser.set_defaults(command="auth")

    logger.debug("Argument parser built with %d sub-command(s).", 2)
    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    logger.debug("Application starting.")
    parser = build_parser()
    args = parser.parse_args()

    # Honour --log-level before doing anything else
    numeric_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(numeric_level)
    logger.debug("Log level set to '%s'.", args.log_level)

    app = Application(args)
    exit_code = app.run()

    logger.debug("Application exiting with code %d.", exit_code)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
