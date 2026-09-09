"""Command-line entry point for validation, runs, and paired comparison."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .contracts import load_json
from .evidence import write_json
from .runner import BenchmarkRunFailure, BenchmarkRunner, NeedsUserInput, compare_artifacts, load_inputs
from .runtime import RuntimeContractError, RuntimeDescriptor

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "config" / "run.json"
DEFAULT_CORPUS = PROJECT_ROOT / "corpus" / "tasks.json"


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

    compare = subparsers.add_parser("compare", help="compare two completed arm artifacts")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser


def add_inputs(parser: argparse.ArgumentParser, *, descriptor_required: bool) -> None:
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--descriptor", type=Path, required=descriptor_required)


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "validate":
            code = validate(args)
        elif args.command == "run":
            code = asyncio.run(run(args))
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
    from .contracts import load_run_manifest, load_task_corpus

    manifest = load_run_manifest(args.manifest)
    tasks = load_task_corpus(args.corpus)
    manifest.validate_tasks(tasks)
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
        "repeats": manifest.repeats,
        "model_classes": [model.class_name for model in manifest.models],
        "descriptor": descriptor_info,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


async def run(args: argparse.Namespace) -> int:
    manifest, tasks, descriptor = load_inputs(args.manifest, args.corpus, args.descriptor)
    runner = BenchmarkRunner(manifest, tasks, descriptor, arm=args.arm)
    try:
        artifact = await runner.run()
    except BenchmarkRunFailure as exc:
        write_json(args.output, exc.artifact, exc.secrets)
        raise
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


def compare(args: argparse.Namespace) -> int:
    baseline = load_json(args.baseline)
    candidate = load_json(args.candidate)
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ValueError("run artifacts must be JSON objects")
    result = compare_artifacts(baseline, candidate)
    write_json(args.output, result)
    print(json.dumps({"status": result["gate"]["status"], "output": str(args.output)}, ensure_ascii=False, sort_keys=True))
    return 0 if result["gate"]["status"] == "pass" else 1


if __name__ == "__main__":
    main()
