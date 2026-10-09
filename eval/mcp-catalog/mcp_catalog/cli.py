"""Command-line entry point for validation, runs, and paired comparison."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .amendment import AmendmentPlan, CONTINUATION_RUN_CAP_USD
from .contracts import ArmName, load_json
from .evidence import write_json
from .execution import BudgetLedger
from .recovery import NEW_EXECUTION_LIMIT_USD, RecoverySelection, build_recovery_summary
from .runner import (
    BenchmarkRunFailure,
    BenchmarkRunner,
    NeedsUserInput,
    PairedArmCoordinator,
    attach_paired_execution_evidence,
    compare_artifacts,
    load_inputs,
)
from .runtime import RuntimeContractError, RuntimeDescriptor

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "config" / "run.json"
DEFAULT_CORPUS = PROJECT_ROOT / "corpus" / "tasks.json"
DEFAULT_COVERAGE = PROJECT_ROOT / "corpus" / "tool-coverage.json"
SIGNAL_GRACE_SECONDS = 30.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the source-blind MCP tool catalog benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate contracts without network or provider calls")
    add_inputs(validate, descriptor_required=False)
    validate.add_argument("--check-environment", action="store_true", help="also require configured environment values")

    run = subparsers.add_parser("run", help="run one baseline or candidate arm")
    add_inputs(run, descriptor_required=True)
    run.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--checkpoint",
        type=Path,
        help="atomically persist redacted trial checkpoints at this path",
    )
    run.add_argument(
        "--resume",
        type=Path,
        help="resume exact inputs from this existing checkpoint path",
    )

    paired = subparsers.add_parser(
        "run-paired",
        help="run baseline and candidate in preregistered counterbalanced order",
    )
    paired.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    paired.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    paired.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    paired.add_argument("--baseline-descriptor", type=Path, required=True)
    paired.add_argument("--candidate-descriptor", type=Path, required=True)
    paired.add_argument("--baseline-output", type=Path, required=True)
    paired.add_argument("--candidate-output", type=Path, required=True)
    paired.add_argument("--comparison-output", type=Path, required=True)
    paired.add_argument("--baseline-checkpoint", type=Path, required=True)
    paired.add_argument("--candidate-checkpoint", type=Path, required=True)
    paired.add_argument("--resume", action="store_true", help="resume both independent arm checkpoints")
    execution_plan = paired.add_mutually_exclusive_group()
    execution_plan.add_argument(
        "--amendment",
        type=Path,
        help="continue only the AKB-361 authorized missing outcomes into new child artifacts",
    )
    execution_plan.add_argument(
        "--selection",
        type=Path,
        help="run only the hash-pinned AKB-361 paired provider recovery identities",
    )
    paired.add_argument(
        "--recovery-summary-output",
        type=Path,
        help="write the selected recovery summary separately from the canonical inconclusive comparison",
    )

    compare = subparsers.add_parser("compare", help="compare two completed arm artifacts")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser


def add_inputs(parser: argparse.ArgumentParser, *, descriptor_required: bool) -> None:
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--descriptor", type=Path, required=descriptor_required)


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "validate":
            code = validate(args)
        elif args.command == "run":
            code = asyncio.run(run(args))
        elif args.command == "run-paired":
            code = asyncio.run(run_paired(args))
        else:
            code = compare(args)
    except (ValueError, RuntimeContractError) as exc:
        print(f"configuration_error: {exc}", file=sys.stderr)
        code = 2
    except NeedsUserInput as exc:
        print(f"needs_user_input: {exc}", file=sys.stderr)
        code = 2
    except RuntimeError as exc:
        print(f"run_error: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


def validate(args: argparse.Namespace) -> int:
    from .contracts import load_run_manifest, load_task_corpus, load_tool_coverage

    manifest = load_run_manifest(args.manifest)
    tasks = load_task_corpus(args.corpus)
    coverage = load_tool_coverage(getattr(args, "coverage", DEFAULT_COVERAGE))
    manifest.validate_tasks(tasks)
    coverage.validate_tasks(tasks)
    coverage.validate_manifest(manifest)
    descriptor_info: dict[str, Any] | None = None
    if args.descriptor is not None:
        descriptor = RuntimeDescriptor.from_file(args.descriptor)
        if descriptor.scenario != manifest.fixture_scenario:
            raise ValueError("runtime scenario does not match run manifest")
        descriptor_info = {
            "scenario": descriptor.scenario,
            "supports_stdio": descriptor.supports_stdio,
            "app_origin": descriptor.app_origin,
            "fixture_origin": descriptor.fixture_origin,
        }
    if args.check_environment:
        for model in manifest.models:
            import os

            if not os.environ.get(model.base_url_env) or not os.environ.get(model.provider_key_env):
                raise NeedsUserInput(f"model {model.class_name} environment is incomplete")
    payload = {
        "valid": True,
        "task_count": len(tasks),
        "category_counts": {key: sum(task.category == key for task in tasks) for key in sorted(manifest.category_minimums)},
        "suite_counts": {key: sum(task.suite == key for task in tasks) for key in sorted(manifest.suite_minimums)},
        "capability_counts": {
            key: sum(key in task.capability_families for task in tasks)
            for key in sorted(manifest.capability_minimums)
        },
        "risk_counts": {
            key: sum(key in task.risk_hypotheses for task in tasks)
            for key in sorted(manifest.risk_minimums)
        },
        "coverage_tool_count": len(coverage.entries),
        "locale_counts": dict(sorted(Counter(task.locale for task in tasks).items())),
        "pair_ids": sorted({task.pair_id for task in tasks}),
        "repeats": manifest.repeats,
        "model_classes": [model.class_name for model in manifest.models],
        "descriptor": descriptor_info,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


async def run(args: argparse.Namespace) -> int:
    manifest, tasks, descriptor = load_inputs(
        args.manifest,
        args.corpus,
        args.descriptor,
        getattr(args, "coverage", DEFAULT_COVERAGE),
    )
    runner = BenchmarkRunner(
        manifest,
        tasks,
        descriptor,
        arm=args.arm,
        checkpoint_path=getattr(args, "checkpoint", None),
        resume_path=getattr(args, "resume", None),
    )
    run_task = asyncio.create_task(runner.run(), name="catalog-benchmark-run")
    signal_received = asyncio.Event()

    def handle_signal() -> None:
        signal_received.set()
        if not run_task.done():
            run_task.cancel()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, handle_signal)
    try:
        while not run_task.done():
            if signal_received.is_set():
                try:
                    await asyncio.wait_for(asyncio.shield(run_task), timeout=SIGNAL_GRACE_SECONDS)
                except asyncio.TimeoutError as exc:
                    run_task.cancel()
                    raise RuntimeError("benchmark interrupted; graceful finalization timed out") from exc
                break
            await asyncio.sleep(0.1)
        artifact = await run_task
    except BenchmarkRunFailure as exc:
        write_json(args.output, exc.artifact, exc.secrets)
        raise
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(signum)
    write_json(args.output, artifact, runner.secrets)
    print(
        json.dumps(
            {
                "arm": args.arm,
                "output": str(args.output),
                "source_revision": artifact["source_revision"],
                "catalogs": sorted(artifact["catalogs"]),
                "runs": sorted(artifact["runs"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if artifact["status"] == "complete" else 1


async def run_paired(args: argparse.Namespace) -> int:
    baseline_manifest, baseline_tasks, baseline_descriptor = load_inputs(
        args.manifest,
        args.corpus,
        args.baseline_descriptor,
        args.coverage,
    )
    candidate_manifest, candidate_tasks, candidate_descriptor = load_inputs(
        args.manifest,
        args.corpus,
        args.candidate_descriptor,
        args.coverage,
    )
    if baseline_manifest != candidate_manifest or baseline_tasks != candidate_tasks:
        raise ValueError("paired arms must use identical manifest and corpus inputs")
    selection_path = getattr(args, "selection", None)
    summary_path = getattr(args, "recovery_summary_output", None)
    if selection_path is not None and summary_path is None:
        raise ValueError("--selection requires --recovery-summary-output")
    if selection_path is None and summary_path is not None:
        raise ValueError("--recovery-summary-output requires --selection")
    recovery_selection = (
        RecoverySelection.load(selection_path, manifest=baseline_manifest, tasks=baseline_tasks)
        if selection_path is not None
        else None
    )
    destination_paths = {
        args.baseline_output.resolve(strict=False),
        args.candidate_output.resolve(strict=False),
        args.comparison_output.resolve(strict=False),
        args.baseline_checkpoint.resolve(strict=False),
        args.candidate_checkpoint.resolve(strict=False),
    }
    if summary_path is not None:
        destination_paths.add(summary_path.resolve(strict=False))
    if len(destination_paths) != 5 + int(summary_path is not None):
        raise ValueError("paired artifacts, checkpoints, and recovery summary must use distinct paths")
    amendment_path = getattr(args, "amendment", None)
    continuation = (
        AmendmentPlan.load(amendment_path, manifest=baseline_manifest, tasks=baseline_tasks)
        if amendment_path is not None
        else None
    )
    if continuation is not None and recovery_selection is not None:
        raise ValueError("--selection and --amendment cannot be combined")
    if continuation is not None:
        output_paths = {
            args.baseline_output.resolve(strict=False),
            args.candidate_output.resolve(strict=False),
            args.comparison_output.resolve(strict=False),
            args.baseline_checkpoint.resolve(strict=False),
            args.candidate_checkpoint.resolve(strict=False),
        }
        if continuation.source_paths.intersection(output_paths):
            raise ValueError("AKB-361 continuation destinations must not overwrite parent evidence")
        artifact_outputs = (args.baseline_output, args.candidate_output, args.comparison_output)
        if not args.resume and any(path.exists() for path in artifact_outputs):
            raise ValueError("AKB-361 continuation artifact outputs must be new paths")
        child_checkpoints: dict[ArmName, Path] = {
            "baseline": args.baseline_checkpoint,
            "candidate": args.candidate_checkpoint,
        }
        continuation.seed_child_checkpoints(child_checkpoints, resume=args.resume)

    if recovery_selection is not None:
        protected_paths = {
            recovery_selection.path.resolve(strict=False),
            *(Path(parent["path"]).resolve(strict=False) for parent in recovery_selection.parents.values()),
        }
        if protected_paths.intersection(destination_paths):
            raise ValueError("recovery outputs must not overwrite the selection or parent artifacts")
        recovery_outputs = [args.baseline_output, args.candidate_output, args.comparison_output, summary_path]
        if not args.resume and any(path.exists() for path in recovery_outputs if path is not None):
            raise ValueError("AKB-361 recovery outputs must be new paths unless resuming its selected checkpoints")

    started = time.perf_counter()
    if continuation is None:
        initial_events = None
    elif args.resume:
        initial_events = continuation.history_events_for_resume(
            {"baseline": args.baseline_checkpoint, "candidate": args.candidate_checkpoint}
        )
    else:
        initial_events = continuation.source_events
    coordinator = PairedArmCoordinator(
        baseline_manifest,
        baseline_tasks,
        initial_events=initial_events,
        recovery_selection=recovery_selection,
    )
    ledger = BudgetLedger(
        baseline_manifest,
        wall_clock=lambda: max(0.0, time.perf_counter() - started),
        total_cost_limit_usd=(
            CONTINUATION_RUN_CAP_USD
            if continuation is not None
            else NEW_EXECUTION_LIMIT_USD
            if recovery_selection is not None
            else None
        ),
    )
    baseline_runner = BenchmarkRunner(
        baseline_manifest,
        baseline_tasks,
        baseline_descriptor,
        arm="baseline",
        checkpoint_path=args.baseline_checkpoint,
        resume_path=args.baseline_checkpoint if args.resume or continuation is not None else None,
        paired_coordinator=coordinator,
        shared_ledger=ledger,
        continuation=continuation,
        recovery_selection=recovery_selection,
    )
    candidate_runner = BenchmarkRunner(
        candidate_manifest,
        candidate_tasks,
        candidate_descriptor,
        arm="candidate",
        checkpoint_path=args.candidate_checkpoint,
        resume_path=args.candidate_checkpoint if args.resume or continuation is not None else None,
        paired_coordinator=coordinator,
        shared_ledger=ledger,
        continuation=continuation,
        recovery_selection=recovery_selection,
    )
    run_tasks = {
        "baseline": asyncio.create_task(baseline_runner.run(), name="catalog-benchmark-baseline"),
        "candidate": asyncio.create_task(candidate_runner.run(), name="catalog-benchmark-candidate"),
    }
    done, pending = await asyncio.wait(run_tasks.values(), return_when=asyncio.FIRST_EXCEPTION)
    primary_failure: BaseException | None = None
    for task in done:
        try:
            exception = task.exception()
        except asyncio.CancelledError as exc:
            exception = exc
        if exception is not None:
            primary_failure = exception
            break
    if primary_failure is not None:
        for task in pending:
            task.cancel()
    results = await asyncio.gather(*run_tasks.values(), return_exceptions=True)
    await ledger.release_all_reservations()

    artifacts: dict[str, dict[str, Any]] = {}
    failures: list[BaseException] = []
    for arm, result in zip(run_tasks, results, strict=True):
        if isinstance(result, BenchmarkRunFailure):
            artifacts[arm] = result.artifact
            failures.append(result)
        elif isinstance(result, BaseException):
            checkpoints = {
                "baseline": args.baseline_checkpoint,
                "candidate": args.candidate_checkpoint,
            }
            artifacts[arm] = _paired_arm_failure_artifact(
                arm,
                result,
                baseline_manifest,
                checkpoints[arm],
            )
            if continuation is not None:
                artifacts[arm]["continuation_lineage"] = continuation.lineage
            if recovery_selection is not None:
                artifacts[arm]["recovery_selection"] = recovery_selection.artifact_metadata()
            failures.append(result)
        else:
            artifacts[arm] = result

    evidence = coordinator.evidence()
    paired_budget: dict[str, int | float | str] = {
        "model_requests": ledger.requests,
        "provider_setup_requests": ledger.provider_setup_requests,
        "input_tokens": ledger.input_tokens,
        "output_tokens": ledger.output_tokens,
        "total_tokens": ledger.input_tokens + ledger.output_tokens,
        "cost_usd": float(ledger.cost_usd),
        "wall_seconds": ledger.observe_wall(),
        "model_work_seconds": ledger.model_work_seconds,
        "max_total_cost_usd": baseline_manifest.budget.max_total_cost_usd,
    }
    if recovery_selection is not None:
        paired_budget["new_execution_limit_usd"] = str(NEW_EXECUTION_LIMIT_USD)
    runners = {"baseline": baseline_runner, "candidate": candidate_runner}
    outputs = {"baseline": args.baseline_output, "candidate": args.candidate_output}
    for arm, artifact in artifacts.items():
        if continuation is not None:
            artifact.setdefault("continuation_lineage", continuation.lineage)
        if recovery_selection is not None:
            artifact.setdefault("recovery_selection", recovery_selection.artifact_metadata())
        if isinstance(artifact.get("artifact_hash_input"), dict):
            attach_paired_execution_evidence(artifact, evidence=evidence, budget_used=paired_budget)
        else:
            artifact["paired_execution"] = evidence
            artifact["paired_budget_used"] = paired_budget
        write_json(outputs[arm], artifact, runners[arm].secrets)
    comparison = compare_artifacts(
        artifacts.get("baseline", {}),
        artifacts.get("candidate", {}),
    )
    write_json(args.comparison_output, comparison)
    recovery_summary: dict[str, Any] | None = None
    if recovery_selection is not None and summary_path is not None:
        recovery_summary = build_recovery_summary(
            recovery_selection,
            artifacts,
            comparison,
            manifest=baseline_manifest,
            tasks=baseline_tasks,
        )
        write_json(summary_path, recovery_summary)
    print(
        json.dumps(
            {
                "status": comparison["verdict"],
                "baseline": str(args.baseline_output),
                "candidate": str(args.candidate_output),
                "comparison": str(args.comparison_output),
                "cost_usd": paired_budget["cost_usd"],
                **(
                    {
                        "recovery_status": recovery_summary["status"],
                        "recovery_summary": str(summary_path),
                    }
                    if recovery_summary is not None
                    else {}
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if recovery_summary is not None:
        return 0 if recovery_summary["status"] == "complete" and not failures else 1
    return 0 if comparison["verdict"] == "adopt" and not failures else 1


def _paired_arm_failure_artifact(
    arm: str,
    error: BaseException,
    manifest: Any,
    checkpoint_path: Path,
) -> dict[str, Any]:
    cancelled = isinstance(error, asyncio.CancelledError)
    return {
        "schema_version": 2,
        "status": "incomplete",
        "arm": arm,
        "source_revision": manifest.arm_source_revisions[arm],
        "checkpoint_path": str(checkpoint_path),
        "failure": {
            "stage": "paired_execution",
            "type": type(error).__name__,
            "message": (
                "paired arm was stopped after its peer failed before this arm produced a run artifact"
                if cancelled
                else "paired arm terminated before producing a run artifact"
            ),
        },
        "incomplete_reasons": ["paired arm did not produce a benchmark artifact"],
    }


def compare(args: argparse.Namespace) -> int:
    baseline = load_json(args.baseline)
    candidate = load_json(args.candidate)
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ValueError("run artifacts must be JSON objects")
    result = compare_artifacts(baseline, candidate)
    write_json(args.output, result)
    print(json.dumps({"status": result["verdict"], "output": str(args.output)}, ensure_ascii=False, sort_keys=True))
    return 0 if result["verdict"] == "adopt" else 1


if __name__ == "__main__":
    main()
