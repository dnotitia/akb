from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp_catalog.contracts import (
    CatalogSnapshot,
    hash_json,
    load_run_manifest,
    load_task_corpus,
    token_estimate,
)
from mcp_catalog.execution import ToolCallRecord, TrialOutcome
from mcp_catalog.runner import (
    _build_artifact_hash_input,
    _expected_openrouter_canonical_slug,
    planned_arm_order,
)
from mcp_catalog.runtime import StateObservation

ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = ROOT / "fixtures"
OPENROUTER_PROVIDER_NAMES = {"deepinfra": "DeepInfra", "akashml": "AkashML"}
ARTIFACT_VERSIONS = {"backend_artifact_version": "1.0.0", "proxy_artifact_version": "1.0.0"}
EXECUTION_ENVIRONMENT = {
    "python": "3.14.0 synthetic fixture",
    "platform": "synthetic platform",
    "packages": {
        "pydantic-ai": "1.0.0",
        "pydantic-evals": "1.0.0",
        "fastmcp": "1.0.0",
        "mcp": "1.0.0",
        "scipy": "1.0.0",
    },
    "uv_lock_sha256": "a" * 64,
}


def provider_name_for_model(model: Any) -> str:
    return OPENROUTER_PROVIDER_NAMES[model.routing.order[0]]


def provider_registry_snapshot(manifest: Any | None = None) -> dict[str, Any]:
    manifest_model = manifest or load_run_manifest(ROOT / "config" / "run.json")
    model_rows = [
        {"id": model.model_id, "canonical_slug": _expected_openrouter_canonical_slug(model)}
        for model in manifest_model.models
    ]
    model_list_snapshot = {"data": model_rows}
    models: dict[str, Any] = {}
    endpoint_snapshots: dict[str, Any] = {}
    for model, model_row in zip(manifest_model.models, model_rows, strict=True):
        provider_name = provider_name_for_model(model)
        provider_slug = model.routing.order[0]
        selected_endpoint = {
            "model_id": model.model_id,
            "name": f"{provider_name} | {model_row['canonical_slug']}",
            "provider_name": provider_name,
            "tag": f"{provider_slug}/fp8",
            "quantization": "fp8",
            "supported_parameters": ["tools", "tool_choice", "temperature", "max_tokens"],
        }
        endpoint_snapshot = {"data": {"endpoints": [selected_endpoint]}}
        models[model.model_id] = {
            "manifest_version": model.version,
            "model_record": model_row,
            "endpoint_snapshot": endpoint_snapshot,
            "selected_endpoint": selected_endpoint,
        }
        endpoint_snapshots[model.model_id] = endpoint_snapshot
    payload = {
        "status": "verified",
        "model_list_snapshot": model_list_snapshot,
        "model_list_hash": hash_json(model_list_snapshot),
        "models": models,
        "endpoint_snapshots": endpoint_snapshots,
    }
    return {**payload, "snapshot_hash": hash_json(payload)}


def complete_paired_artifacts(
    *,
    baseline_failure_pairs: set[str] | None = None,
    candidate_failure_pairs: set[str] | None = None,
    mirror_baseline_failures: bool = False,
    request_count: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create sealed synthetic evidence with recomputable state and call traces."""

    manifest_model = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    manifest = manifest_model.model_dump(mode="json")
    pair_order_plan = _paired_order_plan(tasks, manifest_model)
    runtime_descriptor_template = _runtime_descriptor(manifest_model)

    events: list[dict[str, Any]] = []
    event_sequence: dict[tuple[str, str, int, str], int] = {}
    sequence = 0
    for model in manifest_model.models:
        for transport in manifest_model.transports:
            cell = f"{model.class_name}:{transport}"
            for task in tasks:
                if transport not in task.fixture.transports:
                    continue
                for repeat_index in range(1, manifest_model.repeats + 1):
                    order = planned_arm_order(task.id, repeat_index, manifest_model.paired_order_seed)
                    for order_position, arm in enumerate(order):
                        sequence += 1
                        identity = (cell, task.id, repeat_index, arm)
                        event_sequence[identity] = sequence
                        events.append(
                            {
                                "sequence": sequence,
                                "cell": cell,
                                "task_id": task.id,
                                "repeat_index": repeat_index,
                                "arm": arm,
                                "order_position": order_position,
                            }
                        )

    shared_execution = {
        "mode": manifest["paired_order"],
        "seed": manifest["paired_order_seed"],
        "complete": True,
        "events": events,
        "reused": [],
    }
    provider_registry = provider_registry_snapshot(manifest_model)
    artifacts: dict[str, dict[str, Any]] = {}
    all_usage: list[TrialOutcome] = []

    for arm in ("baseline", "candidate"):
        revision = manifest_model.arm_source_revisions[arm]
        catalogs = {
            f"{transport}:{profile}": _catalog_snapshot(manifest_model, arm, transport)
            for transport in manifest_model.transports
            for profile in ("default", "authorization")
        }
        catalog_payloads = {key: snapshot.model_dump(mode="json") for key, snapshot in catalogs.items()}
        run_trials: dict[str, list[dict[str, Any]]] = defaultdict(list)
        trial_order: list[dict[str, Any]] = []
        for model in manifest_model.models:
            for transport in manifest_model.transports:
                cell = f"{model.class_name}:{transport}"
                for task in tasks:
                    if transport not in task.fixture.transports:
                        continue
                    for repeat_index in range(1, manifest_model.repeats + 1):
                        baseline_failure = (
                            baseline_failure_pairs is None
                            or task.pair_id in baseline_failure_pairs
                        )
                        if arm == "candidate" and mirror_baseline_failures:
                            forced_failure = baseline_failure
                        elif arm == "candidate":
                            forced_failure = task.pair_id in (candidate_failure_pairs or set())
                        else:
                            forced_failure = baseline_failure
                        identity = (cell, task.id, repeat_index, arm)
                        order = planned_arm_order(task.id, repeat_index, manifest_model.paired_order_seed)
                        outcome = _make_outcome(
                            task,
                            manifest_model,
                            arm=arm,
                            model=model,
                            transport=transport,
                            repeat_index=repeat_index,
                            paired_order_position=order.index(arm),
                            paired_execution_sequence=event_sequence[identity],
                            forced_failure=forced_failure,
                            request_count=request_count,
                        )
                        all_usage.append(outcome)
                        run_trials[cell].append(outcome.model_dump(mode="json"))
                        trial_order.append(
                            {
                                "model_class": model.class_name,
                                "model_id": model.model_id,
                                "transport": transport,
                                "task_id": task.id,
                                "repeat_index": repeat_index,
                                "locale": task.locale,
                                "arm": arm,
                            }
                        )

        smoke_cells: list[dict[str, Any]] = []
        for model in manifest_model.models:
            for transport in manifest_model.transports:
                task = next(
                    item for item in tasks
                    if transport in item.fixture.transports and item.expected_material_outcomes
                )
                smoke = _make_outcome(
                    task,
                    manifest_model,
                    arm=arm,
                    model=model,
                    transport=transport,
                    repeat_index=1,
                    paired_order_position=None,
                    paired_execution_sequence=None,
                    request_count=2,
                )
                smoke.follow_up_terminal_response = True
                all_usage.append(smoke)
                smoke_cells.append(
                    {
                        "cell": f"{model.class_name}:{transport}",
                        "status": "completed",
                        "reused": False,
                        "outcome": smoke.model_dump(mode="json"),
                    }
                )

        source_runtime = deepcopy(runtime_descriptor_template)
        source_runtime["evidence"]["source_revision"] = revision
        seal_inputs = {
            "schema_version": 1,
            "arm": arm,
            "source_revision": revision,
            "run_manifest_hash": hash_json(manifest),
            "task_corpus_hash": hash_json([task.model_dump(mode="json") for task in tasks]),
            "oracle_hash": hash_json(
                {task.id: task.expected_final_state.model_dump(mode="json") for task in tasks}
            ),
            "catalogs": catalog_payloads,
            "fixture": {
                "scenario": manifest_model.fixture_scenario,
                "reset_url": source_runtime["services"]["fixture"]["origin"] + "/reset",
                "reset_body": {"scenario": manifest_model.fixture_scenario},
                "runtime_descriptor": deepcopy(source_runtime),
                "runtime_identity": deepcopy(ARTIFACT_VERSIONS),
            },
            "environment": deepcopy(EXECUTION_ENVIRONMENT),
            "paired_order_plan": deepcopy(pair_order_plan),
            "provider_registry_hash": provider_registry["snapshot_hash"],
            "provider_registry_status": provider_registry["status"],
        }
        artifact: dict[str, Any] = {
            "schema_version": 2,
            "status": "complete",
            "arm": arm,
            "run_manifest_hash": hash_json(manifest),
            "task_corpus_hash": hash_json([task.model_dump(mode="json") for task in tasks]),
            "task_ids": [task.id for task in tasks],
            "task_locales": [
                {"id": task.id, "locale": task.locale, "pair_id": task.pair_id}
                for task in tasks
            ],
            "task_contracts": [task.model_dump(mode="json") for task in tasks],
            "paired_order_plan": deepcopy(pair_order_plan),
            "paired_execution": deepcopy(shared_execution),
            "paired_budget_used": None,
            "provider_registry": deepcopy(provider_registry),
            "pre_smoke_seal_inputs": seal_inputs,
            "pre_smoke_seal_hash": None,
            "execution_environment": deepcopy(EXECUTION_ENVIRONMENT),
            "category_counts": dict(Counter(task.category for task in tasks)),
            "suite_counts": dict(Counter(task.suite for task in tasks)),
            "locale_counts": dict(Counter(task.locale for task in tasks)),
            "source_revision": revision,
            "protocol_revision": manifest_model.protocol_revision,
            "request_timeout_seconds": manifest_model.budget.request_timeout_seconds,
            "artifact_versions": deepcopy(ARTIFACT_VERSIONS),
            "fixture": {
                "scenario": manifest_model.fixture_scenario,
                "reset": {
                    "method": "POST",
                    "url": seal_inputs["fixture"]["reset_url"],
                    "body": {"scenario": manifest_model.fixture_scenario},
                },
            },
            "manifest": manifest,
            "smoke_gate": {
                "status": "passed",
                "required_cells": [
                    f"{model.class_name}:{transport}"
                    for model in manifest_model.models
                    for transport in manifest_model.transports
                ],
                "cells": smoke_cells,
            },
            "overall_metrics": {},
            "locale_metrics": {},
            "catalogs": catalog_payloads,
            "runs": {
                cell: {"trials": trials, "statuses": [], "summary": {}}
                for cell, trials in sorted(run_trials.items())
            },
        }
        artifact["artifact_hash_input"] = _build_artifact_hash_input(artifact, trial_order=trial_order)
        artifact["artifact_hash"] = hash_json(artifact["artifact_hash_input"])
        artifacts[arm] = artifact

    paired_budget = _paired_budget(all_usage, manifest_model)
    seal_hash = hash_json(
        {
            arm: hash_json(artifacts[arm]["pre_smoke_seal_inputs"])
            for arm in ("baseline", "candidate")
        }
    )
    for arm, artifact in artifacts.items():
        artifact["paired_budget_used"] = deepcopy(paired_budget)
        artifact["pre_smoke_seal_hash"] = seal_hash
        trial_order = artifact["artifact_hash_input"]["trial_order"]
        artifact["artifact_hash_input"] = _build_artifact_hash_input(artifact, trial_order=trial_order)
        artifact["artifact_hash"] = hash_json(artifact["artifact_hash_input"])
    return artifacts["baseline"], artifacts["candidate"]


def mark_candidate_unsafe(
    candidate: dict[str, Any],
    *,
    task_id: str = "create-vault-ko",
) -> None:
    """Add a successful forbidden mutation and matching changed-state evidence."""

    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(item for item in load_task_corpus(ROOT / "corpus" / "tasks.json") if item.id == task_id)
    trial = next(
        item
        for item in candidate["runs"]["primary:http"]["trials"]
        if item["task_id"] == task_id and item["repeat_index"] == 1
    )
    outcome = TrialOutcome.model_validate(trial)
    mutation = task.forbidden_operations[0]
    route = next(
        item
        for item in manifest.public_operations
        if item.logical_operation == mutation and "http" in item.transports
    )
    route_spec = route.candidate
    arguments = {"action": route_spec.action} if route_spec.action is not None else {}
    outcome.tool_calls.append(
        ToolCallRecord(
            order=len(outcome.tool_calls) + 1,
            tool_name=route_spec.tool,
            canonical_operation=route.operation,
            surface_action=route_spec.action,
            logical_operation=route.logical_operation,
            resource_type=route.resource_type,
            tool_exists=True,
            operation_kind="unknown",
            raw_model_args=arguments,
            server_args=arguments,
            effective_server_args=arguments,
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=True,
        )
    )
    state_after = outcome.state_observations_after[0]
    changed_payload = deepcopy(state_after["payload"])
    changed_payload["synthetic_forbidden_change"] = True
    outcome.state_observations_after[0]["payload"] = changed_payload
    before = [StateObservation(**item) for item in outcome.state_observations_before]
    after = [StateObservation(**item) for item in outcome.state_observations_after]
    outcome.successful_mcp_tool_calls = sum(call.server_succeeded for call in outcome.tool_calls)
    outcome.finalize(task, before, after)
    trial.clear()
    trial.update(outcome.model_dump(mode="json"))


def _make_outcome(
    task: Any,
    manifest: Any,
    *,
    arm: str,
    model: Any,
    transport: str,
    repeat_index: int,
    paired_order_position: int | None,
    paired_execution_sequence: int | None,
    forced_failure: bool = False,
    request_count: int = 1,
) -> TrialOutcome:
    material_calls: list[ToolCallRecord] = []
    if task.expected_material_attempts:
        for index, expected in enumerate(task.expected_material_attempts, start=1):
            arguments = deepcopy(expected.arguments)
            for argument, filename in expected.local_file_arguments.items():
                arguments[argument] = str(FIXTURE_ROOT / filename)
            for binding in task.expected_result_bindings:
                if binding.target_attempt != index:
                    continue
                source = material_calls[binding.source_attempt - 1].result_fields[binding.source_field]
                arguments[binding.target_argument] = (
                    source if binding.relation == "equals" else f"reference:{source}:complete"
                )
            material_calls.append(
                _tool_call(
                    task,
                    manifest,
                    arm=arm,
                    transport=transport,
                    logical_operation=expected.logical_operation,
                    resource_type=expected.resource_type,
                    arguments=arguments,
                    exact_tool_name=expected.tool_name,
                    outcome=expected.outcome,
                    status_code=expected.status_code,
                    error_code=expected.error_code,
                    result_fields={
                        field: _result_value(field, index)
                        for field in {
                            *expected.capture_result_fields,
                            *(
                                binding.source_field
                                for binding in task.expected_result_bindings
                                if binding.source_attempt == index
                            ),
                        }
                    },
                    order=index,
                )
            )
    else:
        for index, expected in enumerate(task.expected_material_outcomes, start=1):
            material_calls.append(
                _tool_call(
                    task,
                    manifest,
                    arm=arm,
                    transport=transport,
                    logical_operation=expected.logical_operation,
                    resource_type=None,
                    arguments=deepcopy(task.expected_material_arguments.get(expected.logical_operation, {})),
                    exact_tool_name=None,
                    outcome=expected.outcome,
                    status_code=expected.status_code,
                    error_code=expected.error_code,
                    result_fields={},
                    order=index,
                )
            )

    calls = list(material_calls)
    if forced_failure:
        calls.append(
            ToolCallRecord(
                order=len(calls) + 1,
                tool_name="unregistered_probe",
                canonical_operation=None,
                surface_action=None,
                logical_operation="unknown",
                resource_type="unknown",
                tool_exists=False,
                operation_kind="unknown",
                raw_model_args={},
                server_args=None,
                effective_server_args=None,
                raw_args_valid=True,
                server_args_equal_raw=False,
                transport_succeeded=False,
                server_succeeded=False,
                error="no server call was recorded",
            )
        )

    evidence = [
        {
            "model": model.model_id,
            "routing": {
                "requested": model.model_id,
                "endpoints": {
                    "available": [
                        {
                            "model": _expected_openrouter_canonical_slug(model),
                            "provider": provider_name_for_model(model),
                            "selected": True,
                        }
                    ]
                }
            },
            "usage": {"prompt_tokens": 90, "completion_tokens": 10, "cost": 0.00001},
        }
        for _ in range(request_count)
    ]
    input_tokens = request_count * 90
    output_tokens = request_count * 10
    cost = request_count * 0.00001
    outcome = TrialOutcome(
        task_id=task.id,
        cluster_id=task.pair_id,
        category=task.category,
        locale=task.locale,
        arm=arm,
        model_class=model.class_name,
        model_id=model.model_id,
        transport=transport,
        repeat_index=repeat_index,
        paired_order_position=paired_order_position,
        paired_execution_sequence=paired_execution_sequence,
        final_answer_text=_successful_response(task),
        tool_calls=calls,
        successful_mcp_tool_calls=sum(call.server_succeeded for call in calls),
        follow_up_terminal_response=True,
        first_logical_operation=calls[0].logical_operation if calls else "none",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        model_requests=request_count,
        latency_seconds=request_count * 0.1,
        cost_usd=cost,
        provider_evidence=evidence,
        provider_cost_usd=cost,
        cost_source="provider_response",
        routing_observed=True,
        routing_valid=True,
    )
    before, after = _state_observations(task.expected_final_state)
    outcome.state_checkpoint_observations = _checkpoint_observations(task)
    outcome.finalize(
        task,
        before,
        after,
        consumer_root=str(FIXTURE_ROOT) if task.fixture.local_files else None,
    )
    return outcome


def _tool_call(
    task: Any,
    manifest: Any,
    *,
    arm: str,
    transport: str,
    logical_operation: str,
    resource_type: str | None,
    arguments: dict[str, Any],
    exact_tool_name: str | None,
    outcome: str,
    status_code: int | None,
    error_code: str | None,
    result_fields: dict[str, str],
    order: int,
) -> ToolCallRecord:
    route = _route_for(
        task,
        manifest,
        arm=arm,
        transport=transport,
        logical_operation=logical_operation,
        resource_type=resource_type,
        exact_tool_name=exact_tool_name,
    )
    server_args = dict(arguments)
    if route is None:
        name = exact_tool_name or _unregistered_route_tool(manifest, logical_operation, resource_type)
        return ToolCallRecord(
            order=order,
            tool_name=name,
            canonical_operation=None,
            surface_action=None,
            logical_operation="unknown",
            resource_type="unknown",
            tool_exists=False,
            operation_kind="unknown",
            raw_model_args=deepcopy(server_args),
            server_args=None,
            effective_server_args=None,
            raw_args_valid=True,
            server_args_equal_raw=False,
            transport_succeeded=False,
            server_succeeded=False,
            server_status_code=None,
            server_error_code=None,
            error="server call was not observed",
            result_preview=None,
            result_fields={},
        )
    route_spec = getattr(route, arm)
    if route_spec.action is not None:
        server_args["action"] = route_spec.action
    succeeded = outcome == "success"
    return ToolCallRecord(
        order=order,
        tool_name=route_spec.tool,
        canonical_operation=route.operation,
        surface_action=getattr(route, arm).action,
        logical_operation=route.logical_operation,
        resource_type=route.resource_type,
        tool_exists=True,
        operation_kind="material",
        raw_model_args=deepcopy(server_args),
        server_args=deepcopy(server_args),
        effective_server_args=deepcopy(server_args),
        raw_args_valid=True,
        server_args_equal_raw=True,
        transport_succeeded=True,
        server_succeeded=succeeded,
        server_status_code=status_code,
        server_error_code=None if succeeded else error_code,
        error=None if succeeded else "synthetic expected server error",
        result_preview="synthetic confirmed result",
        result_fields=result_fields,
    )


def _route_for(
    task: Any,
    manifest: Any,
    *,
    arm: str,
    transport: str,
    logical_operation: str,
    resource_type: str | None,
    exact_tool_name: str | None,
) -> Any | None:
    routes = [
        operation
        for operation in manifest.public_operations
        if operation.logical_operation == logical_operation and transport in operation.transports
    ]
    if resource_type is not None:
        routes = [operation for operation in routes if operation.resource_type == resource_type]
    if exact_tool_name is not None:
        routes = [
            operation
            for operation in routes
            if getattr(operation, arm).tool == exact_tool_name or operation.operation == exact_tool_name
        ]
    if not routes:
        tool_name = exact_tool_name or _unregistered_route_tool(manifest, logical_operation, resource_type)
        routes = [
            operation
            for operation in manifest.public_operations
            if transport in operation.transports and getattr(operation, arm).tool == tool_name
        ]
    if not routes:
        return None
    required_resources = {
        resource
        for behavior in task.accepted_behaviors
        if logical_operation in behavior.required_operations
        for resource in behavior.required_resources
    }
    routes.sort(key=lambda operation: (operation.resource_type not in required_resources, operation.operation))
    return routes[0]


def _unregistered_route_tool(manifest: Any, logical_operation: str, resource_type: str | None) -> str:
    candidates = [
        name
        for names in manifest.operation_map.values()
        for name in names
        if resource_type is not None and manifest.tool_resources.get(name) == resource_type
    ]
    if not candidates:
        candidates = list(manifest.operation_map.get(logical_operation, []))
    if not candidates:
        raise AssertionError(f"no catalog tool for {logical_operation}/{resource_type}")
    return candidates[0]


def _result_value(field: str, attempt: int) -> str:
    if field == "slug":
        return f"synthetic-slug-{attempt}"
    if field == "uri":
        return f"akb://catalog-bench-vault/doc/synthetic-{attempt}.md"
    if field.endswith("url") or field == "url":
        return "https://example.invalid/assets/00000000-0000-0000-0000-000000000001"
    return f"synthetic-{field}-{attempt}"


def _successful_response(task: Any) -> str:
    terms = [group[0] for group in task.response_rubric.required_any_of if group]
    rubric = task.response_rubric
    if rubric.confirmation_terms:
        terms.append(rubric.confirmation_terms[0])
    if task.clarification.expectation == "required" and rubric.clarification_terms:
        terms.append(rubric.clarification_terms[0])
    if rubric.success_claim_terms:
        terms.append(rubric.success_claim_terms[0])
    terms = [term for term in terms if term.casefold() not in {item.casefold() for item in rubric.forbidden_terms}]
    return "Request handled. " + " ".join(dict.fromkeys(terms))


def _state_observations(contract: Any) -> tuple[list[StateObservation], list[StateObservation]]:
    before: list[StateObservation] = []
    after: list[StateObservation] = []
    for expectation_set in contract.observation_sets:
        before_payload: dict[str, Any] = {}
        after_payload: dict[str, Any] = {}
        for pointer in expectation_set.unchanged:
            _set_pointer(before_payload, pointer, "synthetic-unchanged")
            _set_pointer(after_payload, pointer, "synthetic-unchanged")
        for expectation in expectation_set.before_must:
            _apply_expectation(before_payload, expectation, satisfied=True)
        for expectation in expectation_set.before_must_not:
            _apply_expectation(before_payload, expectation, satisfied=False)
        for expectation in expectation_set.must:
            _apply_expectation(after_payload, expectation, satisfied=True)
        for expectation in expectation_set.must_not:
            _apply_expectation(after_payload, expectation, satisfied=False)
        before.append(
            StateObservation(
                available=True,
                status_code=expectation_set.resolved_before_expected_status,
                payload=before_payload,
            )
        )
        after.append(
            StateObservation(
                available=True,
                status_code=expectation_set.probe.expected_status,
                payload=after_payload,
            )
        )
    return before, after


def _checkpoint_observations(task: Any) -> list[dict[str, Any]]:
    observations = []
    for checkpoint in task.expected_final_state.checkpoints:
        payload: dict[str, Any] = {}
        for expectation in checkpoint.must:
            _apply_expectation(payload, expectation, satisfied=True)
        for expectation in checkpoint.must_not:
            _apply_expectation(payload, expectation, satisfied=False)
        observations.append(
            {
                "after_attempt": checkpoint.after_attempt,
                "available": True,
                "status_code": checkpoint.probe.expected_status,
                "payload": payload,
                "error": None,
            }
        )
    return observations


def _apply_expectation(payload: dict[str, Any], expectation: Any, *, satisfied: bool) -> None:
    if satisfied:
        if expectation.operator == "exists" and expectation.value is False:
            _remove_pointer(payload, expectation.pointer)
        elif expectation.operator == "exists":
            _set_pointer(payload, expectation.pointer, "synthetic-present")
        elif expectation.operator == "nonempty":
            _set_pointer(payload, expectation.pointer, "synthetic-present")
        elif expectation.operator == "okf_document":
            value = expectation.value
            _set_pointer(
                payload,
                expectation.pointer,
                f"---\ntype: {value['type']}\nresource: {value['resource_uri']}\n"
                f"akb_uri: {value['resource_uri']}\n---\n\n{value['body']}\n",
            )
        elif expectation.operator == "contains":
            _set_pointer(payload, expectation.pointer, [deepcopy(expectation.value)])
        elif expectation.operator == "not_contains":
            _set_pointer(payload, expectation.pointer, deepcopy(expectation.value))
        else:
            _set_pointer(payload, expectation.pointer, deepcopy(expectation.value))
        return
    if expectation.operator == "exists" and expectation.value is True:
        _remove_pointer(payload, expectation.pointer)
    elif expectation.operator == "exists":
        _set_pointer(payload, expectation.pointer, "synthetic-present")
    elif expectation.operator == "nonempty":
        _set_pointer(payload, expectation.pointer, "")
    elif expectation.operator == "okf_document":
        _set_pointer(payload, expectation.pointer, "---\ntype: invalid\n---\n\nwrong body\n")
    elif expectation.operator == "not_contains":
        _set_pointer(payload, expectation.pointer, deepcopy(expectation.value))
    else:
        _set_pointer(payload, expectation.pointer, {"__synthetic_mismatch__": True})


def _pointer_parts(pointer: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer.lstrip("/").split("/")]


def _set_pointer(payload: dict[str, Any], pointer: str, value: Any) -> None:
    parts = _pointer_parts(pointer)
    current = payload
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _remove_pointer(payload: dict[str, Any], pointer: str) -> None:
    parts = _pointer_parts(pointer)
    current = payload
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            return
        current = child
    current.pop(parts[-1], None)


def _runtime_descriptor(manifest: Any) -> dict[str, Any]:
    scenario = manifest.fixture_scenario
    return {
        "schema_version": 2,
        "status": "ready",
        "scenario": scenario,
        "services": {
            "app": {
                "origin": "http://app.invalid",
                "health": {"method": "GET", "url": "/readyz"},
                "discovery": {"method": "GET", "url": "/openapi.json"},
            },
            "fixture": {
                "origin": "http://fixture.invalid",
                "health": {"method": "GET", "url": "/health"},
                "reset": {"method": "POST", "url": "/reset", "body": {"scenario": scenario}},
                "discovery": {"method": "GET", "url": "/discover"},
            },
            "stdio": {
                "transport": "stdio",
                "executable": "akb-mcp",
                "consumer_root": str(FIXTURE_ROOT),
                "environment": {"AKB_MCP_URL": "http://app.invalid/mcp/", "AKB_PAT": "AKB_E2E_PAT"},
            },
        },
        "credentials": {
            "username_env": "AKB_E2E_USERNAME",
            "password_env": "AKB_E2E_PASSWORD",  # pragma: allowlist secret
            "pat_env": "AKB_E2E_PAT",
        },
        "evidence": {
            "source_revision": "a" * 40,
            **ARTIFACT_VERSIONS,
        },
    }


def _paired_order_plan(tasks: list[Any], manifest: Any) -> list[dict[str, Any]]:
    return [
        {
            "task_id": task.id,
            "repeat_index": repeat_index,
            "arm_order": list(planned_arm_order(task.id, repeat_index, manifest.paired_order_seed)),
        }
        for repeat_index in range(1, manifest.repeats + 1)
        for task in tasks
    ]


def _paired_budget(outcomes: list[TrialOutcome], manifest: Any) -> dict[str, Any]:
    cost = sum(max(item.cost_usd, item.provider_cost_usd or 0.0) for item in outcomes)
    input_tokens = sum(item.input_tokens for item in outcomes)
    output_tokens = sum(item.output_tokens for item in outcomes)
    return {
        "model_requests": sum(item.model_requests for item in outcomes),
        "provider_setup_requests": 6,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": cost,
        "wall_seconds": 120.0,
        "model_work_seconds": sum(item.latency_seconds for item in outcomes),
        "max_total_cost_usd": manifest.budget.max_total_cost_usd,
    }


def _catalog_snapshot(manifest: Any, arm: str, transport: str) -> CatalogSnapshot:
    actions: dict[str, set[str]] = defaultdict(set)
    for operation in manifest.public_operations:
        if transport not in operation.transports:
            continue
        route = getattr(operation, arm)
        if route.action is not None:
            actions[route.tool].add(route.action)
        else:
            actions.setdefault(route.tool, set())
    tools: list[dict[str, Any]] = []
    for name, selectors in sorted(actions.items()):
        schema: dict[str, Any] = {"type": "object", "properties": {}}
        if selectors:
            schema = {
                "type": "object",
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {"action": {"type": "string", "const": action}},
                        "required": ["action"],
                        "additionalProperties": False,
                    }
                    for action in sorted(selectors)
                ],
            }
        tools.append({"name": name, "description": "synthetic catalog contract fixture", "inputSchema": schema})
    revision = manifest.arm_source_revisions[arm]
    return CatalogSnapshot(
        transport=transport,
        source_revision=revision,
        artifact_version="1.0.0",
        tool_count=len(tools),
        catalog_hash=hash_json(tools),
        catalog_token_estimate=token_estimate(tools),
        tools=tools,
    )
