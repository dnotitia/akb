"""Run the exact curated HTTP E2E suite used by hosted CI.

The list and count handling live here so an Apple VM gate and GitHub Actions
cannot silently drift.  A suite must report both counts in its final matching
``Results: N passed, M failed`` line; otherwise the gate fails closed.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from e2e_gate_observability import (
    emit_gate_event,
    signal_from_returncode,
    signal_name,
)


CURATED_SUITES: tuple[str, ...] = (
    "test_probes_e2e.sh",
    "test_mcp_e2e.sh",
    "test_edit_e2e.sh",
    "test_security_edge_e2e.sh",
    "test_pg_rbac_e2e.sh",
    "test_graph_replace_e2e.sh",
    "test_relations_rest_e2e.sh",
    "test_collection_lifecycle_e2e.sh",
    "test_history_rest_e2e.sh",
    "test_jwt_revocation_e2e.sh",
    "test_table_constraints_e2e.sh",
    "test_forbidden_permission_code_e2e.sh",
    "test_okf_export_import_e2e.sh",
    "test_publication_resolution_e2e.sh",
    "test_publications_e2e.sh",
)

# Deliberately empty.  Adding an exception removes measured coverage and must
# be an explicit, reviewed change to this source of truth.
EMPTY_COUNT_ALLOWED: frozenset[str] = frozenset()
SUMMARY_RE = re.compile(r"Results:\s*([0-9]+)\s+passed,\s*([0-9]+)\s+failed")


@dataclass(frozen=True)
class SuiteResult:
    name: str
    returncode: int
    passed: int
    failed: int
    summary: str | None
    output: str

    @property
    def counted(self) -> bool:
        return self.summary is not None

    @property
    def gate_failed(self) -> bool:
        if self.returncode != 0 or self.failed != 0:
            return True
        return self.passed == 0 and self.name not in EMPTY_COUNT_ALLOWED

    @property
    def signal(self) -> int | None:
        return signal_from_returncode(self.returncode)


def parse_assertion_summary(output: str) -> tuple[int, int, str] | None:
    """Return the last complete assertion summary, matching hosted CI."""

    matches = list(SUMMARY_RE.finditer(output))
    if not matches:
        return None
    match = matches[-1]
    return int(match.group(1)), int(match.group(2)), match.group(0)


def run_suite(name: str, repo_root: Path, env: dict[str, str] | None = None) -> SuiteResult:
    suite_path = repo_root / "backend" / "tests" / name
    if not suite_path.is_file():
        return SuiteResult(name, 127, 0, 0, None, f"suite not found: {suite_path}\n")

    completed = subprocess.run(
        ["bash", str(suite_path)],
        cwd=repo_root,
        env=env or os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    parsed = parse_assertion_summary(completed.stdout)
    if parsed is None:
        passed = failed = 0
        summary = None
    else:
        passed, failed, summary = parsed
    return SuiteResult(name, completed.returncode, passed, failed, summary, completed.stdout)


def _tail(text: str, lines: int = 80) -> str:
    chunks = text.splitlines()
    return "\n".join(chunks[-lines:]) if chunks else ""


def run_curated(repo_root: Path, env: dict[str, str] | None = None) -> int:
    total_pass = 0
    total_fail = 0
    failed_suites: list[str] = []

    for suite in CURATED_SUITES:
        emit_gate_event(
            {
                "event": "suite_start",
                "process": "suite_runner",
                "suite": suite,
            }
        )
        print(f"::group::{suite}", flush=True)
        result = run_suite(suite, repo_root, env)
        tail = _tail(result.output)
        if tail:
            print(tail, flush=True)
        total_pass += result.passed
        total_fail += result.failed
        print("::endgroup::", flush=True)
        print(
            f"▸ {suite}: {result.passed} passed, {result.failed} failed "
            f"(rc={result.returncode})",
            flush=True,
        )
        completion_event: dict[str, object] = {
            "event": "suite_complete",
            "process": "suite_runner",
            "suite": suite,
            "returncode": result.returncode,
            "passed": result.passed,
            "failed": result.failed,
            "summary": result.summary,
        }
        if result.signal is not None:
            completion_event["signal"] = result.signal
            completion_event["signal_name"] = signal_name(result.signal)
        emit_gate_event(completion_event)

        if result.gate_failed:
            failed_suites.append(suite)
            if result.returncode == 0 and result.failed == 0 and result.passed == 0:
                if result.summary is None:
                    print(
                        f"::error::{suite} produced no parsable assertion count: "
                        "no line matching 'Results: N passed, M failed' — a "
                        "summary carrying only one of the two counts does not parse",
                        flush=True,
                    )
                else:
                    print(
                        f"::error::{suite} produced no parsable assertion count: "
                        f"summary line reported 0 passed ({result.summary})",
                        flush=True,
                    )

    print("", flush=True)
    print("═════════════════════════════════════════", flush=True)
    print(f"TOTAL: {total_pass} passed, {total_fail} failed", flush=True)
    print("═════════════════════════════════════════", flush=True)
    gate_returncode = 1 if failed_suites else 0
    emit_gate_event(
        {
            "event": "gate_complete",
            "process": "suite_runner",
            "returncode": gate_returncode,
            "passed": total_pass,
            "failed": total_fail,
            "failed_suites": failed_suites,
        }
    )
    if failed_suites:
        print("FAILED SUITES:", flush=True)
        for suite in failed_suites:
            print(f"  - {suite}", flush=True)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
    )
    args = parser.parse_args(argv)
    return run_curated(args.repo_root.resolve())


if __name__ == "__main__":
    sys.exit(main())
