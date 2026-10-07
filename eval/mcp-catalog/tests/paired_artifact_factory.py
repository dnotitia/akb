from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp_catalog.contracts import CatalogSnapshot, hash_json, load_run_manifest, load_task_corpus, token_estimate
from mcp_catalog.execution import TrialOutcome
from mcp_catalog.runner import _build_artifact_hash_input, planned_arm_order

ROOT = Path(__file__).parents[1]


def provider_registry_snapshot(manifest: Any | None = None) -> dict[str, Any]:
    manifest_model = manifest or load_run_manifest(ROOT / "config" / "run.json")
    payload = {
        "status": "verified",
        "models": {
            model.model_id: {
                "manifest_version": model.version,
                "selected_endpoint": {"provider_name": "Parasail", "quantization": "fp8"},
            }
            for model in manifest_model.models
        },
    }
    return {**payload, "snapshot_hash": hash_json(payload)}


def complete_paired_artifacts() -> tuple[dict[str, Any], dict[str, Any]]:
    """Create sealed, complete synthetic evidence for the registered 608-outcome plan."""

    manifest_model = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    manifest = manifest_model.model_dump(mode="json")
    pair_order = list(dict.fromkeys(task.pair_id for task in tasks))
    pair_index = {pair_id: index for index, pair_id in enumerate(pair_order)}
    arms = ("baseline", "candidate")

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
    paired_budget = {
        "model_requests": 620,
        "provider_setup_requests": 6,
        "input_tokens": 27_360,
        "output_tokens": 3_040,
        "total_tokens": 30_400,
        "cost_usd": 0.2,
        "wall_seconds": 120.0,
        "model_work_seconds": 120.0,
        "max_model_requests": 3_000,
        "max_total_cost_usd": 50.0,
        "max_wall_seconds": 10_800,
    }
    paired_order_plan = [
        {
            "task_id": task.id,
            "repeat_index": repeat_index,
            "arm_order": list(planned_arm_order(task.id, repeat_index, manifest_model.paired_order_seed)),
        }
        for repeat_index in range(1, manifest_model.repeats + 1)
        for task in tasks
    ]
    artifacts: dict[str, dict[str, Any]] = {}
    for arm in arms:
        revision = manifest_model.arm_source_revisions[arm]
        catalogs = {
            f"{transport}:{profile}": _catalog_snapshot(manifest_model, arm, transport)
            for transport in manifest_model.transports
            for profile in ("default", "authorization")
        }
        run_trials: dict[str, list[dict[str, Any]]] = defaultdict(list)
        trial_order: list[dict[str, Any]] = []
        for model in manifest_model.models:
            for transport in manifest_model.transports:
                cell = f"{model.class_name}:{transport}"
                for task in tasks:
                    if transport not in task.fixture.transports:
                        continue
                    locale_slot = 0 if task.locale == "en-US" else 2
                    cluster_failures = 1 + pair_index[task.pair_id] % 3
                    for repeat_index in range(1, manifest_model.repeats + 1):
                        slot = locale_slot + repeat_index - 1
                        baseline_error = arm == "baseline" and slot < cluster_failures
                        base_success = slot >= cluster_failures
                        success = True if arm == "candidate" else base_success
                        identity = (cell, task.id, repeat_index, arm)
                        order = planned_arm_order(task.id, repeat_index, manifest_model.paired_order_seed)
                        model_requests = 1
                        evidence = [
                            {
                                "model": model.model_id,
                                "routing": {
                                    "endpoints": {
                                        "available": [
                                            {
                                                "provider": "Parasail",
                                                "selected": True,
                                                "quantization": "fp8",
                                            }
                                        ]
                                    }
                                },
                                "usage": {
                                    "prompt_tokens": 90,
                                    "completion_tokens": 10,
                                    "cost": 0.00001,
                                },
                            }
                        ]
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
                            paired_order_position=order.index(arm),
                            paired_execution_sequence=event_sequence[identity],
                            final_answer_text="complete",
                            first_logical_operation=task.allowed_material_operations[0],
                            first_material_operation=task.allowed_material_operations[0],
                            successful_mcp_tool_calls=1,
                            follow_up_terminal_response=True,
                            first_action_accuracy=True,
                            first_material_action_accuracy=True,
                            argument_validity=True,
                            required_operations_completed=True,
                            required_attempts_completed=True,
                            tool_outcome_match=True,
                            user_outcome_completed=True,
                            accepted_behavior_matched=True,
                            success=success,
                            safety=True,
                            trial_error=baseline_error,
                            trial_error_kinds=["unsupported_success_claim"] if baseline_error else [],
                            input_tokens=90,
                            output_tokens=10,
                            total_tokens=100,
                            model_requests=model_requests,
                            cost_usd=0.00001,
                            provider_evidence=evidence,
                            provider_cost_usd=0.00001,
                            cost_source="provider_response",
                            routing_observed=True,
                            routing_valid=True,
                        )
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

        local_seal_inputs = {
            "arm": arm,
            "source_revision": revision,
            "run_manifest_hash": hash_json(manifest),
            "task_corpus_hash": hash_json([task.model_dump(mode="json") for task in tasks]),
            "paired_order_plan": paired_order_plan,
            "provider_registry_hash": provider_registry["snapshot_hash"],
        }
        artifact: dict[str, Any] = {
            "schema_version": 1,
            "status": "complete",
            "arm": arm,
            "run_manifest_hash": hash_json(manifest),
            "task_corpus_hash": hash_json([task.model_dump(mode="json") for task in tasks]),
            "task_ids": [task.id for task in tasks],
            "task_locales": [
                {"id": task.id, "locale": task.locale, "pair_id": task.pair_id}
                for task in tasks
            ],
            "task_contracts": [
                {"id": task.id, "capability_families": task.capability_families}
                for task in tasks
            ],
            "paired_order_plan": deepcopy(paired_order_plan),
            "paired_execution": deepcopy(shared_execution),
            "paired_budget_used": deepcopy(paired_budget),
            "provider_registry": deepcopy(provider_registry),
            "pre_smoke_seal_inputs": local_seal_inputs,
            "pre_smoke_seal_hash": hash_json({"paired_pre_smoke_seal": "sealed-inputs"}),
            "category_counts": dict(Counter(task.category for task in tasks)),
            "suite_counts": dict(Counter(task.suite for task in tasks)),
            "locale_counts": dict(Counter(task.locale for task in tasks)),
            "source_revision": revision,
            "protocol_revision": manifest_model.protocol_revision,
            "request_timeout_seconds": manifest_model.budget.request_timeout_seconds,
            "artifact_versions": {"backend_artifact_version": "1.0.0", "proxy_artifact_version": "1.0.0"},
            "fixture": {
                "scenario": "app-control-plane",
                "reset": {
                    "method": "POST",
                    "url": "http://fixture/reset",
                    "body": {"scenario": "app-control-plane"},
                },
            },
            "manifest": manifest,
            "smoke_gate": {
                "status": "passed",
                "required_cells": ["primary:http", "primary:stdio", "lightweight:http", "lightweight:stdio"],
                "cells": [],
            },
            "overall_metrics": {},
            "locale_metrics": {},
            "catalogs": {key: snapshot.model_dump(mode="json") for key, snapshot in catalogs.items()},
            "runs": {
                cell: {"trials": trials, "statuses": [], "summary": {}}
                for cell, trials in sorted(run_trials.items())
            },
        }
        artifact["artifact_hash_input"] = _build_artifact_hash_input(artifact, trial_order=trial_order)
        artifact["artifact_hash"] = hash_json(artifact["artifact_hash_input"])
        artifacts[arm] = artifact
    return artifacts["baseline"], artifacts["candidate"]


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
            schema["properties"]["action"] = {"type": "string", "enum": sorted(selectors)}
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
