#!/usr/bin/env python3
"""Print a one-line pass/fail summary from a pytest JUnit XML report.

Standard-library only -- runnable identically by a developer locally or by any
CI provider, independent of GitHub Actions' $GITHUB_STEP_SUMMARY mechanism:

    python scripts/summarize_tests.py test-results/junit.xml
"""
import sys
import xml.etree.ElementTree as ET


def summarize(junit_path: str) -> tuple[str, bool]:
    root = ET.parse(junit_path).getroot()
    suite = root.find("testsuite") if root.tag == "testsuites" else root

    tests = int(suite.get("tests", 0))
    failures = int(suite.get("failures", 0))
    errors = int(suite.get("errors", 0))
    skipped = int(suite.get("skipped", 0))
    passed = tests - failures - errors - skipped
    ok = failures == 0 and errors == 0

    message = (
        f"{'PASSED' if ok else 'FAILED'} -- {passed} passed, {failures} failed, "
        f"{errors} errors, {skipped} skipped (of {tests} total)"
    )
    return message, ok


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <junit.xml>", file=sys.stderr)
        return 2
    message, ok = summarize(argv[1])
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
