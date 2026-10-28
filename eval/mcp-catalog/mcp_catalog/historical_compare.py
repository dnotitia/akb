"""Read-only scoring for sealed artifacts produced by the retired evaluator."""

from __future__ import annotations

import re
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .contracts import (
    ArmOperationRoute,
    Budget,
    CatalogSnapshot,
    ModelSpec,
    PublicOperation,
    StatisticalProcedure,
    TaskManifest,
    _input_schema_exposes_action,
    hash_json,
    public_arguments_match_schema,
)
from .evidence import validate_model_input_evidence
from .execution import (
    ToolCallRecord,
    TrialOutcome,
    _expected_call_target_matches,
    _resolved_material_stage_calls,
    _sequence_target_mismatches,
    decode_raw_args,
    eligible_openrouter_endpoints,
    registered_route_target,
    summarize_outcomes,
    validate_routing_evidence,
)
from .runtime import StateObservation

ArtifactHashBuilder = Callable[..., dict[str, Any]]
_ARMS = ("baseline", "candidate")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


def has_historical_shape(artifact: dict[str, Any]) -> bool:
    manifest = artifact.get("manifest")
    return isinstance(manifest, dict) and {
        "arm_source_revisions",
        "candidate_route_overrides",
    } <= set(manifest)


def compare_historical_artifacts(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    current_hash_builder: ArtifactHashBuilder,
) -> dict[str, Any]:
    """Validate historical seals, then report rescored evidence without adoption gates."""

    artifacts = {"baseline": baseline, "candidate": candidate}
    try:
        for arm, artifact in artifacts.items():
            _validate_integrity(artifact, arm, current_hash_builder)
        manifest, models, operations, budget = _validate_manifest_pair(artifacts)
        tasks = _validate_task_pair(artifacts, manifest)
        route_lineage = {
            arm: _validate_provider_routes(artifact, models)
            for arm, artifact in artifacts.items()
        }
        catalogs = {
            arm: _catalog_route_report(artifact, arm, operations, manifest["arm_source_revisions"][arm])
            for arm, artifact in artifacts.items()
        }
        arm_plans = {
            arm: _validate_arm_plan(artifact, arm, manifest, models, tasks)
            for arm, artifact in artifacts.items()
        }
        selection = _validate_selection_and_lineage(artifacts, manifest, arm_plans, models, budget)
        _validate_pre_smoke_seal(artifacts)
        _validate_budget_receipts(artifacts, budget, arm_plans)
        summaries = {
            arm: _rescore_arm(artifact, arm, models, tasks, arm_plans[arm], route_lineage[arm])
            for arm, artifact in artifacts.items()
        }
    except HistoricalArtifactError as exc:
        return _inconclusive(baseline, candidate, str(exc))
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        return _inconclusive(baseline, candidate, f"historical evidence is invalid: {type(exc).__name__}")

    route_failures = [
        entry
        for arm in _ARMS
        for entry in [*catalogs[arm]["errors"], *catalogs[arm]["logical_function_omissions"]]
    ]
    reasons = ["historical evidence is diagnostic only; adoption gates were not applied"]
    if route_failures:
        reasons.append("sealed catalog routes contain contract errors or logical-operation omissions")
    if any(artifact.get("status") != "complete" for artifact in artifacts.values()):
        reasons.append("one or more source artifacts are marked incomplete")
    return {
        **_inconclusive(baseline, candidate, reasons[0]),
        "gate": {
            "status": "inconclusive",
            "reasons": reasons,
            "checks": {"catalog_routes_valid": not route_failures},
        },
        "observed_historical_diagnostics": {
            "classification": "historical_diagnostic_only",
            "adoption_eligible": False,
            "rescored_from_recorded_evidence": True,
            "selection": selection,
            "validation": {
                "artifact_integrity": "passed",
                "manifest": "passed",
                "task_contracts": "passed",
                "source_revisions": "passed",
                "provider_route_lineage": "passed",
                "checkpoint_receipts": "passed",
                "continuation_lineage": "passed",
                "pre_smoke_seal": "passed",
                "catalog_routes": catalogs,
            },
            "provenance": {
                arm: {
                    "artifact_hash": artifact.get("artifact_hash"),
                    "run_manifest_hash": artifact.get("run_manifest_hash"),
                    "source_revision": artifact.get("source_revision"),
                    "status": artifact.get("status"),
                    "selection_sha256": _selection_sha(artifact),
                    "provider_registry_hash": artifact.get("provider_registry", {}).get("snapshot_hash"),
                }
                for arm, artifact in artifacts.items()
            },
            "arms": summaries,
        },
    }


class HistoricalArtifactError(ValueError):
    """A structural or integrity failure in historical evidence."""


def _inconclusive(baseline: dict[str, Any], candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "schema_version": 4,
        "baseline_source_revision": baseline.get("source_revision"),
        "candidate_source_revision": candidate.get("source_revision"),
        "verdict": "inconclusive",
        "gate": {"status": "inconclusive", "reasons": [reason]},
    }


def _validate_integrity(
    artifact: dict[str, Any],
    arm: str,
    current_hash_builder: ArtifactHashBuilder,
) -> None:
    if artifact.get("schema_version") != 2 or artifact.get("arm") != arm:
        raise HistoricalArtifactError(f"invalid historical {arm} artifact identity")
    manifest = artifact.get("manifest")
    hash_input = artifact.get("artifact_hash_input")
    if not isinstance(manifest, dict) or not isinstance(hash_input, dict):
        raise HistoricalArtifactError(f"historical {arm} artifact is missing its integrity envelope")
    trial_order = hash_input.get("trial_order")
    if not isinstance(trial_order, list):
        raise HistoricalArtifactError(f"historical {arm} trial plan is missing")
    try:
        validate_model_input_evidence(artifact.get("runs", {}))
        validate_model_input_evidence(artifact.get("smoke_gate", {}))
        expected = current_hash_builder(artifact, trial_order=trial_order)
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoricalArtifactError(f"historical {arm} integrity envelope is invalid") from exc
    checkpoint = artifact.get("checkpoint")
    if checkpoint is None:
        receipt = None
    elif isinstance(checkpoint, dict):
        receipt = {
            key: checkpoint.get(key)
            for key in (
                "enabled",
                "lifecycle",
                "record_count",
                "valid_completed_trials",
                "reserved_cost_usd",
                "smoke_status",
                "smoke_cell_count",
            )
        }
        for key in ("attempt_history", "selection_route_revalidations"):
            if key in checkpoint:
                receipt[key] = checkpoint[key]
    else:
        raise HistoricalArtifactError(f"historical {arm} checkpoint receipt is invalid")
    expected["checkpoint_receipt"] = receipt
    for key in ("continuation_lineage", "continuation_runtime_provenance", "selection", "routing_verification"):
        if key in artifact:
            expected[key] = artifact[key]
    if (
        hash_json(manifest) != artifact.get("run_manifest_hash")
        or hash_input != expected
        or hash_json(hash_input) != artifact.get("artifact_hash")
    ):
        raise HistoricalArtifactError(f"historical {arm} artifact integrity digest does not match")


def _validate_manifest_pair(
    artifacts: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, ModelSpec], list[PublicOperation], Budget]:
    baseline_manifest = artifacts["baseline"].get("manifest")
    if not isinstance(baseline_manifest, dict) or any(
        artifact.get("manifest") != baseline_manifest
        or artifact.get("run_manifest_hash") != artifacts["baseline"].get("run_manifest_hash")
        for artifact in artifacts.values()
    ):
        raise HistoricalArtifactError("historical arm manifests do not match")
    manifest = baseline_manifest
    if manifest.get("schema_version") != 2 or manifest.get("source_revision") != "runtime_descriptor":
        raise HistoricalArtifactError("historical manifest schema or source declaration is invalid")
    if set(manifest.get("arms", [])) != set(_ARMS) or set(manifest.get("transports", [])) != {"http", "stdio"}:
        raise HistoricalArtifactError("historical manifest arm or transport declarations are invalid")
    revisions = manifest.get("arm_source_revisions")
    if (
        not isinstance(revisions, dict)
        or set(revisions) != set(_ARMS)
        or any(not isinstance(revisions[arm], str) or _REVISION_RE.fullmatch(revisions[arm]) is None for arm in _ARMS)
        or any(artifacts[arm].get("source_revision") != revisions[arm] for arm in _ARMS)
    ):
        raise HistoricalArtifactError("historical runtime source revisions do not match the sealed manifest")
    try:
        models = [ModelSpec.model_validate(item) for item in manifest["models"]]
        operations = [PublicOperation.model_validate(item) for item in manifest["public_operations"]]
        budget = Budget.model_validate(manifest["budget"])
        StatisticalProcedure.model_validate(manifest["statistical_procedure"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoricalArtifactError("historical manifest contract is invalid") from exc
    model_by_class: dict[str, ModelSpec] = {model.class_name: model for model in models}
    if len(model_by_class) != len(models) or len({model.model_id for model in models}) != len(models):
        raise HistoricalArtifactError("historical manifest model identities are duplicated")
    operation_map = manifest.get("operation_map")
    resources = manifest.get("tool_resources")
    overrides = manifest.get("candidate_route_overrides")
    if not isinstance(operation_map, dict) or not isinstance(resources, dict) or not isinstance(overrides, dict):
        raise HistoricalArtifactError("historical public operation registry is missing")
    logical_by_operation: dict[str, str] = {}
    for logical, tools in operation_map.items():
        if not isinstance(logical, str) or not isinstance(tools, list) or not tools:
            raise HistoricalArtifactError("historical operation map is invalid")
        for tool in tools:
            if not isinstance(tool, str) or tool in logical_by_operation:
                raise HistoricalArtifactError("historical operation map contains duplicate tools")
            logical_by_operation[tool] = logical
    if set(resources) != set(logical_by_operation) or not set(overrides) <= set(logical_by_operation):
        raise HistoricalArtifactError("historical route registry does not cover the operation map")
    route_by_operation = {operation.operation: operation for operation in operations}
    if len(route_by_operation) != len(operations) or set(route_by_operation) != set(logical_by_operation):
        raise HistoricalArtifactError("historical public operation routes do not match the operation map")
    for operation, route in route_by_operation.items():
        identity = ArmOperationRoute(tool=operation)
        try:
            expected_candidate = ArmOperationRoute.model_validate(overrides.get(operation, identity.model_dump(mode="json")))
        except ValueError as exc:
            raise HistoricalArtifactError("historical candidate route override is invalid") from exc
        if (
            route.logical_operation != logical_by_operation[operation]
            or route.resource_type != resources[operation]
            or route.baseline != identity
            or route.candidate != expected_candidate
            or not set(route.transports) <= set(manifest["transports"])
            or not route.transports
            or (operation in overrides and expected_candidate == identity)
        ):
            raise HistoricalArtifactError("historical public operation route differs from its registry")
    return manifest, model_by_class, operations, budget


def _validate_task_pair(artifacts: dict[str, dict[str, Any]], manifest: dict[str, Any]) -> dict[str, TaskManifest]:
    baseline_raw = artifacts["baseline"].get("task_contracts")
    if not isinstance(baseline_raw, list) or any(
        artifact.get("task_contracts") != baseline_raw for artifact in artifacts.values()
    ):
        raise HistoricalArtifactError("historical task contracts are missing or differ between arms")
    try:
        tasks = [TaskManifest.model_validate(item) for item in baseline_raw]
    except (TypeError, ValueError) as exc:
        raise HistoricalArtifactError("historical task contract schema is invalid") from exc
    by_id = {task.id: task for task in tasks}
    if len(by_id) != len(tasks) or not tasks:
        raise HistoricalArtifactError("historical task identities are missing or duplicated")
    expected_hash = hash_json([task.model_dump(mode="json") for task in tasks])
    expected_locales = [{"id": task.id, "locale": task.locale, "pair_id": task.pair_id} for task in tasks]
    expected_ids = [task.id for task in tasks]
    for arm, artifact in artifacts.items():
        if (
            artifact.get("task_corpus_hash") != expected_hash
            or artifact.get("task_ids") != expected_ids
            or artifact.get("task_locales") != expected_locales
            or any(task.fixture.scenario != manifest.get("fixture_scenario") for task in tasks)
        ):
            raise HistoricalArtifactError(f"historical {arm} task corpus does not match its sealed declaration")
        registered_tools = {tool for tools in manifest["operation_map"].values() for tool in tools}
        if any(tool not in registered_tools for task in tasks for tool in task.coverage_tools):
            raise HistoricalArtifactError("historical task coverage references an unregistered operation")
    return by_id


def _validate_provider_routes(artifact: dict[str, Any], models: dict[str, ModelSpec]) -> dict[str, Any]:
    registry = artifact.get("provider_registry")
    if not isinstance(registry, dict) or registry.get("status") != "verified":
        raise HistoricalArtifactError("historical provider registry is not verified")
    declared_hash = registry.get("snapshot_hash")
    if not isinstance(declared_hash, str) or hash_json({key: value for key, value in registry.items() if key != "snapshot_hash"}) != declared_hash:
        raise HistoricalArtifactError("historical provider registry digest does not match")
    registered_models = registry.get("models")
    if not isinstance(registered_models, dict) or set(registered_models) != {model.model_id for model in models.values()}:
        raise HistoricalArtifactError("historical provider registry does not cover the sealed models")
    lineages: dict[str, Any] = {}
    verified_models: dict[str, dict[str, Any]] = {}
    for model in models.values():
        registered = registered_models.get(model.model_id)
        if not isinstance(registered, dict):
            raise HistoricalArtifactError("historical model route evidence is invalid")
        candidate = registered
        if not isinstance(candidate.get("eligible_endpoints"), list):
            snapshot = candidate.get("endpoint_snapshot")
            data = snapshot.get("data") if isinstance(snapshot, dict) else None
            endpoints = data.get("endpoints") if isinstance(data, dict) else None
            model_record = candidate.get("model_record")
            slug = model_record.get("canonical_slug") if isinstance(model_record, dict) else None
            if not isinstance(endpoints, list) or not isinstance(slug, str):
                raise HistoricalArtifactError("historical endpoint snapshot is invalid")
            eligible: list[dict[str, Any]] = []
            for legacy_projection in (False, True):
                eligible, _ = eligible_openrouter_endpoints(
                    model,
                    slug,
                    endpoints,
                    legacy_provider_name_tags=legacy_projection,
                )
                if registered_route_target(model, {**candidate, "eligible_endpoints": eligible}) is not None:
                    break
            candidate = {**candidate, "eligible_endpoints": eligible}
        route = registered_route_target(model, candidate)
        if route is None:
            raise HistoricalArtifactError("historical registered model route does not match its endpoint snapshot")
        lineages[model.model_id] = route.lineage()
        verified_models[model.model_id] = candidate
    recorded = artifact.get("routing_verification")
    if recorded is not None and recorded != {"status": "verified", "models": lineages}:
        raise HistoricalArtifactError("historical routing verification does not match its provider registry")
    return {"lineages": lineages, "registered_models": verified_models}


def _catalog_route_report(
    artifact: dict[str, Any],
    arm: str,
    operations: list[PublicOperation],
    source_revision: str,
) -> dict[str, Any]:
    catalogs = artifact.get("catalogs")
    if not isinstance(catalogs, dict) or not catalogs:
        raise HistoricalArtifactError(f"historical {arm} catalog snapshots are missing")
    transports = {key.split(":", 1)[0] for key in catalogs if isinstance(key, str) and ":" in key}
    if transports != {"http", "stdio"}:
        raise HistoricalArtifactError(f"historical {arm} catalog snapshots omit a transport")
    errors: list[str] = []
    omissions: list[str] = []
    checked = 0
    for key, raw in sorted(catalogs.items()):
        if not isinstance(key, str) or key.count(":") != 1:
            raise HistoricalArtifactError(f"historical {arm} catalog key is invalid")
        transport, profile = key.split(":", 1)
        if transport not in {"http", "stdio"} or not profile:
            raise HistoricalArtifactError(f"historical {arm} catalog key is invalid")
        try:
            snapshot = CatalogSnapshot.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise HistoricalArtifactError(f"historical {arm} catalog schema is invalid") from exc
        if snapshot.transport != transport or snapshot.source_revision != source_revision:
            raise HistoricalArtifactError(f"historical {arm} catalog source or transport differs from its seal")
        checked += 1
        required = [operation for operation in operations if transport in operation.transports]
        expected_names = {getattr(operation, arm).tool for operation in required}
        actual_by_name = {
            tool.get("name"): tool
            for tool in snapshot.tools
            if isinstance(tool.get("name"), str)
        }
        actual_names = set(actual_by_name)
        missing_names = expected_names - actual_names
        extra_names = actual_names - expected_names
        if missing_names:
            omissions.extend(
                f"{key}:{operation.operation}->{getattr(operation, arm).tool}"
                for operation in required
                if getattr(operation, arm).tool in missing_names
            )
        if extra_names:
            errors.append(f"{key}:catalog contains unregistered tools")
        for operation in required:
            route = getattr(operation, arm)
            if route.action is None or route.tool not in actual_by_name:
                continue
            tool = actual_by_name[route.tool]
            schema = tool.get("inputSchema", tool.get("input_schema", {}))
            if not isinstance(schema, dict) or not _input_schema_exposes_action(schema, route.action):
                errors.append(f"{key}:catalog route action is missing for {operation.operation}")
        if snapshot.tool_count != len(expected_names):
            errors.append(f"{key}:catalog tool count differs from the registered physical routes")
    return {
        "status": "passed" if not errors and not omissions else "failed",
        "checked_snapshots": checked,
        "errors": errors,
        "logical_function_omissions": omissions,
    }


def _validate_arm_plan(
    artifact: dict[str, Any],
    arm: str,
    manifest: dict[str, Any],
    models: dict[str, ModelSpec],
    tasks: dict[str, TaskManifest],
) -> dict[str, Any]:
    hash_input = artifact.get("artifact_hash_input")
    plan = hash_input.get("trial_order") if isinstance(hash_input, dict) else None
    runs = artifact.get("runs")
    if not isinstance(plan, list) or not isinstance(runs, dict):
        raise HistoricalArtifactError(f"historical {arm} trial evidence is missing")
    model_ids = {model_class: model.model_id for model_class, model in models.items()}
    planned: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for item in plan:
        if not isinstance(item, dict):
            raise HistoricalArtifactError(f"historical {arm} trial plan contains an invalid row")
        key = _trial_identity(item)
        model_class, transport, task_id, repeat_index = key
        task = tasks.get(task_id)
        if (
            key in planned
            or item.get("arm") != arm
            or model_class not in models
            or item.get("model_id") != model_ids.get(model_class)
            or transport not in manifest["transports"]
            or task is None
            or transport not in task.fixture.transports
            or repeat_index > manifest.get("repeats", 0)
            or item.get("locale") != task.locale
        ):
            raise HistoricalArtifactError(f"historical {arm} trial plan does not match the manifest")
        planned[key] = item
    rows: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for cell, report in runs.items():
        if not isinstance(cell, str) or not isinstance(report, dict) or not isinstance(report.get("trials"), list):
            raise HistoricalArtifactError(f"historical {arm} run cell is invalid")
        for row in report["trials"]:
            if not isinstance(row, dict):
                raise HistoricalArtifactError(f"historical {arm} trial row is invalid")
            key = _trial_identity(row)
            if (
                key in rows
                or key not in planned
                or row.get("arm") != arm
                or row.get("model_id") != model_ids.get(key[0])
                or cell != f"{key[0]}:{key[1]}"
                or row.get("locale") != tasks[key[2]].locale
                or row.get("cluster_id") != tasks[key[2]].pair_id
            ):
                raise HistoricalArtifactError(f"historical {arm} trial rows do not match the sealed plan")
            rows[key] = row
    if set(rows) != set(planned):
        raise HistoricalArtifactError(f"historical {arm} observed rows do not match the sealed plan")
    _validate_checkpoint(artifact, arm, rows)
    _validate_paired_execution(artifacts_execution := artifact.get("paired_execution"), arm, rows, tasks, models, manifest)
    return {"plan": planned, "rows": rows, "execution": artifacts_execution}


def _trial_identity(row: dict[str, Any]) -> tuple[str, str, str, int]:
    model_class, transport, task_id, repeat_index = (
        row.get("model_class"),
        row.get("transport"),
        row.get("task_id"),
        row.get("repeat_index"),
    )
    if (
        not isinstance(model_class, str)
        or not isinstance(transport, str)
        or not isinstance(task_id, str)
        or type(repeat_index) is not int
        or repeat_index < 1
    ):
        raise HistoricalArtifactError("historical trial identity is invalid")
    return model_class, transport, task_id, repeat_index


def _validate_checkpoint(artifact: dict[str, Any], arm: str, rows: dict[Any, dict[str, Any]]) -> None:
    checkpoint = artifact.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise HistoricalArtifactError(f"historical {arm} checkpoint receipt is missing")
    if (
        checkpoint.get("enabled") is not True
        or checkpoint.get("lifecycle") != "finalized"
        or type(checkpoint.get("record_count")) is not int
        or checkpoint["record_count"] != len(rows)
        or type(checkpoint.get("valid_completed_trials")) is not int
        or not 0 <= checkpoint["valid_completed_trials"] <= checkpoint["record_count"]
        or type(checkpoint.get("smoke_cell_count")) is not int
        or checkpoint["smoke_cell_count"] < 0
    ):
        raise HistoricalArtifactError(f"historical {arm} checkpoint receipt does not match observed rows")
    cost = _decimal(checkpoint.get("reserved_cost_usd"), f"historical {arm} checkpoint reservation")
    if cost < 0:
        raise HistoricalArtifactError(f"historical {arm} checkpoint reservation is negative")
    attempts = checkpoint.get("attempt_history", [])
    if not isinstance(attempts, list):
        raise HistoricalArtifactError(f"historical {arm} checkpoint attempt history is invalid")
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get("schema_version") != 4:
            raise HistoricalArtifactError(f"historical {arm} checkpoint attempt record schema is invalid")
        key = attempt.get("key")
        outcome = attempt.get("outcome")
        if (
            not isinstance(key, dict)
            or not isinstance(outcome, dict)
            or key.get("arm") != arm
            or key.get("task_id") != outcome.get("task_id")
            or key.get("model_class") != outcome.get("model_class")
            or key.get("transport") != outcome.get("transport")
            or key.get("repeat_index") != outcome.get("repeat_index")
            or not isinstance(attempt.get("status"), str)
            or not isinstance(attempt.get("record_hash"), str)
            or hash_json({field: attempt[field] for field in ("status", "key", "outcome")}) != attempt["record_hash"]
        ):
            raise HistoricalArtifactError(f"historical {arm} checkpoint attempt digest does not match")


def _validate_paired_execution(
    execution: Any,
    arm: str,
    rows: dict[tuple[str, str, str, int], dict[str, Any]],
    tasks: dict[str, TaskManifest],
    models: dict[str, ModelSpec],
    manifest: dict[str, Any],
) -> None:
    if not isinstance(execution, dict) or execution.get("mode") != manifest.get("paired_order") or execution.get("seed") != manifest.get("paired_order_seed"):
        raise HistoricalArtifactError(f"historical {arm} paired execution lineage is invalid")
    events = execution.get("events")
    if not isinstance(events, list):
        raise HistoricalArtifactError(f"historical {arm} paired execution events are missing")
    sequences: set[int] = set()
    event_identities: set[tuple[str, str, str, int, str]] = set()
    last_sequence = 0
    for event in events:
        if not isinstance(event, dict):
            raise HistoricalArtifactError(f"historical {arm} paired execution event is invalid")
        sequence = event.get("sequence")
        cell = event.get("cell")
        event_arm = event.get("arm")
        task_id = event.get("task_id")
        repeat_index = event.get("repeat_index")
        if (
            type(sequence) is not int
            or sequence <= last_sequence
            or sequence in sequences
            or not isinstance(cell, str)
            or cell.count(":") != 1
            or not isinstance(event_arm, str)
            or event_arm not in _ARMS
            or not isinstance(task_id, str)
            or type(repeat_index) is not int
            or repeat_index < 1
            or type(event.get("order_position")) is not int
            or event["order_position"] not in (0, 1)
        ):
            raise HistoricalArtifactError(f"historical {arm} paired execution sequence is invalid")
        model_class, transport = cell.split(":", 1)
        identity = (model_class, transport, task_id, repeat_index)
        task = tasks.get(task_id)
        if model_class not in models or transport not in manifest["transports"] or task is None or transport not in task.fixture.transports:
            raise HistoricalArtifactError(f"historical {arm} paired execution event is outside the plan")
        sequences.add(sequence)
        event_identities.add((*identity, event_arm))
        last_sequence = sequence
    if any((*identity, arm) not in event_identities for identity in rows):
        raise HistoricalArtifactError(f"historical {arm} paired execution omits an observed trial")


def _validate_selection_and_lineage(
    artifacts: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
    plans: dict[str, dict[str, Any]],
    models: dict[str, ModelSpec],
    benchmark_budget: Budget,
) -> dict[str, Any] | None:
    baseline = artifacts["baseline"]
    candidate = artifacts["candidate"]
    if baseline.get("paired_execution") != candidate.get("paired_execution"):
        raise HistoricalArtifactError("historical arms do not share paired execution lineage")
    resume_source = baseline.get("resume_source")
    if not isinstance(resume_source, dict) or candidate.get("resume_source") != resume_source:
        raise HistoricalArtifactError("historical arms do not share continuation lineage")
    parents = resume_source.get("parents")
    if not isinstance(parents, dict) or set(parents) != set(_ARMS):
        raise HistoricalArtifactError("historical continuation parents are invalid")
    for arm, parent in parents.items():
        if (
            not isinstance(parent, dict)
            or any(not _is_hash(parent.get(field)) for field in ("artifact_hash", "artifact_sha256", "checkpoint_sha256", "checkpoint_spent_hash"))
            or parent.get("source_revision") != manifest["arm_source_revisions"][arm]
        ):
            raise HistoricalArtifactError("historical continuation parent hashes or source revisions are invalid")
    if type(resume_source.get("schema_version")) is not int or not isinstance(resume_source.get("source_execution"), dict) or not isinstance(resume_source.get("source_accounting"), dict):
        raise HistoricalArtifactError("historical continuation accounting is incomplete")
    selection = baseline.get("selection")
    if selection is None:
        if candidate.get("selection") is not None or resume_source.get("selection_sha256") is not None:
            raise HistoricalArtifactError("historical selection lineage is inconsistent")
        return None
    if candidate.get("selection") != selection:
        raise HistoricalArtifactError("historical arm selection plans do not match")
    if not isinstance(selection, dict) or not _is_hash(selection.get("selection_sha256")):
        raise HistoricalArtifactError("historical selection digest is invalid")
    if resume_source.get("selection_sha256") != selection.get("selection_sha256"):
        raise HistoricalArtifactError("historical selection and continuation digests differ")
    authorization = selection.get("authorization")
    parents_plan = selection.get("parents")
    raw_trials = selection.get("selected_trials")
    if (
        not isinstance(authorization, dict)
        or not isinstance(authorization.get("uri"), str)
        or not authorization["uri"]
        or not isinstance(authorization.get("revision"), str)
        or _REVISION_RE.fullmatch(authorization["revision"]) is None
        or not _is_hash(authorization.get("content_sha256"))
        or not isinstance(parents_plan, dict)
        or set(parents_plan) != {"baseline_artifact", "baseline_checkpoint", "candidate_artifact", "candidate_checkpoint"}
        or not isinstance(raw_trials, list)
        or not raw_trials
    ):
        raise HistoricalArtifactError("historical selection authorization, parents, or trials are invalid")
    for name, parent in parents_plan.items():
        if not isinstance(parent, dict) or not isinstance(parent.get("path"), str) or not parent["path"] or not _is_hash(parent.get("sha256")):
            raise HistoricalArtifactError("historical selection parent file pin is invalid")
    identities: dict[str, set[tuple[str, str, str, int]]] = {arm: set() for arm in _ARMS}
    for item in raw_trials:
        if not isinstance(item, dict) or item.get("arm") not in _ARMS:
            raise HistoricalArtifactError("historical selection identity is invalid")
        identity = _trial_identity(item)
        arm = item["arm"]
        if (
            identity in identities[arm]
            or identity not in plans[arm]["plan"]
            or not isinstance(item.get("role"), str)
            or not isinstance(item.get("parent_lineage"), str)
            or not isinstance(item.get("parent_record_key"), (dict, str))
            or (isinstance(item.get("parent_record_key"), str) and not item["parent_record_key"])
            or not _is_hash(item.get("parent_record_hash"))
            or not isinstance(item.get("failure_kind"), str)
        ):
            raise HistoricalArtifactError("historical selection differs from its observed trial plan")
        identities[arm].add(identity)
    counts = {arm: len(identities[arm]) for arm in _ARMS}
    if selection.get("expected_trials_by_arm") != counts or selection.get("expected_model_trials") != sum(counts.values()):
        raise HistoricalArtifactError("historical selection counts do not match its identities")
    if any(identities[arm] != set(plans[arm]["plan"]) for arm in _ARMS):
        raise HistoricalArtifactError("historical selected rows do not match the sealed per-arm plans")
    _validate_selection_budget(selection.get("budget"), benchmark_budget)
    _validate_selection_routing(selection.get("routing_policy"), models)
    for arm, artifact in artifacts.items():
        seal = artifact.get("pre_smoke_seal_inputs")
        if not isinstance(seal, dict) or seal.get("selection") != selection:
            raise HistoricalArtifactError(f"historical {arm} pre-smoke seal omits the selection")
    return {"selection_sha256": selection["selection_sha256"], "selected_trials_by_arm": counts}


def _validate_selection_budget(raw: Any, benchmark_budget: Budget) -> None:
    fields = (
        "session_max_usd",
        "prior_conservative_spend_usd",
        "new_execution_limit_usd",
        "per_trial_usd",
    )
    if not isinstance(raw, dict) or any(field not in raw for field in fields):
        raise HistoricalArtifactError("historical selection budget is incomplete")
    values = {field: _decimal(raw[field], "historical selection budget") for field in fields}
    timeout = raw.get("request_timeout_seconds")
    if (
        values["session_max_usd"] != Decimal(str(benchmark_budget.max_total_cost_usd))
        or values["per_trial_usd"] != Decimal(str(benchmark_budget.max_cost_per_trial_usd))
        or values["prior_conservative_spend_usd"] < 0
        or values["new_execution_limit_usd"] < 0
        or values["prior_conservative_spend_usd"] + values["new_execution_limit_usd"] > values["session_max_usd"]
        or type(timeout) is not int
        or timeout != benchmark_budget.request_timeout_seconds
    ):
        raise HistoricalArtifactError("historical selection budget differs from the sealed benchmark budget")


def _validate_selection_routing(raw: Any, models: dict[str, ModelSpec]) -> None:
    if not isinstance(raw, dict):
        raise HistoricalArtifactError("historical selection routing policy is missing")
    preferences = raw.get("provider_preference_by_model")
    if (
        not isinstance(preferences, dict)
        or set(preferences) != set(models)
        or raw.get("allow_fallbacks") is not True
        or raw.get("require_parameters") is not True
        or not isinstance(raw.get("quantizations"), list)
        or "fp8" not in raw["quantizations"]
    ):
        raise HistoricalArtifactError("historical selection routing policy is invalid")
    if any(preferences[name] != model.routing.order for name, model in models.items()):
        raise HistoricalArtifactError("historical selection routing preferences differ from the manifest")


def _validate_pre_smoke_seal(artifacts: dict[str, dict[str, Any]]) -> None:
    inputs: dict[str, dict[str, Any]] = {}
    for arm, artifact in artifacts.items():
        seal = artifact.get("pre_smoke_seal_inputs")
        if (
            not isinstance(seal, dict)
            or seal.get("schema_version") != 1
            or seal.get("arm") != arm
            or seal.get("source_revision") != artifact.get("source_revision")
            or seal.get("run_manifest_hash") != artifact.get("run_manifest_hash")
            or seal.get("task_corpus_hash") != artifact.get("task_corpus_hash")
            or seal.get("provider_registry_hash") != artifact.get("provider_registry", {}).get("snapshot_hash")
            or seal.get("provider_registry_status") != artifact.get("provider_registry", {}).get("status")
            or seal.get("catalogs") != artifact.get("catalogs")
            or seal.get("environment") != artifact.get("execution_environment")
            or seal.get("paired_order_plan") != artifact.get("paired_order_plan")
            or seal.get("paired_order_mode") != artifact.get("paired_execution", {}).get("mode")
            or seal.get("paired_order_seed") != artifact.get("paired_execution", {}).get("seed")
            or not _is_hash(seal.get("oracle_hash"))
        ):
            raise HistoricalArtifactError(f"historical {arm} pre-smoke seal inputs differ from the artifact")
        fixture = seal.get("fixture")
        root_fixture = artifact.get("fixture")
        if not isinstance(root_fixture, dict):
            raise HistoricalArtifactError(f"historical {arm} fixture seal differs")
        reset = root_fixture.get("reset")
        if (
            not isinstance(fixture, dict)
            or not isinstance(reset, dict)
            or fixture.get("scenario") != root_fixture.get("scenario")
            or fixture.get("reset_body") != reset.get("body")
            or seal.get("selection") != artifact.get("selection")
        ):
            raise HistoricalArtifactError(f"historical {arm} pre-smoke fixture or selection seal differs")
        inputs[arm] = seal
    expected = hash_json({arm: hash_json(inputs[arm]) for arm in _ARMS})
    if any(artifact.get("pre_smoke_seal_hash") != expected for artifact in artifacts.values()):
        raise HistoricalArtifactError("historical paired pre-smoke seal digest does not match")


def _validate_budget_receipts(
    artifacts: dict[str, dict[str, Any]],
    budget: Budget,
    arm_plans: dict[str, dict[str, Any]],
) -> None:
    paired = artifacts["baseline"].get("paired_budget_used")
    if not isinstance(paired, dict) or artifacts["candidate"].get("paired_budget_used") != paired:
        raise HistoricalArtifactError("historical paired budget receipts do not match")
    cost = _decimal(paired.get("cost_usd"), "historical paired cost")
    if cost > Decimal(str(budget.max_total_cost_usd)):
        raise HistoricalArtifactError("historical paired cost exceeds the sealed budget")
    for field in ("model_requests", "provider_setup_requests", "input_tokens", "output_tokens", "total_tokens"):
        value = paired.get(field)
        if type(value) is not int or value < 0:
            raise HistoricalArtifactError("historical paired usage receipt is invalid")
    if paired["total_tokens"] != paired["input_tokens"] + paired["output_tokens"]:
        raise HistoricalArtifactError("historical paired token totals do not reconcile")
    if "request_timeout_seconds" in paired and paired.get("request_timeout_seconds") != budget.request_timeout_seconds:
        raise HistoricalArtifactError("historical request timeout differs from the sealed budget")
    for arm, plan in arm_plans.items():
        for row in plan["rows"].values():
            row_cost = _decimal(row.get("cost_usd", 0), f"historical {arm} trial cost")
            if row_cost > Decimal(str(budget.max_cost_per_trial_usd)):
                raise HistoricalArtifactError(f"historical {arm} trial exceeds the sealed per-trial budget")


def _rescore_arm(
    artifact: dict[str, Any],
    arm: str,
    models: dict[str, ModelSpec],
    tasks: dict[str, TaskManifest],
    plan: dict[str, Any],
    route_lineage: dict[str, Any],
) -> dict[str, Any]:
    parsed: list[TrialOutcome] = []
    rescored: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    cost = Decimal("0")
    for key, row in sorted(plan["rows"].items()):
        model_class, transport, task_id, repeat_index = key
        task = tasks[task_id]
        raw_cost = _decimal(row.get("cost_usd", 0), f"historical {arm} trial cost")
        raw_ints: dict[str, int] = {}
        for field in ("model_requests", "input_tokens", "output_tokens", "total_tokens"):
            value = row.get(field, 0)
            if type(value) is not int or value < 0:
                raise HistoricalArtifactError(f"historical {arm} trial usage field is invalid")
            raw_ints[field] = value
            totals[field] += value
        tool_calls = row.get("tool_calls")
        if not isinstance(tool_calls, list):
            raise HistoricalArtifactError(f"historical {arm} raw tool trace is missing")
        totals["tool_attempts"] += len(tool_calls)
        cost += raw_cost
        recorded = {
            "recorded_success": row.get("success"),
            "recorded_trial_error": row.get("trial_error"),
            "recorded_trial_error_kinds": row.get("trial_error_kinds"),
            "recorded_execution_error": row.get("error"),
            "recorded_failure_kind": row.get("failure_kind"),
        }
        try:
            outcome = TrialOutcome.model_validate(row)
            schemas = _catalog_input_schemas(artifact, transport, task.fixture.credential_profile)
            for call in outcome.tool_calls:
                decoded, _ = decode_raw_args(call.raw_model_args)
                schema = schemas.get(call.tool_name)
                call.public_schema_valid = (
                    public_arguments_match_schema(decoded, schema)
                    if schema is not None
                    else None
                )
            outcome.finalize(task, _observations(outcome.state_observations_before), _observations(outcome.state_observations_after))
        except (TypeError, ValueError, KeyError) as exc:
            raise HistoricalArtifactError(f"historical {arm} trial cannot be rescored: {type(exc).__name__}") from exc
        invalid_reason = _invalid_condition_reason(task, outcome)
        condition_error_kinds = _condition_error_kinds(task, outcome, invalid_reason)
        model_error_kinds = [kind for kind in outcome.trial_error_kinds if kind not in set(condition_error_kinds)]
        model_error = bool(model_error_kinds)
        model = models[model_class]
        registered = route_lineage["registered_models"][model.model_id]
        observed, route_valid = validate_routing_evidence(outcome.provider_evidence, model, registered)
        if outcome.model_requests and len(outcome.provider_evidence) != outcome.model_requests:
            totals["request_evidence_mismatches"] += 1
        if not route_valid:
            totals["route_evidence_failures"] += 1
        totals["recorded_trial_error_trials"] += bool(recorded["recorded_trial_error"])
        totals["recorded_execution_error_trials"] += recorded["recorded_execution_error"] is not None
        totals["final_successes"] += outcome.success and invalid_reason is None
        totals["observed_trial_error_trials"] += outcome.trial_error
        totals["rescored_trial_error_trials"] += model_error
        totals["invalid_condition_trials"] += invalid_reason is not None
        totals["invalid_condition_error_trials"] += bool(condition_error_kinds)
        totals["evaluated_trials"] += invalid_reason is None
        totals["task_error_trials"] += model_error and invalid_reason is None
        totals["unsafe_mutation_trials"] += outcome.unsafe_mutation
        if invalid_reason is None:
            parsed.append(outcome)
        rescored.append(
            {
                "model_class": model_class,
                "transport": transport,
                "task_id": task_id,
                "repeat_index": repeat_index,
                "record_sha256": hash_json(row),
                **recorded,
                "final_success": outcome.success,
                "observed_trial_error": outcome.trial_error,
                "observed_trial_error_kinds": outcome.trial_error_kinds,
                "final_trial_error": model_error,
                "final_trial_error_kinds": model_error_kinds,
                "condition_error_kinds": condition_error_kinds,
                "evaluated": invalid_reason is None,
                "invalid_condition_reason": invalid_reason,
                "final_failure_kind": outcome.failure_kind,
                "routing_evidence_observed": observed,
                "routing_evidence_valid": route_valid,
                "model_requests": raw_ints["model_requests"],
                "input_tokens": raw_ints["input_tokens"],
                "output_tokens": raw_ints["output_tokens"],
                "total_tokens": raw_ints["total_tokens"],
                "cost_usd": str(raw_cost),
            }
        )
    return {
        "artifact_status": artifact.get("status"),
        "planned_trials": len(plan["plan"]),
        "observed_trials": len(plan["rows"]),
        "evaluated_trials": totals["evaluated_trials"],
        "excluded_invalid_condition_trials": totals["invalid_condition_trials"],
        "rescored_trials": rescored,
        "final_successes": totals["final_successes"],
        "task_error_trials": totals["task_error_trials"],
        "observed_trial_error_trials": totals["observed_trial_error_trials"],
        "recorded_trial_error_trials": totals["recorded_trial_error_trials"],
        "recorded_execution_error_trials": totals["recorded_execution_error_trials"],
        "rescored_trial_error_trials": totals["rescored_trial_error_trials"],
        "invalid_condition_error_trials": totals["invalid_condition_error_trials"],
        "unsafe_mutation_trials": totals["unsafe_mutation_trials"],
        "tool_attempts": totals["tool_attempts"],
        "model_requests": totals["model_requests"],
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "total_tokens": totals["total_tokens"],
        "cost_usd": str(cost),
        "request_evidence_mismatches": totals["request_evidence_mismatches"],
        "route_evidence_failures": totals["route_evidence_failures"],
        "efficiency_metrics": summarize_outcomes(parsed),
    }


def _catalog_input_schemas(artifact: dict[str, Any], transport: str, profile: str) -> dict[str, dict[str, Any]]:
    catalogs = artifact.get("catalogs")
    raw = catalogs.get(f"{transport}:{profile}") if isinstance(catalogs, dict) else None
    if not isinstance(raw, dict):
        raise HistoricalArtifactError("historical captured catalog is missing for a trial")
    snapshot = CatalogSnapshot.model_validate(raw)
    schemas: dict[str, dict[str, Any]] = {}
    for tool in snapshot.tools:
        name = tool.get("name")
        schema = tool.get("inputSchema")
        if isinstance(name, str) and isinstance(schema, dict):
            schemas[name] = schema
    return schemas


def _invalid_condition_reason(task: TaskManifest, outcome: TrialOutcome) -> str | None:
    revision_bodies: list[str] = []
    for attempt in task.expected_material_attempts:
        body = attempt.arguments.get("content")
        if attempt.logical_operation in {"create", "update"} and isinstance(body, str):
            revision_bodies.append(body)
    if len(revision_bodies) >= 2 and not all(body in task.prompt for body in revision_bodies):
        return "task prompt omitted the exact preregistered revision bodies"
    owner_transfer = _fixture_owner_transfer(task, outcome)
    if owner_transfer is not None:
        attempt, _ = owner_transfer
        return f"fixture user {attempt.arguments['new_owner']!r} was absent during the ownership transfer"
    return None


def _fixture_owner_transfer(
    task: TaskManifest,
    outcome: TrialOutcome,
) -> tuple[Any, ToolCallRecord] | None:
    attempts = [
        attempt
        for attempt in task.expected_material_attempts
        if attempt.logical_operation == "update"
        and attempt.resource_type == "access"
        and attempt.outcome == "success"
        and isinstance(attempt.arguments.get("new_owner"), str)
    ]
    if len(attempts) != 1:
        return None
    attempt = attempts[0]
    owner = attempt.arguments["new_owner"]
    transfers = []
    for call in outcome.tool_calls:
        arguments = call.effective_server_args or call.server_args
        if arguments is not None:
            arguments = {key: value for key, value in arguments.items() if key != "_vault_skill_ack"}
        if (
            call.tool_exists
            and call.stage_id == attempt.stage_id
            and call.logical_operation == attempt.logical_operation
            and call.resource_type == attempt.resource_type
            and (attempt.tool_name is None or call.tool_name == attempt.tool_name)
            and arguments == attempt.arguments
            and not call.server_succeeded
            and call.server_error_code == "not_found"
        ):
            transfers.append(call)
    if len(transfers) != 1:
        return None
    for search in outcome.tool_calls:
        search_arguments = search.effective_server_args or search.server_args
        if (
            not search.tool_exists
            or search.logical_operation != "list"
            or search.resource_type != "access"
            or search.operation_kind != "preparatory"
            or not search.server_succeeded
            or search.server_error_code is not None
            or search_arguments != {"action": "search_users", "query": owner}
            or not isinstance(search.result_preview, str)
        ):
            continue
        try:
            result = json.loads(search.result_preview)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict) and result.get("users") == []:
            return attempt, transfers[0]
    return None


def _condition_error_kinds(
    task: TaskManifest,
    outcome: TrialOutcome,
    invalid_reason: str | None,
) -> list[str]:
    if invalid_reason is None:
        return []
    owner_transfer = _fixture_owner_transfer(task, outcome)
    if owner_transfer is None:
        return []
    attempt, transfer = owner_transfer
    owner = attempt.arguments["new_owner"]
    if invalid_reason != f"fixture user {owner!r} was absent during the ownership transfer":
        return []
    attributed: set[str] = set()
    schema_error_codes = {"invalid_argument", "validation_error", "invalid_params", "schema_error"}
    has_other_unexpected_error = any(
        call is not transfer
        and not call.server_succeeded
        and call.server_error_code is not None
        and call.server_error_code != "vault_skill_required"
        and call.server_error_code not in schema_error_codes
        and call.server_status_code not in {400, 422}
        and not (
            call.operation_kind == "preparatory"
            and call.server_error_code == "not_found"
            and call.logical_operation in {"read", "list", "search"}
        )
        for call in outcome.tool_calls
    )
    if "unexpected_tool_error" in outcome.trial_error_kinds and not has_other_unexpected_error:
        attributed.add("unexpected_tool_error")
    stage_calls, _ = _resolved_material_stage_calls(task, outcome.tool_calls)
    mismatches = _sequence_target_mismatches(stage_calls, task, outcome.tool_calls)
    mismatches.update(
        index
        for index, call in enumerate(stage_calls, start=1)
        if not _expected_call_target_matches(call, task, outcome.tool_calls)
    )
    mismatch_sources = [call for index, call in enumerate(stage_calls, start=1) if index in mismatches]
    if (
        "wrong_target_payload" in outcome.trial_error_kinds
        and outcome.wrong_target_calls == len(mismatches) == 1
        and len(mismatch_sources) == 1
        and mismatch_sources[0] is transfer
    ):
        attributed.add("wrong_target_payload")
    return sorted(attributed)


def _observations(raw: list[dict[str, Any]]) -> list[StateObservation]:
    if not isinstance(raw, list):
        raise ValueError("state observations are invalid")
    observations: list[StateObservation] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"available", "status_code", "payload", "error"}:
            raise ValueError("state observation record is invalid")
        observations.append(
            StateObservation(
                available=item["available"],
                status_code=item["status_code"],
                payload=item["payload"],
                error=item["error"],
            )
        )
    return observations


def _decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HistoricalArtifactError(f"{label} is invalid") from exc
    if not parsed.is_finite():
        raise HistoricalArtifactError(f"{label} is not finite")
    return parsed


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _selection_sha(artifact: dict[str, Any]) -> str | None:
    selection = artifact.get("selection")
    digest = selection.get("selection_sha256") if isinstance(selection, dict) else None
    return digest if isinstance(digest, str) else None
