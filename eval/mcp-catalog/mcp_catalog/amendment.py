"""One-time, source-bound continuation for the AKB-361 budget amendment."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .checkpoint import (
    CheckpointDocument,
    CheckpointError,
    CheckpointHeader,
    CheckpointKey,
    CheckpointStore,
    SmokeModelClass,
    SmokeTransport,
    TrialCheckpoint,
    checkpoint_key_digest,
    checkpoint_record_hash,
    spent_hash,
    timing_hash,
    valid_completed_outcome,
    valid_smoke_outcome,
)
from .contracts import ArmName, BenchmarkRunManifest, ContractModel, TaskManifest, canonical_json, hash_json
from .execution import TrialOutcome
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
SOURCE_REVISIONS = {
    "baseline": "9daf7b44f9387776f845f181d29f1a7c8d21b37a",  # pragma: allowlist secret
    "candidate": "8d74114042b7d94a02c6b0f7aa27a6ab749e1874",  # pragma: allowlist secret
}
SOURCE_PAIRED_EVENT_COUNT = 595
SOURCE_TERMINAL_COUNTS = {"baseline": 298, "candidate": 297}
SOURCE_COST_USD = Decimal("4.63290984")
PRIOR_ACCOUNT_OBSERVATION_USD = Decimal("7.86955884")
ACCOUNT_LIMIT_USD = Decimal("50.00000000")
CONTINUATION_RUN_CAP_USD = Decimal("46.76335100")
CONTINUATION_ALLOWANCE_USD = Decimal("42.13044116")


class ParentFileBinding(ContractModel):
    artifact_path: Path
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_path: Path
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AmendmentOperator(ContractModel):
    schema_version: Literal[1] = 1
    work_item: Literal["AKB-361"]
    authorization_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    authorization_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parents: dict[ArmName, ParentFileBinding]

    @model_validator(mode="after")
    def validate_scope(self) -> AmendmentOperator:
        if set(self.parents) != {"baseline", "candidate"}:
            raise ValueError("AKB-361 continuation requires exactly one parent pair per arm")
        if self.authorization_revision != AUTHORIZATION_REVISION:
            raise ValueError("AKB-361 continuation authorization revision is not approved")
        if self.authorization_content_hash != AUTHORIZATION_CONTENT_HASH:
            raise ValueError("AKB-361 continuation authorization content hash is not approved")
        return self


TrialIdentity = tuple[str, str, int, ArmName]
ARMS: tuple[ArmName, ArmName] = ("baseline", "candidate")


def _expected_missing_identities() -> set[TrialIdentity]:
    values: set[TrialIdentity] = set()
    arms: tuple[ArmName, ArmName] = ("baseline", "candidate")
    for transport in ("http", "stdio"):
        cell = f"lightweight:{transport}"
        for task_id in ("access-ownership-public-ko", "access-ownership-public-en"):
            for arm in arms:
                values.add((cell, task_id, 2, arm))
    for task_id in ("stdio-image-files-ko", "stdio-image-files-en"):
        for arm in arms:
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
    """Collect only the AKB-361 runtime lease fields whose values can be reallocated."""

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
                    kind = "network_ids" if identity_name == "network_ids" else "volume_names"
                    fields[prefix + ("dependency_identity", "services", service_name, identity_name)] = kind

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
        replacement: Any
        if kind == "pid":
            replacement = 0
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


def _runtime_observation(
    descriptor: RuntimeDescriptor,
    runtime: dict[str, Any],
    *,
    expected_source_revision: str,
    expected_artifact_versions: dict[str, Any],
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    cell_runtimes = runtime.get("cell_runtimes")
    if not isinstance(cell_runtimes, dict) or set(cell_runtimes) != set(descriptor.benchmark_cells):
        raise ValueError("continuation runtime readiness did not cover every benchmark cell")
    if (
        runtime.get("source_revision") != expected_source_revision
        or runtime.get("artifact_versions") != expected_artifact_versions
    ):
        raise ValueError("continuation runtime source or artifact versions differ from the parent")

    cells: dict[str, Any] = {}
    for cell, cell_runtime in sorted(cell_runtimes.items()):
        if not isinstance(cell_runtime, dict):
            raise ValueError(f"continuation runtime readiness evidence is malformed for {cell}")
        discovery = cell_runtime.get("discovery")
        if not isinstance(discovery, dict):
            raise ValueError(f"continuation runtime discovery evidence is missing for {cell}")
        cell_descriptor = descriptor.cell_for(cell)
        if (
            cell_runtime.get("source_revision") != expected_source_revision
            or cell_runtime.get("artifact_versions") != expected_artifact_versions
            or cell_descriptor.source_revision_from(discovery) != expected_source_revision
            or cell_descriptor.artifact_versions_from(discovery) != expected_artifact_versions
        ):
            raise ValueError(f"continuation runtime readiness found source or version drift in {cell}")
        cells[cell] = {
            "readiness_status": "passed",
            "source_revision": expected_source_revision,
            "artifact_versions": expected_artifact_versions,
            "discovery_sha256": hash_json(safe_json(discovery, secrets)),
        }
    return {
        "readiness_status": "passed",
        "source_revision": expected_source_revision,
        "artifact_versions": expected_artifact_versions,
        "cells": cells,
    }


def _build_runtime_provenance(
    parent_inputs: dict[str, Any],
    current_inputs: dict[str, Any],
    current_descriptor_raw: dict[str, Any],
    runtime: dict[str, Any],
    *,
    continuation_manifest_hash: str,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    parent_fixture = parent_inputs.get("fixture")
    current_fixture = current_inputs.get("fixture")
    if not isinstance(parent_fixture, dict) or not isinstance(current_fixture, dict):
        raise ValueError("continuation pre-smoke fixture inputs are missing")
    parent_descriptor = parent_fixture.get("runtime_descriptor")
    current_descriptor = current_fixture.get("runtime_descriptor")
    if not isinstance(parent_descriptor, dict) or not isinstance(current_descriptor, dict):
        raise ValueError("continuation runtime descriptor evidence is missing")

    current_descriptor_safe = safe_json(current_descriptor_raw)
    current_descriptor_safe_with_secrets = safe_json(current_descriptor_raw, secrets)
    if current_descriptor_safe != current_descriptor_safe_with_secrets:
        raise ValueError("continuation runtime descriptor contains a credential value and cannot be preserved raw")
    if current_descriptor_safe != current_descriptor:
        raise ValueError("continuation runtime descriptor does not match the current pre-smoke evidence")
    try:
        parsed_current_descriptor = RuntimeDescriptor.from_dict(current_descriptor_raw)
    except Exception as exc:
        raise ValueError(f"continuation runtime descriptor is invalid: {exc}") from exc

    parent_projection, current_projection, parent_identity, current_identity, changed_paths = (
        _compare_runtime_descriptors(parent_descriptor, current_descriptor)
    )
    expected = deepcopy(parent_inputs)
    observed = deepcopy(current_inputs)
    if observed.get("run_manifest_hash") != continuation_manifest_hash:
        raise ValueError("continuation runtime seal does not carry its registered budget manifest")
    expected["run_manifest_hash"] = "budget-only-amendment"
    observed["run_manifest_hash"] = "budget-only-amendment"
    expected["fixture"]["runtime_descriptor"] = parent_projection
    observed["fixture"]["runtime_descriptor"] = current_projection
    if observed != expected:
        raise ValueError("continuation pre-smoke evidence changes a result-affecting input")

    expected_source_revision = parent_inputs.get("source_revision")
    expected_fixture = parent_inputs["fixture"]
    expected_artifact_versions = expected_fixture.get("runtime_identity")
    if (
        not isinstance(expected_source_revision, str)
        or not isinstance(expected_artifact_versions, dict)
    ):
        raise ValueError("parent source and artifact identities are missing")
    runtime_observation = _runtime_observation(
        parsed_current_descriptor,
        runtime,
        expected_source_revision=expected_source_revision,
        expected_artifact_versions=expected_artifact_versions,
        secrets=secrets,
    )
    descriptor_json = canonical_json(current_descriptor_raw)
    if any(secret and secret in descriptor_json for secret in secrets):
        raise ValueError("continuation runtime descriptor contains a credential value and cannot be preserved raw")
    return {
        "schema_version": 1,
        "parent_descriptor_sha256": hash_json(parent_descriptor),
        "current_descriptor_sha256": hash_json(current_descriptor_raw),
        "current_descriptor": current_descriptor_safe,
        "current_descriptor_json": descriptor_json,
        "parent_semantic_descriptor_sha256": hash_json(parent_projection),
        "current_semantic_descriptor_sha256": hash_json(current_projection),
        "parent_runtime_identity_sha256": hash_json(parent_identity),
        "current_runtime_identity_sha256": hash_json(current_identity),
        "reallocated_identity_paths": changed_paths,
        "current_pre_smoke_inputs_sha256": hash_json(current_inputs),
        "runtime_observation": runtime_observation,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _finite_equal(actual: Any, expected: float) -> bool:
    return isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isclose(
        float(actual), expected, rel_tol=0, abs_tol=1e-9
    )


def _identity(cell: str, task_id: str, repeat_index: int, arm: str) -> TrialIdentity:
    if arm not in {"baseline", "candidate"}:
        raise ValueError("paired evidence has an invalid arm identity")
    if not isinstance(task_id, str) or not isinstance(repeat_index, int) or isinstance(repeat_index, bool):
        raise ValueError("paired evidence has an invalid task identity")
    return (cell, task_id, repeat_index, arm)  # type: ignore[return-value]


@dataclass(slots=True)
class AmendmentPlan:
    """Validated parent evidence and the exact child-run lineage."""

    lineage: dict[str, Any]
    checkpoint_lineage: dict[ArmName, dict[str, Any]]
    source_artifacts: dict[ArmName, dict[str, Any]]
    source_checkpoints: dict[ArmName, CheckpointDocument]
    source_pre_smoke_inputs: dict[ArmName, dict[str, Any]]
    source_events: list[dict[str, Any]]
    terminal_outcomes: dict[ArmName, dict[TrialIdentity, TrialOutcome]]
    missing_identities: frozenset[TrialIdentity]
    source_paths: frozenset[Path]
    manifest: BenchmarkRunManifest
    tasks: list[TaskManifest]
    continuation_runtime_provenance: dict[ArmName, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        operator_path: Path,
        *,
        manifest: BenchmarkRunManifest,
        tasks: list[TaskManifest],
    ) -> AmendmentPlan:
        operator_path = operator_path.resolve(strict=True)
        raw_operator = _read_object(operator_path, label="continuation operator")
        operator = AmendmentOperator.model_validate(raw_operator)
        artifacts: dict[ArmName, dict[str, Any]] = {}
        checkpoints: dict[ArmName, CheckpointDocument] = {}
        canonical_source_paths: set[Path] = set()
        for arm in ARMS:
            binding = operator.parents[arm]
            artifact_path = (operator_path.parent / binding.artifact_path).resolve(strict=True)
            checkpoint_path = (operator_path.parent / binding.checkpoint_path).resolve(strict=True)
            if artifact_path == checkpoint_path or artifact_path in canonical_source_paths or checkpoint_path in canonical_source_paths:
                raise ValueError("continuation parent paths must be four distinct files")
            canonical_source_paths.update((artifact_path, checkpoint_path))
            if binding.artifact_sha256 != SOURCE_ARTIFACT_SHA256[arm] or _sha256(artifact_path) != SOURCE_ARTIFACT_SHA256[arm]:
                raise ValueError(f"{arm} parent artifact is not the authorized AKB-361 source file")
            if binding.checkpoint_sha256 != SOURCE_CHECKPOINT_SHA256[arm] or _sha256(checkpoint_path) != SOURCE_CHECKPOINT_SHA256[arm]:
                raise ValueError(f"{arm} parent checkpoint is not the authorized AKB-361 source file")
            artifacts[arm] = _read_object(artifact_path, label=f"{arm} parent artifact")
            checkpoint_raw = _read_object(checkpoint_path, label=f"{arm} parent checkpoint")
            try:
                checkpoints[arm] = CheckpointDocument.model_validate(checkpoint_raw)
            except Exception as exc:
                raise ValueError(f"{arm} parent checkpoint contract validation failed: {exc}") from exc

        plan = cls._validate_sources(operator, artifacts, checkpoints, manifest, tasks)
        plan.source_paths = frozenset(canonical_source_paths)
        return plan

    @classmethod
    def _validate_sources(
        cls,
        operator: AmendmentOperator,
        artifacts: dict[ArmName, dict[str, Any]],
        checkpoints: dict[ArmName, CheckpointDocument],
        manifest: BenchmarkRunManifest,
        tasks: list[TaskManifest],
    ) -> AmendmentPlan:
        from .runner import _artifact_integrity_error, planned_arm_order

        task_hash = hash_json([task.model_dump(mode="json") for task in tasks])
        if task_hash != SOURCE_TASK_CORPUS_HASH:
            raise ValueError("current task corpus is not the authorized AKB-361 parent corpus")
        old_manifest = artifacts["baseline"].get("manifest")
        if not isinstance(old_manifest, dict) or hash_json(old_manifest) != SOURCE_MANIFEST_HASH:
            raise ValueError("parent run manifest is not the authorized AKB-361 manifest")
        if artifacts["candidate"].get("manifest") != old_manifest:
            raise ValueError("parent arm manifests differ")
        if artifacts["candidate"].get("provider_registry") != artifacts["baseline"].get("provider_registry"):
            raise ValueError("parent provider registry snapshot differs between arms")
        if old_manifest.get("budget") != {
            "max_model_requests": 3000,
            "max_total_cost_usd": 50.0,
            "max_wall_seconds": 10800,
            "request_timeout_seconds": 300,
            "max_requests_per_trial": 24,
            "max_cost_per_trial_usd": 0.1,
        }:
            raise ValueError("parent budget does not match the authorized capped attempt")
        expected_manifest = deepcopy(old_manifest)
        budget = expected_manifest["budget"]
        for retired_key in ("max_model_requests", "max_wall_seconds", "max_requests_per_trial"):
            budget.pop(retired_key)
        current_manifest = manifest.model_dump(mode="json")
        if current_manifest != expected_manifest:
            raise ValueError("continuation manifest changes inputs beyond the approved budget amendment")
        new_manifest_hash = hash_json(current_manifest)

        if [task.model_dump(mode="json") for task in tasks] != artifacts["baseline"].get("task_contracts"):
            raise ValueError("current task contracts differ from the parent artifact")

        all_outcomes: dict[ArmName, dict[TrialIdentity, TrialOutcome]] = {
            "baseline": {},
            "candidate": {},
        }
        tasks_by_id = {task.id: task for task in tasks}
        for arm in ARMS:
            artifact = artifacts[arm]
            document = checkpoints[arm]
            integrity_error = _artifact_integrity_error(artifact, arm)
            if integrity_error is not None:
                raise ValueError(f"{arm} parent artifact integrity check failed: {integrity_error}")
            if (
                artifact.get("status") != "incomplete"
                or artifact.get("run_manifest_hash") != SOURCE_MANIFEST_HASH
                or artifact.get("task_corpus_hash") != task_hash
                or artifact.get("source_revision") != SOURCE_REVISIONS[arm]
                or artifact.get("pre_smoke_seal_hash") != SOURCE_PRE_SMOKE_SEAL_HASH
            ):
                raise ValueError(f"{arm} parent artifact identity does not match the authorized attempt")
            if document.lifecycle != "finalized" or document.smoke_status != "passed":
                raise ValueError(f"{arm} source checkpoint is not finalized with a passed smoke gate")
            if (
                document.header.arm != arm
                or document.header.source_revision != SOURCE_REVISIONS[arm]
                or document.header.run_manifest_hash != SOURCE_MANIFEST_HASH
                or document.header.task_corpus_hash != task_hash
                or document.header.pre_smoke_seal_hash != SOURCE_PRE_SMOKE_SEAL_HASH
            ):
                raise ValueError(f"{arm} parent checkpoint header differs from the authorized source")
            if document.spent_hash != spent_hash(document.spent) or document.timing_hash != timing_hash(document.timing):
                raise ValueError(f"{arm} parent checkpoint accounting digest is invalid")
            if document.reserved_cost_usd != 0 or len(document.records) != SOURCE_TERMINAL_COUNTS[arm]:
                raise ValueError(f"{arm} parent checkpoint terminal record count is invalid")
            for digest, record in document.records.items():
                if (
                    digest != checkpoint_key_digest(record.key)
                    or record.key.arm != arm
                    or record.key.run_manifest_hash != SOURCE_MANIFEST_HASH
                    or record.key.task_corpus_hash != task_hash
                    or record.key.source_revision != SOURCE_REVISIONS[arm]
                    or not CheckpointStore._outcome_matches_key(record.outcome, record.key)
                ):
                    raise ValueError(f"{arm} parent checkpoint contains a mismatched trial identity")
            if not all(record.status in {"completed", "failed"} for record in document.records.values()):
                raise ValueError(f"{arm} parent checkpoint contains a nonterminal record")
            cls._validate_parent_artifact_records(arm, artifact, document, all_outcomes[arm], tasks_by_id)
            cls._validate_parent_smoke(arm, artifact, document, tasks_by_id)
            seal_inputs = artifact.get("pre_smoke_seal_inputs")
            if not isinstance(seal_inputs, dict):
                raise ValueError(f"{arm} parent pre-smoke seal inputs are missing")
            if (
                seal_inputs.get("arm") != arm
                or seal_inputs.get("run_manifest_hash") != SOURCE_MANIFEST_HASH
                or seal_inputs.get("task_corpus_hash") != task_hash
                or seal_inputs.get("catalogs") != artifact.get("catalogs")
                or seal_inputs.get("provider_registry_hash") != artifact.get("provider_registry", {}).get("snapshot_hash")
            ):
                raise ValueError(f"{arm} parent pre-smoke inputs are detached from its evidence")
        expected_old_seal = hash_json(
            {
                arm: hash_json(artifacts[arm]["pre_smoke_seal_inputs"])
                for arm in ARMS
            }
        )
        if expected_old_seal != SOURCE_PRE_SMOKE_SEAL_HASH:
            raise ValueError("parent pre-smoke seal does not hash to the authorized source seal")

        paired_budget = artifacts["baseline"].get("paired_budget_used")
        if not isinstance(paired_budget, dict) or paired_budget != artifacts["candidate"].get("paired_budget_used"):
            raise ValueError("parent paired ledger is missing or differs between arms")
        if (
            paired_budget.get("model_requests") != 3000
            or paired_budget.get("max_model_requests") != 3000
            or paired_budget.get("max_wall_seconds") != 10800
            or paired_budget.get("max_total_cost_usd") != 50.0
            or not _finite_equal(paired_budget.get("cost_usd"), float(SOURCE_COST_USD))
            or not _finite_equal(paired_budget.get("wall_seconds"), 6379.774386665958)
            or paired_budget["wall_seconds"] >= 10800
        ):
            raise ValueError("parent ledger does not show the approved request-cap-only interruption")
        for arm in ARMS:
            document = checkpoints[arm]
            outcomes = [record.outcome for record in document.records.values()]
            outcomes.extend(record.outcome for record in document.smoke_gate.values())
            expected_requests = sum(item.model_requests for item in outcomes) + document.spent.provider_setup_requests
            expected_input_tokens = sum(item.input_tokens for item in outcomes)
            expected_output_tokens = sum(item.output_tokens for item in outcomes)
            expected_cost = sum((Decimal(str(item.cost_usd)) for item in outcomes), Decimal("0"))
            expected_model_work = sum(item.latency_seconds for item in outcomes)
            if (
                document.spent.model_requests != expected_requests
                or document.spent.input_tokens != expected_input_tokens
                or document.spent.output_tokens != expected_output_tokens
                or not _finite_equal(document.spent.cost_usd, float(expected_cost))
                or not math.isclose(document.spent.model_work_seconds, expected_model_work, abs_tol=1e-8)
            ):
                raise ValueError(f"{arm} parent checkpoint usage does not reconcile with its records")
        parent_request_count = sum(checkpoints[arm].spent.model_requests for arm in ARMS)
        if parent_request_count != 3000:
            raise ValueError("parent checkpoint request totals do not match the paired ledger")
        parent_setup_count = sum(checkpoints[arm].spent.provider_setup_requests for arm in ARMS)
        if parent_setup_count != 3:
            raise ValueError("parent provider setup request totals do not match the paired ledger")
        if Decimal(str(paired_budget["cost_usd"])) != sum(
            (Decimal(str(checkpoints[arm].spent.cost_usd)) for arm in ARMS),
            Decimal("0"),
        ):
            raise ValueError("parent arm costs do not reconcile with the paired ledger")

        paired_execution = artifacts["baseline"].get("paired_execution")
        if not isinstance(paired_execution, dict) or paired_execution != artifacts["candidate"].get("paired_execution"):
            raise ValueError("parent paired execution evidence is missing or differs between arms")
        events = paired_execution.get("events")
        if (
            paired_execution.get("mode") != "counterbalanced_task_repeat"
            or paired_execution.get("seed") != manifest.paired_order_seed
            or paired_execution.get("complete") is not False
            or paired_execution.get("reused") != []
            or not isinstance(events, list)
            or len(events) != SOURCE_PAIRED_EVENT_COUNT
            or [event.get("sequence") for event in events if isinstance(event, dict)]
            != list(range(1, SOURCE_PAIRED_EVENT_COUNT + 1))
        ):
            raise ValueError("parent paired event sequence is not the exact 595-event prefix")
        source_events = [deepcopy(event) for event in events]
        recorded_identities = set(all_outcomes["baseline"]) | set(all_outcomes["candidate"])
        event_identities: set[TrialIdentity] = set()
        events_by_cell: dict[str, list[TrialIdentity]] = {}
        for event in source_events:
            event_identity = _identity(
                event.get("cell"), event.get("task_id"), event.get("repeat_index"), event.get("arm")
            )
            if event_identity in event_identities:
                raise ValueError("parent paired events contain a duplicate trial identity")
            expected_order = planned_arm_order(event_identity[1], event_identity[2], manifest.paired_order_seed)
            if event.get("order_position") != expected_order.index(event_identity[3]):
                raise ValueError("parent paired event order position differs from preregistration")
            source_outcome = all_outcomes[event_identity[3]].get(event_identity)
            if source_outcome is None or source_outcome.paired_execution_sequence != event["sequence"]:
                raise ValueError("parent paired event does not match its checkpoint outcome")
            event_identities.add(event_identity)
            events_by_cell.setdefault(event_identity[0], []).append(event_identity)
        if event_identities != recorded_identities:
            raise ValueError("parent paired events do not cover every terminal checkpoint outcome")

        expected_schedule: dict[str, list[TrialIdentity]] = {}
        for model in manifest.models:
            for transport in manifest.transports:
                cell = f"{model.class_name}:{transport}"
                expected_schedule[cell] = [
                    (cell, task.id, repeat_index, arm)
                    for repeat_index in range(1, manifest.repeats + 1)
                    for task in tasks
                    if transport in task.fixture.transports
                    for arm in planned_arm_order(task.id, repeat_index, manifest.paired_order_seed)
                ]
        missing: set[TrialIdentity] = set()
        for cell, schedule in expected_schedule.items():
            observed = events_by_cell.get(cell, [])
            if observed != schedule[: len(observed)]:
                raise ValueError(f"parent records are not a paired schedule prefix for {cell}")
            missing.update(schedule[len(observed) :])
        if missing != EXPECTED_MISSING_IDENTITIES or len(missing) != 13:
            raise ValueError("parent schedule does not leave exactly the authorized 13 outcomes")

        parent_hashes = {
            arm: {
                "artifact_sha256": SOURCE_ARTIFACT_SHA256[arm],
                "checkpoint_sha256": SOURCE_CHECKPOINT_SHA256[arm],
                "terminal_outcomes": len(all_outcomes[arm]),
            }
            for arm in ARMS
        }
        lineage: dict[str, Any] = {
            "schema_version": 1,
            "work_item": WORK_ITEM,
            "authorization_revision": AUTHORIZATION_REVISION,
            "authorization_content_hash": AUTHORIZATION_CONTENT_HASH,
            "source_run_manifest_hash": SOURCE_MANIFEST_HASH,
            "continuation_run_manifest_hash": new_manifest_hash,
            "source_task_corpus_hash": SOURCE_TASK_CORPUS_HASH,
            "source_pre_smoke_seal_hash": SOURCE_PRE_SMOKE_SEAL_HASH,
            "source_manifest": deepcopy(old_manifest),
            "parent_files": parent_hashes,
            "parent_terminal_outcomes": SOURCE_PAIRED_EVENT_COUNT,
            "newly_authorized_outcomes": len(missing),
            "pending_identities": [
                {"cell": cell, "task_id": task_id, "repeat_index": repeat_index, "arm": arm}
                for cell, task_id, repeat_index, arm in sorted(missing)
            ],
            "accounting": {
                "account_limit_usd": str(ACCOUNT_LIMIT_USD),
                "prior_account_observation_usd": str(PRIOR_ACCOUNT_OBSERVATION_USD),
                "parent_attempt_cost_usd": str(SOURCE_COST_USD),
                "continuation_run_cap_usd": str(CONTINUATION_RUN_CAP_USD),
                "available_continuation_cost_usd": str(CONTINUATION_ALLOWANCE_USD),
            },
        }
        checkpoint_lineage: dict[ArmName, dict[str, Any]] = {
            arm: {
                **lineage,
                "checkpoint_arm": arm,
                "parent_artifact_sha256": SOURCE_ARTIFACT_SHA256[arm],
                "parent_checkpoint_sha256": SOURCE_CHECKPOINT_SHA256[arm],
                "parent_terminal_outcomes": len(all_outcomes[arm]),
            }
            for arm in ARMS
        }
        return cls(
            lineage=lineage,
            checkpoint_lineage=checkpoint_lineage,
            source_artifacts=artifacts,
            source_checkpoints=checkpoints,
            source_pre_smoke_inputs={
                arm: deepcopy(artifacts[arm]["pre_smoke_seal_inputs"])
                for arm in ARMS
            },
            source_events=source_events,
            terminal_outcomes=all_outcomes,
            missing_identities=frozenset(missing),
            source_paths=frozenset(),
            manifest=manifest,
            tasks=tasks,
        )

    @staticmethod
    def _validate_parent_artifact_records(
        arm: ArmName,
        artifact: dict[str, Any],
        document: CheckpointDocument,
        outcomes: dict[TrialIdentity, TrialOutcome],
        tasks: dict[str, TaskManifest],
    ) -> None:
        if not isinstance(artifact.get("runs"), dict):
            raise ValueError(f"{arm} parent artifact does not include run records")
        for cell, run in artifact["runs"].items():
            if not isinstance(run, dict) or not isinstance(run.get("trials"), list) or not isinstance(run.get("statuses"), list):
                raise ValueError(f"{arm} parent artifact run records are malformed")
            statuses = {
                (item.get("task_id"), item.get("repeat_index")): item.get("status")
                for item in run["statuses"]
                if isinstance(item, dict)
            }
            if len(statuses) != len(run["statuses"]):
                raise ValueError(f"{arm} parent artifact statuses are duplicated or malformed")
            for raw_outcome in run["trials"]:
                outcome = TrialOutcome.model_validate(raw_outcome)
                task = tasks.get(outcome.task_id)
                if task is None or task.locale != outcome.locale:
                    raise ValueError(f"{arm} parent artifact outcome references an unregistered task")
                identity = _identity(cell, outcome.task_id, outcome.repeat_index, arm)
                if identity in outcomes:
                    raise ValueError(f"{arm} parent artifact contains a duplicate outcome")
                record = document.records.get(
                    checkpoint_key_digest(
                        CheckpointKey(
                            source_revision=document.header.source_revision,
                            run_manifest_hash=document.header.run_manifest_hash,
                            task_corpus_hash=document.header.task_corpus_hash,
                            arm=arm,
                            model_class=outcome.model_class,  # type: ignore[arg-type]
                            model_id=outcome.model_id,
                            transport=outcome.transport,  # type: ignore[arg-type]
                            task_id=outcome.task_id,
                            repeat_index=outcome.repeat_index,
                            locale=outcome.locale,
                        )
                    )
                )
                if record is None or record.status != statuses.get((outcome.task_id, outcome.repeat_index)):
                    raise ValueError(f"{arm} parent artifact status differs from its checkpoint")
                if record.record_hash != checkpoint_record_hash(record.status, record.key, record.outcome):
                    raise ValueError(f"{arm} parent checkpoint record digest does not match")
                if (
                    record.status == "completed"
                    and not valid_completed_outcome(record.outcome, task)
                    and record.outcome.failure_kind != "request_limit"
                ):
                    raise ValueError(f"{arm} parent checkpoint contains an invalid completed trial")
                if outcome.model_dump(mode="json") != record.outcome.model_dump(mode="json"):
                    raise ValueError(f"{arm} parent artifact outcome differs from its checkpoint")
                outcomes[identity] = outcome
        if len(outcomes) != SOURCE_TERMINAL_COUNTS[arm]:
            raise ValueError(f"{arm} parent artifact outcome count is invalid")

    @staticmethod
    def _validate_parent_smoke(
        arm: ArmName,
        artifact: dict[str, Any],
        document: CheckpointDocument,
        tasks: dict[str, TaskManifest],
    ) -> None:
        smoke = artifact.get("smoke_gate")
        if not isinstance(smoke, dict) or smoke.get("status") != "passed":
            raise ValueError(f"{arm} parent artifact smoke gate is not passed")
        artifact_cells = {item.get("cell"): item for item in smoke.get("cells", []) if isinstance(item, dict)}
        if set(artifact_cells) != set(document.smoke_gate) or document.smoke_status != "passed":
            raise ValueError(f"{arm} parent smoke cells do not match its checkpoint")
        for cell, record in document.smoke_gate.items():
            task = tasks.get(record.outcome.task_id)
            if task is None or record.status != "completed" or not valid_smoke_outcome(record.outcome, task):
                raise ValueError(f"{arm} parent smoke cell {cell} is not reusable")
            from .checkpoint import smoke_record_hash

            if record.record_hash != smoke_record_hash(
                record.status,
                record.model_class,
                record.model_id,
                record.transport,
                record.outcome,
            ):
                raise ValueError(f"{arm} parent smoke record digest does not match")
            item = artifact_cells[cell]
            if (
                item.get("status") != "completed"
                or item.get("outcome") != record.outcome.model_dump(mode="json")
            ):
                raise ValueError(f"{arm} parent smoke artifact differs from its checkpoint")

    def validate_pre_smoke_inputs(
        self,
        arm: ArmName,
        current: dict[str, Any],
        *,
        current_descriptor_raw: dict[str, Any],
        runtime: dict[str, Any],
        secrets: tuple[str, ...] = (),
    ) -> None:
        """Keep the parent seal and bind a semantically identical fresh runtime lease."""

        try:
            self.continuation_runtime_provenance[arm] = _build_runtime_provenance(
                self.source_pre_smoke_inputs[arm],
                current,
                current_descriptor_raw,
                runtime,
                continuation_manifest_hash=self.lineage["continuation_run_manifest_hash"],
                secrets=secrets,
            )
        except ValueError as exc:
            raise ValueError(f"{arm} continuation pre-smoke evidence is invalid: {exc}") from exc

    def runtime_provenance_for(self, arm: ArmName) -> dict[str, Any] | None:
        provenance = self.continuation_runtime_provenance.get(arm)
        return deepcopy(provenance) if provenance is not None else None

    def parent_provider_registry(self) -> dict[str, Any]:
        return self.source_artifacts["baseline"]["provider_registry"]

    def parent_smoke_gate(self, arm: ArmName) -> dict[str, Any]:
        return self.source_artifacts[arm]["smoke_gate"]

    def parent_pre_smoke_inputs(self, arm: ArmName) -> dict[str, Any]:
        return deepcopy(self.source_pre_smoke_inputs[arm])

    def parent_trial_count(self, arm: ArmName) -> int:
        return SOURCE_TERMINAL_COUNTS[arm]

    def seed_child_checkpoints(self, paths: dict[ArmName, Path], *, resume: bool) -> None:
        if set(paths) != {"baseline", "candidate"}:
            raise ValueError("AKB-361 continuation requires two child checkpoint paths")
        resolved_paths = {arm: path.resolve(strict=False) for arm, path in paths.items()}
        if resolved_paths["baseline"] == resolved_paths["candidate"]:
            raise ValueError("continuation arms require independent child checkpoint paths")
        if self.source_paths.intersection(resolved_paths.values()):
            raise ValueError("continuation child checkpoint paths must not overwrite parent evidence")
        if not resume and any(path.exists() for path in resolved_paths.values()):
            raise CheckpointError("continuation child checkpoint already exists; use --resume")
        if resume and any(not path.is_file() for path in resolved_paths.values()):
            raise CheckpointError("--resume requires both continuation child checkpoints")

        for arm in ARMS:
            path = resolved_paths[arm]
            if resume:
                self._validate_child_checkpoint(path, arm)
            else:
                self._seed_child_checkpoint(path, arm)

    def _planned_keys(self, arm: ArmName) -> dict[str, CheckpointKey]:
        manifest_hash = self.lineage["continuation_run_manifest_hash"]
        task_hash = SOURCE_TASK_CORPUS_HASH
        source_revision = SOURCE_REVISIONS[arm]
        keys: dict[str, CheckpointKey] = {}
        for model in self.manifest.models:
            for transport in self.manifest.transports:
                for task in self.tasks:
                    if transport not in task.fixture.transports:
                        continue
                    for repeat_index in range(1, self.manifest.repeats + 1):
                        key = CheckpointKey(
                            source_revision=source_revision,
                            run_manifest_hash=manifest_hash,
                            task_corpus_hash=task_hash,
                            arm=arm,
                            model_class=model.class_name,
                            model_id=model.model_id,
                            transport=transport,
                            task_id=task.id,
                            repeat_index=repeat_index,
                            locale=task.locale,
                        )
                        keys[checkpoint_key_digest(key)] = key
        return keys

    def _expected_smoke(self) -> dict[str, tuple[SmokeModelClass, str, SmokeTransport]]:
        return {
            f"{model.class_name}:{transport}": (model.class_name, model.model_id, transport)
            for model in self.manifest.models
            for transport in self.manifest.transports
        }

    def _seed_child_checkpoint(self, path: Path, arm: ArmName) -> None:
        source = self.source_checkpoints[arm]
        current_hash = self.lineage["continuation_run_manifest_hash"]
        header = CheckpointHeader(
            source_revision=SOURCE_REVISIONS[arm],
            run_manifest_hash=current_hash,
            task_corpus_hash=SOURCE_TASK_CORPUS_HASH,
            arm=arm,
            pre_smoke_seal_hash=SOURCE_PRE_SMOKE_SEAL_HASH,
        )
        records: dict[str, TrialCheckpoint] = {}
        for source_record in source.records.values():
            key = source_record.key.model_copy(update={"run_manifest_hash": current_hash})
            copied = TrialCheckpoint(
                status=source_record.status,
                key=key,
                outcome=source_record.outcome.model_copy(deep=True),
                record_hash=checkpoint_record_hash(source_record.status, key, source_record.outcome),
            )
            records[checkpoint_key_digest(key)] = copied
        child = CheckpointDocument(
            lifecycle="open",
            header=header,
            spent=source.spent.model_copy(deep=True),
            reserved_cost_usd=0,
            reused_trial_count=0,
            rerun_trial_count=0,
            spent_hash=source.spent_hash,
            timing=source.timing.model_copy(deep=True),
            timing_hash=source.timing_hash,
            records=records,
            smoke_status=source.smoke_status,
            smoke_gate={key: item.model_copy(deep=True) for key, item in source.smoke_gate.items()},
            continuation=self.checkpoint_lineage[arm],
        )
        store = CheckpointStore(
            path,
            header=header.model_copy(update={"pre_smoke_seal_hash": None}),
            expected_keys=self._planned_keys(arm),
            expected_tasks={task.id: task for task in self.tasks},
            expected_smoke_cells=self._expected_smoke(),
            continuation=self.checkpoint_lineage[arm],
        )
        store.seed_continuation(child)

    def _validate_child_checkpoint(self, path: Path, arm: ArmName) -> None:
        raw = _read_object(path, label=f"{arm} continuation checkpoint")
        try:
            child = CheckpointDocument.model_validate(raw)
        except Exception as exc:
            raise CheckpointError(f"{arm} continuation checkpoint contract validation failed: {exc}") from exc
        source = self.source_checkpoints[arm]
        expected_header = CheckpointHeader(
            source_revision=SOURCE_REVISIONS[arm],
            run_manifest_hash=self.lineage["continuation_run_manifest_hash"],
            task_corpus_hash=SOURCE_TASK_CORPUS_HASH,
            arm=arm,
            pre_smoke_seal_hash=SOURCE_PRE_SMOKE_SEAL_HASH,
        )
        if child.header != expected_header or child.continuation != self.checkpoint_lineage[arm]:
            raise CheckpointError(f"{arm} continuation checkpoint lineage or input header does not match")
        if child.lifecycle == "closing" or child.reserved_cost_usd != 0:
            raise CheckpointError(f"{arm} continuation checkpoint is not safely resumable")
        if child.spent_hash != spent_hash(child.spent) or child.timing_hash != timing_hash(child.timing):
            raise CheckpointError(f"{arm} continuation checkpoint accounting digest does not match")
        planned = self._planned_keys(arm)
        source_keys: dict[TrialIdentity, TrialCheckpoint] = {}
        for parent in source.records.values():
            identity = (f"{parent.key.model_class}:{parent.key.transport}", parent.key.task_id, parent.key.repeat_index, arm)
            source_keys[identity] = parent
            current_key = parent.key.model_copy(update={"run_manifest_hash": self.lineage["continuation_run_manifest_hash"]})
            copied = child.records.get(checkpoint_key_digest(current_key))
            if copied is None or copied.status != parent.status or copied.outcome != parent.outcome:
                raise CheckpointError(f"{arm} continuation changed an immutable parent outcome")
        all_identities: set[TrialIdentity] = set()
        new_outcomes: list[TrialOutcome] = []
        extra_events: list[dict[str, Any]] = []
        for digest, record in child.records.items():
            if digest != checkpoint_key_digest(record.key) or digest not in planned:
                raise CheckpointError(f"{arm} continuation contains an unplanned child record")
            if record.record_hash != checkpoint_record_hash(record.status, record.key, record.outcome):
                raise CheckpointError(f"{arm} continuation record digest does not match")
            identity = (f"{record.key.model_class}:{record.key.transport}", record.key.task_id, record.key.repeat_index, arm)
            if identity in all_identities:
                raise CheckpointError(f"{arm} continuation contains a duplicate outcome")
            all_identities.add(identity)
            if identity not in source_keys:
                if identity not in self.missing_identities:
                    raise CheckpointError(f"{arm} continuation added an unauthorized trial identity")
                sequence = record.outcome.paired_execution_sequence
                if sequence is None or sequence <= SOURCE_PAIRED_EVENT_COUNT:
                    raise CheckpointError(f"{arm} appended outcome is missing its continuation sequence")
                new_outcomes.append(record.outcome)
                extra_events.append(
                    {
                        "sequence": sequence,
                        "cell": identity[0],
                        "task_id": identity[1],
                        "repeat_index": identity[2],
                        "arm": arm,
                        "order_position": record.outcome.paired_order_position,
                    }
                )
        if not source_keys.keys() <= all_identities:
            raise CheckpointError(f"{arm} continuation lost parent outcomes")
        if child.smoke_status != "passed" or child.smoke_gate != source.smoke_gate:
            raise CheckpointError(f"{arm} continuation changed immutable parent smoke evidence")
        expected_spent = source.spent.model_copy(deep=True)
        for outcome in new_outcomes:
            expected_spent = expected_spent.plus(outcome)
        expected_spent.wall_seconds = child.spent.wall_seconds
        if (
            child.spent.model_requests != expected_spent.model_requests
            or child.spent.provider_setup_requests != source.spent.provider_setup_requests
            or child.spent.input_tokens != expected_spent.input_tokens
            or child.spent.output_tokens != expected_spent.output_tokens
            or Decimal(str(child.spent.cost_usd)) != Decimal(str(expected_spent.cost_usd))
            or not math.isclose(child.spent.model_work_seconds, expected_spent.model_work_seconds, abs_tol=1e-8)
            or child.spent.wall_seconds < source.spent.wall_seconds
        ):
            raise CheckpointError(f"{arm} continuation ledger does not preserve and extend parent usage")
        if extra_events:
            self._validate_child_event_suffix(extra_events)

    def _validate_child_event_suffix(self, extra_events: list[dict[str, Any]]) -> None:
        events = [*self.source_events, *extra_events]
        events.sort(key=lambda item: item["sequence"])
        if [event["sequence"] for event in events] != list(range(1, len(events) + 1)):
            raise CheckpointError("continuation event sequence has a gap or duplicate")
        from .runner import planned_arm_order

        schedule: dict[str, list[TrialIdentity]] = {}
        for model in self.manifest.models:
            for transport in self.manifest.transports:
                cell = f"{model.class_name}:{transport}"
                schedule[cell] = [
                    (cell, task.id, repeat_index, arm)
                    for repeat_index in range(1, self.manifest.repeats + 1)
                    for task in self.tasks
                    if transport in task.fixture.transports
                    for arm in planned_arm_order(task.id, repeat_index, self.manifest.paired_order_seed)
                ]
        by_cell: dict[str, list[TrialIdentity]] = {}
        for event in events:
            identity = _identity(event["cell"], event["task_id"], event["repeat_index"], event["arm"])
            expected = planned_arm_order(identity[1], identity[2], self.manifest.paired_order_seed)
            if event["order_position"] != expected.index(identity[3]):
                raise CheckpointError("continuation event violates its registered arm order")
            by_cell.setdefault(identity[0], []).append(identity)
        for cell, observed in by_cell.items():
            if observed != schedule[cell][: len(observed)]:
                raise CheckpointError(f"continuation event history is not a schedule prefix for {cell}")

    def history_events_for_resume(self, paths: dict[ArmName, Path]) -> list[dict[str, Any]]:
        extras: list[dict[str, Any]] = []
        pending_by_arm = {
            arm: {
                (cell, task_id, repeat_index)
                for cell, task_id, repeat_index, pending_arm in self.missing_identities
                if pending_arm == arm
            }
            for arm in ARMS
        }
        for arm, path in paths.items():
            document = CheckpointDocument.model_validate(_read_object(path, label=f"{arm} continuation checkpoint"))
            for record in document.records.values():
                identity = (
                    f"{record.key.model_class}:{record.key.transport}",
                    record.key.task_id,
                    record.key.repeat_index,
                )
                if identity in pending_by_arm[arm]:
                    sequence = record.outcome.paired_execution_sequence
                    if sequence is None or sequence <= SOURCE_PAIRED_EVENT_COUNT:
                        raise CheckpointError("continuation checkpoint outcome has no appended paired event")
                    extras.append(
                        {
                            "sequence": sequence,
                            "cell": f"{record.key.model_class}:{record.key.transport}",
                            "task_id": record.key.task_id,
                            "repeat_index": record.key.repeat_index,
                            "arm": arm,
                            "order_position": record.outcome.paired_order_position,
                        }
                    )
        self._validate_child_event_suffix(extras)
        return [*self.source_events, *sorted(extras, key=lambda item: item["sequence"])]


def validate_continuation_runtime_provenance(artifact: dict[str, Any]) -> None:
    """Recompute the parent-to-child runtime boundary from the sealed artifacts."""

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
    if set(RuntimeDescriptor.from_dict(current_descriptor_raw).benchmark_cells) != expected_cells:
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
    descriptor = RuntimeDescriptor.from_dict(current_descriptor_raw)
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
    """Validate the one authorized output lineage and return its source seal manifest hash."""

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
        or lineage.get("parent_files") != {
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
