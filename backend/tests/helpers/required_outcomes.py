"""Fail when a required pytest gate skips or marks a selected test as expected to fail."""

import os

import pytest


_FAIL_ON_SKIP_ENV = "AKB_TEST_FAIL_ON_SKIP"
_NEEDS_RESOURCE = "needs_resource"
_HOW_TO_FIX = (
    "Make the test run, mark tests that use CI's PostgreSQL, BM25, Redis, or MinIO services "
    "with @pytest.mark.live_services, or declare other required setup with "
    f'@pytest.mark.{_NEEDS_RESOURCE}("<the resource and how to provide it>").'
)


def _fail_on_skip() -> bool:
    return os.environ.get(_FAIL_ON_SKIP_ENV) == "1"


def _skip_reason(longrepr: object) -> str:
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if not _fail_on_skip():
        return report
    if hasattr(report, "wasxfail"):
        what = f"is marked as an expected failure ({report.wasxfail or 'xfail'})"
        del report.wasxfail
    elif report.skipped:
        what = f"skipped ({_skip_reason(report.longrepr)})"
    else:
        return report
    report.outcome = "failed"
    report.longrepr = (
        f"{item.nodeid} {what}, and {_FAIL_ON_SKIP_ENV}=1 makes that a failure. {_HOW_TO_FIX}"
    )
    return report


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_make_collect_report(collector):
    report = yield
    if not report.skipped or not _fail_on_skip():
        return report
    report.outcome = "failed"
    report.longrepr = (
        f"{collector.nodeid} skipped while it was collected "
        f"({_skip_reason(report.longrepr)}), and {_FAIL_ON_SKIP_ENV}=1 makes that a failure. "
        "Move import-time skips into a fixture so CI can collect and select the tests."
    )
    return report
