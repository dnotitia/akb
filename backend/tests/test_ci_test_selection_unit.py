"""Exercise pytest selection and skip reporting as configured for backend CI."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_UNIT = "not live_services and not needs_resource"
_LIVE = "live_services and not needs_resource"


def _run_pytest(tmp_path, selector, source):
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import (pytest_collection_modifyitems, pytest_deselected, "
        "pytest_make_collect_report, pytest_runtest_makereport, pytest_terminal_summary)\n",
        encoding="utf-8",
    )
    (tmp_path / "test_cases.py").write_text("import pytest\n\n" + source, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-v", "--tb=short", "--strict-markers",
            "-c", str(_BACKEND / "pyproject.toml"),
            "--confcutdir", str(tmp_path), str(tmp_path), "-m", selector,
        ],
        cwd=_BACKEND,
        env={**os.environ, "AKB_TEST_FAIL_ON_SKIP": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("selector", "selected"),
    [
        pytest.param(_UNIT, "test_unit", id="unit"),
        pytest.param(_LIVE, "test_live", id="live"),
    ],
)
def test_ci_selects_each_case_in_one_job(tmp_path, selector, selected):
    exit_code, output = _run_pytest(
        tmp_path, selector,
        """
def test_unit():
    pass

@pytest.mark.live_services
def test_live():
    pass

@pytest.mark.needs_resource("a local object store at TEST_STORE_URL")
def test_external():
    pytest.skip("missing store")
""",
    )
    assert exit_code == 0, output
    assert f"::{selected} PASSED" in output, output
    assert "1 passed, 2 deselected" in output, output
    assert "a local object store at TEST_STORE_URL" in output, output


@pytest.mark.parametrize(
    ("selector", "mark"),
    [
        pytest.param(_UNIT, "", id="unit"),
        pytest.param(_LIVE, "@pytest.mark.live_services\n", id="live"),
    ],
)
@pytest.mark.parametrize(
    ("source", "exit_code", "message"),
    [
        pytest.param(
            "{mark}def test_selected():\n    pytest.skip('missing service')",
            1, "test_selected skipped (Skipped: missing service)", id="skip",
        ),
        pytest.param(
            "{mark}@pytest.mark.xfail(reason='pending fix')\ndef test_selected():\n    assert False",
            1, "expected failure (pending fix)", id="xfail",
        ),
        pytest.param(
            "pytest.skip('missing service', allow_module_level=True)",
            2, "skipped while it was collected", id="collection-skip",
        ),
    ],
)
def test_ci_rejects_skips_and_xfails(tmp_path, selector, mark, source, exit_code, message):
    status, output = _run_pytest(tmp_path, selector, source.format(mark=mark))
    assert status == exit_code, output
    assert message in output, output


@pytest.mark.parametrize(
    ("mark", "exit_code", "message"),
    [
        pytest.param(
            "@pytest.mark.needs_resource(' ')", 4, "takes exactly one non-empty string",
            id="missing-resource-reason",
        ),
        pytest.param("@pytest.mark.live_service", 2, "not found in", id="unregistered-marker"),
    ],
)
def test_ci_validates_marker_declarations(tmp_path, mark, exit_code, message):
    status, output = _run_pytest(tmp_path, _UNIT, f"{mark}\ndef test_case():\n    pass")
    assert status == exit_code, output
    assert message in output, output
