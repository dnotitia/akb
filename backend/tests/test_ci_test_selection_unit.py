"""Exercise the backend CI guards through pytest's real collection and reporting."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit_only
_BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("mode", "selector", "extra", "exit_code", "message"),
    [
        ("UNIT", ["-k", "not _e2e"], "", 0, "1 passed, 1 skipped"),
        ("UNIT", ["-k", "not _e2e and not test_required"], "", 4, "selector deselected"),
        ("UNIT", [], "@pytest.mark.skip(reason='missing service')", 1, "test_required skipped"),
        ("UNIT", [], "@pytest.mark.xfail(reason='expected failure')", 1, "expected failure"),
        ("LIVE", ["-m", "not needs_resource and not unit_only"], "", 1, "test_optional skipped"),
    ],
)
def test_ci_test_selection(tmp_path, mode, selector, extra, exit_code, message):
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import (pytest_collection_modifyitems, pytest_deselected, "
        "pytest_make_collect_report, pytest_runtest_makereport)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_cases.py").write_text(
        f"""import pytest

@pytest.mark.unit_only
{extra}
def test_required():
    pass

def test_optional():
    pytest.skip("missing service")
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "AKB_TEST_REQUIRE_UNIT_ONLY": "1" if mode == "UNIT" else "0",
        "AKB_TEST_FAIL_ON_SKIP": "1" if mode == "LIVE" else "0",
    }
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q",
            "-c", str(_BACKEND / "pyproject.toml"),
            "--confcutdir", str(tmp_path), str(tmp_path), *selector,
        ],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    assert result.returncode == exit_code, output
    assert message in output, output
    if mode == "LIVE":
        assert "1 deselected" in output, output
