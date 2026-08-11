"""Run and validate the exact curated HTTP E2E suite used by hosted CI.

The list and count handling live here so an isolated host gate and GitHub Actions
cannot silently drift. Every shell E2E suite must be either executed or explicitly
deferred with a reason. A suite must report both counts in its final matching
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
    "test_vault_scope_e2e.sh",
    "test_vault_scope_sql_e2e.sh",
    "test_vault_write_policy_e2e.sh",
    "test_graph_replace_e2e.sh",
    "test_relations_rest_e2e.sh",
    "test_collection_lifecycle_e2e.sh",
    "test_history_rest_e2e.sh",
    "test_author_resolution_e2e.sh",
    "test_agent_sessions_e2e.sh",
    "test_jwt_revocation_e2e.sh",
    "test_table_constraints_e2e.sh",
    "test_row_read_e2e.sh",
    "test_row_write_e2e.sh",
    "test_forbidden_permission_code_e2e.sh",
    "test_okf_export_import_e2e.sh",
    "test_resource_hash_e2e.sh",
    "test_events_emit_e2e.sh",
    "test_s3_delete_outbox_e2e.sh",
    "test_publication_resolution_e2e.sh",
    "test_publications_e2e.sh",
)


@dataclass(frozen=True)
class DeferredSuiteGroup:
    """Suites intentionally kept outside the bounded hosted gate."""

    reason: str
    suites: tuple[str, ...]


# This is the reviewed opt-out side of the manifest. Keep reasons actionable:
# adding a file here makes its lack of hosted execution intentional and visible.
DEFERRED_SUITE_GROUPS: tuple[DeferredSuiteGroup, ...] = (
    DeferredSuiteGroup(
        reason=(
            "current assertion expects 403 for a reused non-admin credential, "
            "while password invalidation returns 401; align the contract first"
        ),
        suites=("test_auth_password_e2e.sh",),
    ),
    DeferredSuiteGroup(
        reason=(
            "the synthetic database-only file fixture no longer appears through browse; "
            "replace it with the supported upload flow before admission"
        ),
        suites=("test_collection_hierarchy_e2e.sh",),
    ),
    DeferredSuiteGroup(
        reason=(
            "the malformed write-AST case expects method_not_allowed while request "
            "validation currently returns invalid_argument; align the contract first"
        ),
        suites=("test_row_read_security_e2e.sh",),
    ),
    DeferredSuiteGroup(
        reason=("requires an optional external service or network dependency not owned by the hosted runtime"),
        suites=(
            "test_external_git_e2e.sh",
            "test_mcp_oauth_e2e.sh",
            "test_seahorse_db_e2e.sh",
        ),
    ),
    DeferredSuiteGroup(
        reason=(
            "requires the Node stdio proxy and local-filesystem behavior, which the "
            "backend-only runtime does not provide"
        ),
        suites=(
            "test_put_file_param_e2e.sh",
            "test_put_slug_e2e.sh",
            "test_stdio_files_e2e.sh",
        ),
    ),
    DeferredSuiteGroup(
        reason=(
            "requires Kubernetes or backend-container process/storage control outside "
            "the repository-owned runtime contract"
        ),
        suites=(
            "test_hybrid_chaos_e2e.sh",
            "test_hybrid_edge_e2e.sh",
            "test_hybrid_git_e2e.sh",
            "test_hybrid_invariants_e2e.sh",
            "test_hybrid_lifecycle_e2e.sh",
            "test_hybrid_ops_e2e.sh",
            "test_pgvector_driver_e2e.sh",
            "test_self_heal_e2e.sh",
        ),
    ),
    DeferredSuiteGroup(
        reason=(
            "long-running hybrid-search probe with timing assumptions and a noncanonical "
            "summary; harden for the deterministic embedding stub before admission"
        ),
        suites=(
            "test_hybrid_access_e2e.sh",
            "test_hybrid_boundary_e2e.sh",
            "test_hybrid_integration_e2e.sh",
            "test_hybrid_misc_e2e.sh",
            "test_hybrid_search_e2e.sh",
            "test_hybrid_security_e2e.sh",
            "test_hybrid_stress_e2e.sh",
        ),
    ),
    DeferredSuiteGroup(
        reason=(
            "fixed-vector embedding stub cannot make the post-delete similarity "
            "assertion deterministic; use a content-sensitive stub before admission"
        ),
        suites=("test_defensive_e2e.sh",),
    ),
    DeferredSuiteGroup(
        reason=(
            "publication concurrency case still sends the retired mode=live input; "
            "update the fixture to the current publication contract first"
        ),
        suites=("test_concurrency_repro_e2e.sh",),
    ),
    DeferredSuiteGroup(
        reason=(
            "broad legacy or overlapping regression suite retained for on-demand runs outside the bounded hosted gate"
        ),
        suites=(
            "test_e2e.sh",
            "test_edge_extra_e2e.sh",
            "test_edge_more_e2e.sh",
            "test_unified_browse_edges_e2e.sh",
        ),
    ),
    DeferredSuiteGroup(
        reason=("targeted on-demand suite still needs the fail-closed Results: N passed, M failed summary contract"),
        suites=(
            "test_help_skill_template_e2e.sh",
            "test_skill_e2e.sh",
            "test_table_crud_envelope_e2e.sh",
            "test_vault_templates_e2e.sh",
        ),
    ),
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


def validate_suite_manifest(repo_root: Path) -> tuple[str, ...]:
    """Return all manifest errors without starting the live E2E runtime."""

    tests_dir = repo_root / "backend" / "tests"
    discovered = {path.name for path in tests_dir.glob("*_e2e.sh") if path.is_file()}
    curated = list(CURATED_SUITES)
    deferred = [suite for group in DEFERRED_SUITE_GROUPS for suite in group.suites]
    errors: list[str] = []

    duplicate_curated = sorted({name for name in curated if curated.count(name) > 1})
    duplicate_deferred = sorted({name for name in deferred if deferred.count(name) > 1})
    reasonless_groups = [
        index for index, group in enumerate(DEFERRED_SUITE_GROUPS, start=1) if not group.reason.strip()
    ]
    overlap = sorted(set(curated) & set(deferred))
    declared = set(curated) | set(deferred)
    unclassified = sorted(discovered - declared)
    missing = sorted(declared - discovered)

    if duplicate_curated:
        errors.append(f"duplicate curated suites: {', '.join(duplicate_curated)}")
    if duplicate_deferred:
        errors.append(f"duplicate deferred suites: {', '.join(duplicate_deferred)}")
    if reasonless_groups:
        errors.append("deferred suite groups require a reason: " + ", ".join(str(index) for index in reasonless_groups))
    if overlap:
        errors.append(f"suites cannot be both curated and deferred: {', '.join(overlap)}")
    if unclassified:
        errors.append("unclassified E2E suites (execute or defer each one explicitly): " + ", ".join(unclassified))
    if missing:
        errors.append(f"manifest entries without a matching suite file: {', '.join(missing)}")
    return tuple(errors)


def check_suite_manifest(repo_root: Path) -> int:
    errors = validate_suite_manifest(repo_root)
    if errors:
        for error in errors:
            print(f"E2E suite manifest error: {error}", file=sys.stderr)
        return 1

    deferred_count = sum(len(group.suites) for group in DEFERRED_SUITE_GROUPS)
    print(f"E2E suite manifest OK: {len(CURATED_SUITES)} curated, {deferred_count} explicitly deferred")
    return 0


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
    if check_suite_manifest(repo_root) != 0:
        return 1

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
            f"▸ {suite}: {result.passed} passed, {result.failed} failed (rc={result.returncode})",
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
    parser.add_argument(
        "--check-manifest",
        action="store_true",
        help=("validate that every backend/tests/*_e2e.sh suite is curated or explicitly deferred"),
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.check_manifest:
        return check_suite_manifest(repo_root)
    return run_curated(repo_root)


if __name__ == "__main__":
    sys.exit(main())
