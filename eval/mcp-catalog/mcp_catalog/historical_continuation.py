"""Read-only validation for historical AKB-361 continuation artifacts."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

from .contracts import BenchmarkRunManifest, hash_json
from .evidence import safe_json
from .runtime import RuntimeDescriptor

WORK_ITEM = "AKB-361"
AUTHORIZATION_REVISION = "20a37d0a18ae33a22f035d388a16988ca65115e5"  # pragma: allowlist secret
AUTHORIZATION_CONTENT_HASH = "df9afde7401506b7e12ea1878b3f7a0e04d197e750d9e02b18251db430bbebb9"  # pragma: allowlist secret
SOURCE_MANIFEST_HASH = "b1a805335aa83335d7e4b44cb1305b272348fc7f3aa8b7ae9342f67eaa42ec25"  # pragma: allowlist secret
SOURCE_TASK_CORPUS_HASH = "51028d1a27632af0ed524dc0f156780ebfecf0df4e0e24012fe4cf6e0e3ace62"  # pragma: allowlist secret
SOURCE_PRE_SMOKE_SEAL_HASH = "9f72c9d9f24325ef3965cc8852fbbcb205e7c87298d62d63c2b6202c338388e1"  # pragma: allowlist secret
SOURCE_ARTIFACT_SHA256 = {
    "baseline": "59d9b823f5a2247311ec286700a85d6dce20240f04b9012f7a3da8be18107900",  # pragma: allowlist secret
    "candidate": "5a376f45ccab370b73d9f31c3fddee0e338f2683c35769166b20495e1928f435",  # pragma: allowlist secret
}
SOURCE_CHECKPOINT_SHA256 = {
    "baseline": "0fba860846d1d9bf36abd14f360075e18ba31a160cf39f189550d570142f56ca",  # pragma: allowlist secret
    "candidate": "bb08b84d6dcc3dc8a43ba8f40103dbc790411842d369ad4605a59fb1cfcd7a9c",  # pragma: allowlist secret
}
SOURCE_PAIRED_EVENT_COUNT = 595
SOURCE_TERMINAL_COUNTS = {"baseline": 298, "candidate": 297}
SOURCE_COST_USD = Decimal("4.63290984")
PRIOR_ACCOUNT_OBSERVATION_USD = Decimal("7.86955884")
ACCOUNT_LIMIT_USD = Decimal("50.00000000")
CONTINUATION_RUN_CAP_USD = Decimal("46.76335100")
CONTINUATION_ALLOWANCE_USD = Decimal("42.13044116")

TrialIdentity = tuple[str, str, int, str]
ARMS = ("baseline", "candidate")


def _expected_missing_identities() -> set[TrialIdentity]:
    values: set[TrialIdentity] = set()
    for transport in ("http", "stdio"):
        cell = f"lightweight:{transport}"
        for task_id in ("access-ownership-public-ko", "access-ownership-public-en"):
            for arm in ARMS:
                values.add((cell, task_id, 2, arm))
    for task_id in ("stdio-image-files-ko", "stdio-image-files-en"):
        for arm in ARMS:
            values.add(("lightweight:stdio", task_id, 2, arm))
    values.add(("lightweight:stdio", "table-schema-conflict-en", 2, "candidate"))
    return values


EXPECTED_MISSING_IDENTITIES = frozenset(_expected_missing_identities())
RuntimeFieldPath = tuple[str, ...]


def _get_path(value: dict[str, Any], path: RuntimeFieldPath) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"runtime descriptor is missing the registered field /{'/'.join(path)}")
        current = current[part]
    return current


def _set_path(value: dict[str, Any], path: RuntimeFieldPath, replacement: Any) -> None:
    current: Any = value
    for part in path[:-1]:
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"runtime descriptor is missing the registered field /{'/'.join(path)}")
        current = current[part]
    if not isinstance(current, dict) or path[-1] not in current:
        raise ValueError(f"runtime descriptor is missing the registered field /{'/'.join(path)}")
    current[path[-1]] = replacement


def _json_pointer(path: RuntimeFieldPath) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path)


def _runtime_reallocation_fields(descriptor: dict[str, Any]) -> dict[RuntimeFieldPath, str]:
    """Collect only the recorded runtime lease fields allowed to change."""

    fields: dict[RuntimeFieldPath, str] = {}

    def add_identity(evidence: Any, prefix: RuntimeFieldPath) -> None:
        if not isinstance(evidence, dict):
            raise ValueError("runtime descriptor identity evidence is missing")
        process_identity = evidence.get("process_identity")
        if isinstance(process_identity, dict):
            if not process_identity:
                raise ValueError("runtime process identity evidence is empty")
            for process_name, process in process_identity.items():
                if (
                    not isinstance(process_name, str)
                    or not isinstance(process, dict)
                    or isinstance(process.get("pid"), bool)
                    or not isinstance(process.get("pid"), int)
                    or process["pid"] <= 0
                    or process.get("running") is not True
                ):
                    raise ValueError("runtime process identity must name a running process with a positive PID")
                fields[prefix + ("process_identity", process_name, "pid")] = "pid"

        dependency_identity = evidence.get("dependency_identity")
        if isinstance(dependency_identity, dict):
            services = dependency_identity.get("services")
            if not isinstance(services, dict) or not services:
                raise ValueError("runtime dependency identity services are missing")
            for service_name, service in services.items():
                if not isinstance(service_name, str) or not isinstance(service, dict):
                    raise ValueError("runtime dependency identity service is malformed")
                container_id = service.get("container_id")
                if not isinstance(container_id, str) or not container_id:
                    raise ValueError("runtime dependency container identity is missing")
                fields[prefix + ("dependency_identity", "services", service_name, "container_id")] = "container_id"
                for identity_name in ("network_ids", "volume_names"):
                    identities = service.get(identity_name)
                    if (
                        not isinstance(identities, list)
                        or not identities
                        or any(not isinstance(item, str) or not item for item in identities)
                    ):
                        raise ValueError(f"runtime dependency {identity_name} must be a non-empty string list")
                    fields[prefix + ("dependency_identity", "services", service_name, identity_name)] = (
                        "network_ids" if identity_name == "network_ids" else "volume_names"
                    )

        fixture = evidence.get("fixture")
        if isinstance(fixture, dict) and "namespace" in fixture:
            namespace = fixture["namespace"]
            if not isinstance(namespace, str) or not namespace:
                raise ValueError("runtime fixture namespace is invalid")
            fields[prefix + ("fixture", "namespace")] = "namespace"

        stdio = evidence.get("stdio")
        if isinstance(stdio, dict) and "consumer_root" in stdio:
            consumer_root = stdio["consumer_root"]
            if not isinstance(consumer_root, str) or not Path(consumer_root).is_absolute():
                raise ValueError("runtime stdio consumer root must be absolute")
            fields[prefix + ("stdio", "consumer_root")] = "consumer_root"

    evidence = descriptor.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("runtime descriptor identity evidence is missing")
    add_identity(evidence, ("evidence",))
    evidence_cells = evidence.get("benchmark_cells")
    if not isinstance(evidence_cells, dict) or not evidence_cells:
        raise ValueError("runtime descriptor benchmark-cell evidence is missing")
    for cell, cell_evidence in evidence_cells.items():
        if not isinstance(cell, str):
            raise ValueError("runtime descriptor benchmark-cell key is invalid")
        add_identity(cell_evidence, ("evidence", "benchmark_cells", cell))

    benchmark_cells = descriptor.get("benchmark_cells")
    if not isinstance(benchmark_cells, dict) or set(benchmark_cells) != set(evidence_cells):
        raise ValueError("runtime descriptor benchmark-cell matrix is inconsistent")
    for cell, cell_descriptor in benchmark_cells.items():
        if not isinstance(cell_descriptor, dict):
            raise ValueError("runtime descriptor benchmark cell is malformed")
        add_identity(cell_descriptor.get("evidence"), ("benchmark_cells", cell, "evidence"))

    def add_service_consumer_root(services: Any, prefix: RuntimeFieldPath) -> None:
        if not isinstance(services, dict):
            raise ValueError("runtime descriptor services are missing")
        stdio_service = services.get("stdio")
        if not isinstance(stdio_service, dict):
            raise ValueError("runtime descriptor stdio service is missing")
        consumer_root = stdio_service.get("consumer_root")
        if not isinstance(consumer_root, str) or not Path(consumer_root).is_absolute():
            raise ValueError("runtime descriptor stdio consumer root must be absolute")
        fields[prefix + ("stdio", "consumer_root")] = "consumer_root"

    add_service_consumer_root(descriptor.get("services"), ("services",))
    for cell, cell_descriptor in benchmark_cells.items():
        add_service_consumer_root(cell_descriptor.get("services"), ("benchmark_cells", cell, "services"))

    required_kinds = {"pid", "container_id", "network_ids", "volume_names", "namespace", "consumer_root"}
    if set(fields.values()) < required_kinds:
        raise ValueError("runtime descriptor omits required process, dependency, fixture, or consumer identities")
    return fields


def _project_runtime_descriptor(descriptor: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = _runtime_reallocation_fields(descriptor)
    projected = deepcopy(descriptor)
    identity: dict[str, Any] = {}
    for path, kind in fields.items():
        value = _get_path(descriptor, path)
        identity[_json_pointer(path)] = deepcopy(value)
        if kind == "pid":
            replacement: Any = 0
        elif kind in {"network_ids", "volume_names"}:
            replacement = ["<runtime-lease-identity>"] * len(value)
        else:
            replacement = "<runtime-lease-identity>"
        _set_path(projected, path, replacement)
    return projected, identity


def _compare_runtime_descriptors(
    parent: dict[str, Any], current: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    parent_projection, parent_identity = _project_runtime_descriptor(parent)
    current_projection, current_identity = _project_runtime_descriptor(current)
    if set(parent_identity) != set(current_identity):
        raise ValueError("continuation runtime lease identity fields changed shape")
    if parent_projection != current_projection:
        raise ValueError("continuation runtime descriptor changes source or semantic fixture inputs")
    changed = sorted(path for path in parent_identity if parent_identity[path] != current_identity[path])
    if not changed:
        raise ValueError("continuation runtime descriptor does not identify a new runtime lease")
    return parent_projection, current_projection, parent_identity, current_identity, changed


def _finite_equal(actual: Any, expected: float) -> bool:
    return isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isclose(
        float(actual), expected, rel_tol=0, abs_tol=1e-9
    )


def validate_continuation_runtime_provenance(artifact: dict[str, Any]) -> None:
    """Recompute the parent-to-child runtime boundary from a stored artifact."""

    provenance = artifact.get("continuation_runtime_provenance")
    parent_inputs = artifact.get("pre_smoke_seal_inputs")
    if not isinstance(provenance, dict) or not isinstance(parent_inputs, dict):
        raise ValueError("continuation runtime provenance or parent seal is missing")
    expected_keys = {
        "schema_version",
        "parent_descriptor_sha256",
        "current_descriptor_sha256",
        "current_descriptor",
        "current_descriptor_json",
        "parent_semantic_descriptor_sha256",
        "current_semantic_descriptor_sha256",
        "parent_runtime_identity_sha256",
        "current_runtime_identity_sha256",
        "reallocated_identity_paths",
        "current_pre_smoke_inputs_sha256",
        "runtime_observation",
    }
    if set(provenance) != expected_keys or provenance.get("schema_version") != 1:
        raise ValueError("continuation runtime provenance schema is invalid")
    parent_fixture = parent_inputs.get("fixture")
    parent_descriptor = parent_fixture.get("runtime_descriptor") if isinstance(parent_fixture, dict) else None
    if not isinstance(parent_descriptor, dict):
        raise ValueError("continuation parent runtime descriptor is missing")
    descriptor_json = provenance.get("current_descriptor_json")
    if not isinstance(descriptor_json, str):
        raise ValueError("continuation child runtime descriptor is not preserved as raw JSON")
    try:
        current_descriptor_raw = json.loads(descriptor_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"continuation child runtime descriptor JSON is invalid: {exc}") from exc
    if not isinstance(current_descriptor_raw, dict):
        raise ValueError("continuation child runtime descriptor must be an object")
    current_descriptor = safe_json(current_descriptor_raw)
    if current_descriptor != provenance.get("current_descriptor"):
        raise ValueError("continuation child descriptor raw and sanitized views differ")
    if (
        provenance.get("parent_descriptor_sha256") != hash_json(parent_descriptor)
        or provenance.get("current_descriptor_sha256") != hash_json(current_descriptor_raw)
    ):
        raise ValueError("continuation runtime descriptor digest does not match")
    try:
        RuntimeDescriptor.from_dict(current_descriptor_raw)
    except Exception as exc:
        raise ValueError(f"continuation child runtime descriptor is invalid: {exc}") from exc

    (
        parent_projection,
        current_projection,
        parent_identity,
        current_identity,
        changed_paths,
    ) = _compare_runtime_descriptors(parent_descriptor, current_descriptor)
    if (
        provenance.get("parent_semantic_descriptor_sha256") != hash_json(parent_projection)
        or provenance.get("current_semantic_descriptor_sha256") != hash_json(current_projection)
        or provenance.get("parent_runtime_identity_sha256") != hash_json(parent_identity)
        or provenance.get("current_runtime_identity_sha256") != hash_json(current_identity)
        or provenance.get("reallocated_identity_paths") != changed_paths
    ):
        raise ValueError("continuation runtime identity comparison cannot be independently reproduced")

    arm = artifact.get("arm")
    if arm not in {"baseline", "candidate"}:
        raise ValueError("continuation runtime provenance arm is invalid")
    manifest = artifact.get("manifest")
    try:
        validated_manifest = BenchmarkRunManifest.model_validate(manifest)
    except Exception as exc:
        raise ValueError(f"continuation runtime manifest is invalid: {exc}") from exc
    expected_cells = {
        f"{model.class_name}:{transport}"
        for model in validated_manifest.models
        for transport in validated_manifest.transports
    }
    descriptor = RuntimeDescriptor.from_dict(current_descriptor_raw)
    if set(descriptor.benchmark_cells) != expected_cells:
        raise ValueError("continuation child runtime descriptor cells differ from the registered matrix")

    reconstructed_inputs = deepcopy(parent_inputs)
    reconstructed_inputs["run_manifest_hash"] = artifact.get("run_manifest_hash")
    reconstructed_fixture = reconstructed_inputs.get("fixture")
    if not isinstance(reconstructed_fixture, dict):
        raise ValueError("continuation parent fixture inputs are invalid")
    reconstructed_fixture["runtime_descriptor"] = current_descriptor
    if provenance.get("current_pre_smoke_inputs_sha256") != hash_json(reconstructed_inputs):
        raise ValueError("continuation pre-smoke input digest cannot be reconstructed from its parent and child")

    expected_source_revision = parent_inputs.get("source_revision")
    expected_artifact_versions = parent_fixture.get("runtime_identity") if isinstance(parent_fixture, dict) else None
    observation = provenance.get("runtime_observation")
    if not isinstance(observation, dict) or set(observation) != {
        "readiness_status",
        "source_revision",
        "artifact_versions",
        "cells",
    }:
        raise ValueError("continuation runtime readiness observation is missing")
    if (
        observation.get("readiness_status") != "passed"
        or observation.get("source_revision") != expected_source_revision
        or observation.get("artifact_versions") != expected_artifact_versions
        or artifact.get("source_revision") != expected_source_revision
        or artifact.get("artifact_versions") != expected_artifact_versions
    ):
        raise ValueError("continuation runtime readiness or source versions differ from the parent")
    runtime = artifact.get("runtime")
    runtime_cells = runtime.get("cells") if isinstance(runtime, dict) else None
    observed_cells = observation.get("cells")
    if (
        not isinstance(runtime_cells, dict)
        or set(runtime_cells) != expected_cells
        or not isinstance(observed_cells, dict)
        or set(observed_cells) != expected_cells
    ):
        raise ValueError("continuation runtime readiness evidence does not cover every registered cell")
    for cell in sorted(expected_cells):
        discovery = runtime_cells[cell]
        cell_observation = observed_cells[cell]
        if not isinstance(discovery, dict) or not isinstance(cell_observation, dict) or set(cell_observation) != {
            "readiness_status",
            "source_revision",
            "artifact_versions",
            "discovery_sha256",
        }:
            raise ValueError(f"continuation runtime readiness evidence is invalid for {cell}")
        cell_descriptor = descriptor.cell_for(cell)
        if (
            cell_observation.get("readiness_status") != "passed"
            or cell_observation.get("source_revision") != expected_source_revision
            or cell_observation.get("artifact_versions") != expected_artifact_versions
            or cell_observation.get("discovery_sha256") != hash_json(discovery)
            or cell_descriptor.source_revision_from(discovery) != expected_source_revision
            or cell_descriptor.artifact_versions_from(discovery) != expected_artifact_versions
        ):
            raise ValueError(f"continuation runtime source, versions, or readiness evidence differs for {cell}")


def validate_artifact_lineage(artifact: dict[str, Any]) -> str:
    """Validate the recorded one-time continuation's lineage without executing it."""

    lineage = artifact.get("continuation_lineage")
    if not isinstance(lineage, dict):
        raise ValueError("continuation artifact lineage is missing")
    if (
        lineage.get("schema_version") != 1
        or lineage.get("work_item") != WORK_ITEM
        or lineage.get("authorization_revision") != AUTHORIZATION_REVISION
        or lineage.get("authorization_content_hash") != AUTHORIZATION_CONTENT_HASH
        or lineage.get("source_run_manifest_hash") != SOURCE_MANIFEST_HASH
        or lineage.get("continuation_run_manifest_hash") != artifact.get("run_manifest_hash")
        or lineage.get("source_task_corpus_hash") != SOURCE_TASK_CORPUS_HASH
        or lineage.get("source_pre_smoke_seal_hash") != SOURCE_PRE_SMOKE_SEAL_HASH
        or hash_json(lineage.get("source_manifest")) != SOURCE_MANIFEST_HASH
        or lineage.get("parent_files")
        != {
            arm: {
                "artifact_sha256": SOURCE_ARTIFACT_SHA256[arm],
                "checkpoint_sha256": SOURCE_CHECKPOINT_SHA256[arm],
                "terminal_outcomes": SOURCE_TERMINAL_COUNTS[arm],
            }
            for arm in ARMS
        }
        or lineage.get("parent_terminal_outcomes") != SOURCE_PAIRED_EVENT_COUNT
        or lineage.get("newly_authorized_outcomes") != 13
        or lineage.get("accounting")
        != {
            "account_limit_usd": str(ACCOUNT_LIMIT_USD),
            "prior_account_observation_usd": str(PRIOR_ACCOUNT_OBSERVATION_USD),
            "parent_attempt_cost_usd": str(SOURCE_COST_USD),
            "continuation_run_cap_usd": str(CONTINUATION_RUN_CAP_USD),
            "available_continuation_cost_usd": str(CONTINUATION_ALLOWANCE_USD),
        }
    ):
        raise ValueError("continuation artifact lineage is not the authorized AKB-361 amendment")
    manifest = artifact.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("continuation artifact manifest is missing")
    if hash_json(manifest) != artifact.get("run_manifest_hash"):
        raise ValueError("continuation artifact manifest digest does not match")
    source_manifest = deepcopy(lineage["source_manifest"])
    source_budget = source_manifest.get("budget")
    if not isinstance(source_budget, dict):
        raise ValueError("continuation source manifest budget is missing")
    for retired_key in ("max_model_requests", "max_wall_seconds", "max_requests_per_trial"):
        source_budget.pop(retired_key, None)
    if manifest != source_manifest:
        raise ValueError("continuation artifact changes non-budget inputs or the registered $50 cap")
    paired_budget = artifact.get("paired_budget_used")
    if (
        not isinstance(paired_budget, dict)
        or not _finite_equal(paired_budget.get("max_total_cost_usd"), float(ACCOUNT_LIMIT_USD))
        or not isinstance(paired_budget.get("cost_usd"), (int, float))
        or not math.isfinite(float(paired_budget["cost_usd"]))
        or float(paired_budget["cost_usd"]) < float(SOURCE_COST_USD)
        or float(paired_budget["cost_usd"]) > float(CONTINUATION_RUN_CAP_USD)
    ):
        raise ValueError("continuation artifact exceeds its account-adjusted run allowance")
    expected_pending = [
        {"cell": cell, "task_id": task_id, "repeat_index": repeat_index, "arm": arm}
        for cell, task_id, repeat_index, arm in sorted(EXPECTED_MISSING_IDENTITIES)
    ]
    if lineage.get("pending_identities") != expected_pending:
        raise ValueError("continuation artifact changed the exact pending outcome list")
    validate_continuation_runtime_provenance(artifact)
    return SOURCE_MANIFEST_HASH
