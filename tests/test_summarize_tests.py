"""Unit tests for scripts/summarize_tests.py."""
import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "summarize_tests.py"
_spec = importlib.util.spec_from_file_location("summarize_tests", _SCRIPT_PATH)
summarize_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summarize_tests)


def _write_junit(tmp_path, *, tests, failures=0, errors=0, skipped=0):
    path = tmp_path / "junit.xml"
    path.write_text(
        f'<?xml version="1.0"?>'
        f'<testsuites><testsuite name="pytest" tests="{tests}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}" time="0.1"></testsuite></testsuites>'
    )
    return str(path)


def test_summarize_all_passed(tmp_path):
    junit_path = _write_junit(tmp_path, tests=72)
    message, ok = summarize_tests.summarize(junit_path)
    assert ok is True
    assert "PASSED" in message
    assert "72 passed, 0 failed" in message


def test_summarize_with_failures_is_not_ok(tmp_path):
    junit_path = _write_junit(tmp_path, tests=72, failures=2)
    message, ok = summarize_tests.summarize(junit_path)
    assert ok is False
    assert "FAILED" in message
    assert "2 failed" in message


def test_summarize_with_errors_is_not_ok(tmp_path):
    junit_path = _write_junit(tmp_path, tests=10, errors=1)
    _, ok = summarize_tests.summarize(junit_path)
    assert ok is False


def test_summarize_counts_skipped_separately_from_passed(tmp_path):
    junit_path = _write_junit(tmp_path, tests=10, skipped=3)
    message, ok = summarize_tests.summarize(junit_path)
    assert ok is True
    assert "7 passed" in message
    assert "3 skipped" in message


def test_main_exits_nonzero_on_failure(tmp_path, capsys):
    junit_path = _write_junit(tmp_path, tests=5, failures=1)
    rc = summarize_tests.main(["summarize_tests.py", junit_path])
    assert rc == 1
    assert "FAILED" in capsys.readouterr().out


def test_main_exits_zero_on_success(tmp_path, capsys):
    junit_path = _write_junit(tmp_path, tests=5)
    rc = summarize_tests.main(["summarize_tests.py", junit_path])
    assert rc == 0
    assert "PASSED" in capsys.readouterr().out


def test_main_usage_error_without_path(capsys):
    rc = summarize_tests.main(["summarize_tests.py"])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err
