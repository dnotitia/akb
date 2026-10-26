"""Source-bound selection for the AKB-361 provider-failure remeasurement."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, model_validator

from .contracts import ArmName, BenchmarkRunManifest, ContractModel, TaskManifest, Transport, hash_json
from .evidence import validate_model_input_evidence
from .execution import TrialOutcome, has_measured_evidence, has_server_instructions_evidence
from .pairing import planned_arm_order

SelectionIdentity = tuple[ArmName, str, str, str, int]
ParentLineage = Literal["resume04"]
ParentName = Literal[
    "baseline_artifact",
    "baseline_checkpoint",
    "candidate_artifact",
    "candidate_checkpoint",
]

TOTAL_BUDGET_USD = Decimal("50")
PRIOR_SPEND_USD = Decimal("11.1961127352")
NEW_EXECUTION_LIMIT_USD = TOTAL_BUDGET_USD - PRIOR_SPEND_USD
PER_TRIAL_LIMIT_USD = Decimal("0.10")
REQUEST_TIMEOUT_SECONDS = 300
PAIRED_ORDER_MODE = "counterbalanced_task_repeat"
PAIRED_ORDER_SEED = "akb-358-v1"
ARM_NAMES: tuple[ArmName, ArmName] = ("baseline", "candidate")
APPROVED_AUTHORIZATION = {
    "uri": "akb://reef-akb/coll/issues/doc/akb-361.md",
    "revision": "c7ffee0f74e3f85d1ce8502f343c16d51a6bb83d",  # pragma: allowlist secret
    "content_sha256": "b1bed7a8febaad288255a55ad0caaacc7fe55aa1d114d7319a47740f0dda9b7f",  # pragma: allowlist secret
}
APPROVED_PARENT_SHA256: dict[ParentName, str] = {
    "baseline_artifact": "aee24ffd43b29636cb1ea50994fef068183b5ce6013a10f30cc29980e87d9a0d",  # pragma: allowlist secret
    "baseline_checkpoint": "87a78c5929685e7586b567648c40ac1d159d40e1928fff611626fda630b9a798",  # pragma: allowlist secret
    "candidate_artifact": "b4f22694020812e2cdd99058fd3153958a5cce491fb830f6011baacb6c214987",  # pragma: allowlist secret
    "candidate_checkpoint": "ded15e8564780025738acd4bbd60a3f3b389f9febfe228c02d7951a48a88d623",  # pragma: allowlist secret
}
SOURCE_REVISIONS = {
    "baseline": "9daf7b44f9387776f845f181d29f1a7c8d21b37a",  # pragma: allowlist secret
    "candidate": "c353e91ca3a3277c37986c635ed593cdf119ba85",  # pragma: allowlist secret
}

# arm, model class, transport, task, repeat, original failure kind
APPROVED_FAILURES: tuple[tuple[ArmName, str, Transport, str, int, str], ...] = (
    ("baseline", "lightweight", "http", "authorization-readonly-ko", 2, "provider"),
    ("baseline", "lightweight", "http", "create-vault-ko", 1, "request_timeout"),
    ("baseline", "lightweight", "http", "history-provenance-en", 1, "interrupted"),
    ("baseline", "lightweight", "http", "invalid-recovery-en", 2, "unknown"),
    ("baseline", "lightweight", "http", "overlapping-document-en", 2, "unknown"),
    ("baseline", "lightweight", "http", "overlapping-document-ko", 2, "provider"),
    ("baseline", "lightweight", "stdio", "history-provenance-en", 1, "provider"),
    ("baseline", "lightweight", "stdio", "import-export-en", 2, "provider"),
    ("baseline", "lightweight", "stdio", "import-export-ko", 2, "provider"),
    ("baseline", "lightweight", "stdio", "table-schema-conflict-en", 1, "provider"),
    ("baseline", "primary", "http", "collection-relation-lifecycle-en", 1, "unknown"),
    ("baseline", "primary", "http", "collection-relation-lifecycle-ko", 1, "unknown"),
    ("baseline", "primary", "http", "knowledge-workflow-ko", 1, "request_timeout"),
    ("candidate", "lightweight", "http", "overlapping-document-en", 2, "provider"),
    ("candidate", "lightweight", "http", "overlapping-document-ko", 1, "provider"),
    ("candidate", "lightweight", "http", "table-publication-ko", 1, "provider"),
    ("candidate", "lightweight", "stdio", "access-ownership-public-en", 2, "provider"),
    ("candidate", "lightweight", "stdio", "access-ownership-public-ko", 1, "provider"),
    ("candidate", "lightweight", "stdio", "create-vault-ko", 1, "request_timeout"),
    ("candidate", "lightweight", "stdio", "import-export-en", 2, "provider"),
    ("candidate", "primary", "http", "knowledge-workflow-ko", 1, "unknown"),
    ("candidate", "primary", "stdio", "history-provenance-en", 1, "interrupted"),
    ("candidate", "primary", "stdio", "knowledge-workflow-ko", 1, "request_timeout"),
)

_FAILURE_KIND_BY_IDENTITY: dict[SelectionIdentity, str] = {
    (arm, model_class, transport, task_id, repeat_index): failure_kind
    for arm, model_class, transport, task_id, repeat_index, failure_kind in APPROVED_FAILURES
}


class AuthorizationPin(ContractModel):
    uri: str = Field(pattern=r"^akb://[a-z0-9][a-z0-9-]*/coll/issues/doc/[a-z0-9][a-z0-9-]*\.md$")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ParentFile(ContractModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SelectionBudget(ContractModel):
    session_max_usd: Decimal
    prior_conservative_spend_usd: Decimal
    new_execution_limit_usd: Decimal
    per_trial_usd: Decimal
    request_timeout_seconds: int


class SelectedTrial(ContractModel):
    arm: ArmName
    model_class: Literal["primary", "lightweight"]
    transport: Transport
    task_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    repeat_index: int = Field(ge=1)
    role: Literal["failure"]
    parent_lineage: ParentLineage
    parent_record_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_kind: Literal["provider", "request_timeout", "interrupted", "unknown"]

    @property
    def identity(self) -> SelectionIdentity:
        return (self.arm, self.model_class, self.transport, self.task_id, self.repeat_index)


class SelectionDocument(ContractModel):
    schema_version: Literal[1]
    authorization: AuthorizationPin
    parents: dict[ParentName, ParentFile]
    selected_trials: list[SelectedTrial]
    budget: SelectionBudget

    @model_validator(mode="after")
    def validate_approved_selection(self) -> SelectionDocument:
        if self.authorization.model_dump(mode="json") != APPROVED_AUTHORIZATION:
            raise ValueError("selection authorization differs from the approved AKB-361 revision")
        if set(self.parents) != set(APPROVED_PARENT_SHA256):
            raise ValueError("selection must pin the four approved resume04 parent files")
        if {name: parent.sha256 for name, parent in self.parents.items()} != APPROVED_PARENT_SHA256:
            raise ValueError("selection parent file hashes differ from the approved AKB-361 evidence")
        identities = [trial.identity for trial in self.selected_trials]
        if len(set(identities)) != len(identities):
            raise ValueError("selection contains duplicate arm-specific trial identities")
        if set(identities) != set(_FAILURE_KIND_BY_IDENTITY):
            raise ValueError("selection identities differ from the 23 approved parent failures")
        for trial in self.selected_trials:
            if (
                trial.role != "failure"
                or trial.parent_lineage != "resume04"
                or trial.failure_kind != _FAILURE_KIND_BY_IDENTITY[trial.identity]
            ):
                raise ValueError("selection role, parent lineage, or failure kind differs from the approved sample")
        if Counter(trial.arm for trial in self.selected_trials) != Counter(baseline=13, candidate=10):
            raise ValueError("selection must contain 13 baseline and 10 candidate failures")
        budget = self.budget
        if (
            budget.session_max_usd != TOTAL_BUDGET_USD
            or budget.prior_conservative_spend_usd != PRIOR_SPEND_USD
            or budget.new_execution_limit_usd != NEW_EXECUTION_LIMIT_USD
            or budget.per_trial_usd != PER_TRIAL_LIMIT_USD
            or budget.request_timeout_seconds != REQUEST_TIMEOUT_SECONDS
        ):
            raise ValueError("selection budget differs from the approved measurement limits")
        return self


@dataclass(frozen=True, slots=True)
class ToolGuidanceSelection:
    path: Path
    sha256: str
    authorization: AuthorizationPin
    budget: SelectionBudget
    parents: dict[str, dict[str, str]]
    selected_trials: tuple[SelectedTrial, ...]
    parent_results: dict[SelectionIdentity, dict[str, Any]]
    execution_manifest: BenchmarkRunManifest

    @property
    def expected_trials_by_arm(self) -> dict[ArmName, int]:
        counts = Counter(trial.arm for trial in self.selected_trials)
        return {"baseline": counts["baseline"], "candidate": counts["candidate"]}

    @property
    def identities(self) -> frozenset[SelectionIdentity]:
        return frozenset(trial.identity for trial in self.selected_trials)

    def contains(
        self,
        arm: ArmName,
        model_class: str,
        transport: str,
        task_id: str,
        repeat_index: int,
    ) -> bool:
        return (arm, model_class, transport, task_id, repeat_index) in self.identities

    def artifact_metadata(self) -> dict[str, Any]:
        preferences = {
            model.class_name: list(model.routing.order)
            for model in self.execution_manifest.models
        }
        return {
            "purpose": "provider_failure_remeasurement",
            "authorization": self.authorization.model_dump(mode="json"),
            "selection_sha256": self.sha256,
            "parents": {name: dict(parent) for name, parent in sorted(self.parents.items())},
            "selected_trials": [trial.model_dump(mode="json") for trial in self.selected_trials],
            "expected_trials_by_arm": self.expected_trials_by_arm,
            "expected_model_trials": len(self.selected_trials),
            "routing_policy": {
                "provider_preference_by_model": preferences,
                "allow_fallbacks": True,
                "require_parameters": True,
                "quantizations": ["fp8"],
            },
            "budget": {
                "session_max_usd": str(self.budget.session_max_usd),
                "prior_conservative_spend_usd": str(self.budget.prior_conservative_spend_usd),
                "new_execution_limit_usd": str(self.budget.new_execution_limit_usd),
                "per_trial_usd": str(self.budget.per_trial_usd),
                "request_timeout_seconds": self.budget.request_timeout_seconds,
            },
        }

    def checkpoint_preflight(self) -> dict[str, Any]:
        """Return the immutable source and budget validation stored in child checkpoints."""

        return {
            "read_only": True,
            "selection_path": str(self.path),
            "selection_sha256": self.sha256,
            "authorization": self.authorization.model_dump(mode="json"),
            "parents": {name: dict(parent) for name, parent in sorted(self.parents.items())},
            "parent_count": len(self.parents),
            "selected_trial_count": len(self.selected_trials),
            "selected_trials_by_arm": self.expected_trials_by_arm,
            "failure_kind_counts": dict(
                sorted(Counter(trial.failure_kind for trial in self.selected_trials).items())
            ),
            "budget": {
                "session_max_usd": str(self.budget.session_max_usd),
                "prior_conservative_spend_usd": str(self.budget.prior_conservative_spend_usd),
                "new_execution_limit_usd": str(self.budget.new_execution_limit_usd),
                "per_trial_usd": str(self.budget.per_trial_usd),
                "request_timeout_seconds": self.budget.request_timeout_seconds,
            },
        }

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        manifest: BenchmarkRunManifest,
        tasks: list[TaskManifest],
    ) -> ToolGuidanceSelection:
        try:
            path = path.expanduser().resolve(strict=True)
            raw = path.read_bytes()
        except (OSError, RuntimeError) as exc:
            raise ValueError("selection file is unavailable") from exc
        selection_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            document = SelectionDocument.model_validate_json(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"selection file is invalid: {exc}") from exc

        parents = _load_parent_files(document.parents, selection_directory=path.parent)
        source_manifest = _validate_parent_artifacts(parents, tasks)
        execution_manifest = _build_execution_manifest(
            source_manifest,
            manifest,
            tasks,
            document.budget,
        )
        parent_results = _parent_results(parents, document.selected_trials)
        return cls(
            path=path,
            sha256=selection_sha256,
            authorization=document.authorization,
            budget=document.budget,
            parents={
                name: {"path": parent["path"], "sha256": parent["sha256"]}
                for name, parent in sorted(parents.items())
            },
            selected_trials=tuple(document.selected_trials),
            parent_results=parent_results,
            execution_manifest=execution_manifest,
        )


def _load_parent_files(
    parents: dict[ParentName, ParentFile],
    *,
    selection_directory: Path,
) -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    for name, binding in parents.items():
        try:
            parent_path = Path(binding.path).expanduser()
            if not parent_path.is_absolute():
                parent_path = selection_directory / parent_path
            parent_path = parent_path.resolve(strict=True)
            raw = parent_path.read_bytes()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{name} parent file is unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != binding.sha256:
            raise ValueError(f"{name} parent file SHA-256 does not match the selection")
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"{name} parent file is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{name} parent file must contain a JSON object")
        loaded[name] = {
            "path": str(parent_path),
            "sha256": binding.sha256,
            "document": value,
        }
    return loaded


def _validate_parent_artifacts(
    parents: dict[str, dict[str, Any]],
    tasks: list[TaskManifest],
) -> BenchmarkRunManifest:
    task_corpus_hash = hash_json([task.model_dump(mode="json") for task in tasks])
    manifests: list[BenchmarkRunManifest] = []
    for arm in ("baseline", "candidate"):
        artifact = parents[f"{arm}_artifact"]["document"]
        checkpoint = parents[f"{arm}_checkpoint"]["document"]
        if artifact.get("arm") != arm or artifact.get("source_revision") != SOURCE_REVISIONS[arm]:
            raise ValueError(f"resume04 {arm} artifact has the wrong source identity")
        try:
            parent_manifest = BenchmarkRunManifest.model_validate(artifact["manifest"])
        except Exception as exc:
            raise ValueError(f"resume04 {arm} artifact manifest is invalid: {exc}") from exc
        manifest_hash = hash_json(parent_manifest.model_dump(mode="json"))
        if (
            artifact.get("run_manifest_hash") != manifest_hash
            or artifact.get("task_corpus_hash") != task_corpus_hash
            or parent_manifest.arm_source_revisions[arm] != SOURCE_REVISIONS[arm]
        ):
            raise ValueError(f"resume04 {arm} artifact manifest or task corpus hash is invalid")
        if not isinstance(checkpoint.get("header"), dict):
            raise ValueError(f"resume04 {arm} checkpoint header is missing")
        header = checkpoint["header"]
        if (
            checkpoint.get("schema_version") != 4
            or checkpoint.get("lifecycle") != "finalized"
            or header.get("arm") != arm
            or header.get("source_revision") != SOURCE_REVISIONS[arm]
            or header.get("run_manifest_hash") != manifest_hash
            or header.get("task_corpus_hash") != task_corpus_hash
            or header.get("pre_smoke_seal_hash") != artifact.get("pre_smoke_seal_hash")
            or header.get("provider_registry_hash") != _provider_registry_hash(artifact)
        ):
            raise ValueError(f"resume04 {arm} checkpoint header is not bound to its artifact")
        records = checkpoint.get("records")
        if not isinstance(records, dict):
            raise ValueError(f"resume04 {arm} checkpoint has no trial record map")
        for key_digest, record in records.items():
            if not isinstance(record, dict) or not isinstance(record.get("key"), dict):
                raise ValueError(f"resume04 {arm} checkpoint contains a malformed trial record")
            key = record["key"]
            outcome = record.get("outcome")
            if (
                not isinstance(outcome, dict)
                or key_digest != hash_json(key)
                or record.get("record_hash")
                != hash_json({"status": record.get("status"), "key": key, "outcome": outcome})
                or key.get("arm") != arm
                or key.get("source_revision") != SOURCE_REVISIONS[arm]
                or key.get("run_manifest_hash") != manifest_hash
                or key.get("task_corpus_hash") != task_corpus_hash
            ):
                raise ValueError(f"resume04 {arm} checkpoint record identity or hash is invalid")
        manifests.append(parent_manifest)
    baseline_manifest, candidate_manifest = manifests
    if baseline_manifest != candidate_manifest:
        raise ValueError("resume04 parent artifacts do not share an identical preregistered manifest")
    if baseline_manifest.paired_order != PAIRED_ORDER_MODE or baseline_manifest.paired_order_seed != PAIRED_ORDER_SEED:
        raise ValueError("resume04 parent manifest does not preserve the approved paired order")
    if set(baseline_manifest.arm_source_revisions.items()) != set(SOURCE_REVISIONS.items()):
        raise ValueError("resume04 parent manifest source revisions differ from the approved sources")
    for model in baseline_manifest.models:
        if (
            model.version not in {"20260731", "20260814"}
            or model.settings.get("temperature") != 0
            or model.settings.get("max_tokens") != 8192
            or model.routing.allow_fallbacks
            or model.routing.require_parameters is not True
            or model.routing.quantizations != ["fp8"]
        ):
            raise ValueError("resume04 parent manifest does not preserve the approved model contract")
    return baseline_manifest


def _provider_registry_hash(artifact: dict[str, Any]) -> str | None:
    registry = artifact.get("provider_registry")
    return registry.get("snapshot_hash") if isinstance(registry, dict) else None


def _build_execution_manifest(
    source_manifest: BenchmarkRunManifest,
    input_manifest: BenchmarkRunManifest,
    tasks: list[TaskManifest],
    selection_budget: SelectionBudget,
) -> BenchmarkRunManifest:
    source_manifest.validate_tasks(tasks)
    input_manifest.validate_tasks(tasks)
    if source_manifest.model_dump(mode="json"):
        source_data = source_manifest.model_dump(mode="json")
        input_data = input_manifest.model_dump(mode="json")
        for field in ("models", "budget", "arm_source_revisions"):
            source_data.pop(field, None)
            input_data.pop(field, None)
        if source_data != input_data:
            raise ValueError("resume04 parent run plan differs from the supplied benchmark contract")
    source_models = {model.class_name: model for model in source_manifest.models}
    input_models = {model.class_name: model for model in input_manifest.models}
    if set(source_models) != set(input_models):
        raise ValueError("resume04 parent model classes differ from the supplied benchmark contract")
    for class_name, source_model in source_models.items():
        input_model = input_models[class_name]
        if (
            source_model.model_id != input_model.model_id
            or source_model.version != input_model.version
            or source_model.provider != input_model.provider
            or source_model.base_url_env != input_model.base_url_env
            or source_model.provider_key_env != input_model.provider_key_env
            or source_model.settings != input_model.settings
        ):
            raise ValueError(f"resume04 {class_name} model identity differs from the supplied benchmark contract")
    expected_budget = {
        "max_total_cost_usd": float(selection_budget.session_max_usd),
        "max_cost_per_trial_usd": float(selection_budget.per_trial_usd),
        "request_timeout_seconds": selection_budget.request_timeout_seconds,
    }
    if source_manifest.budget.model_dump(mode="json") != expected_budget:
        raise ValueError("resume04 manifest budget differs from the approved selection limits")
    models = [
        model.model_copy(
            update={
                "routing": model.routing.model_copy(update={"allow_fallbacks": True}),
            }
        )
        for model in source_manifest.models
    ]
    execution_manifest = source_manifest.model_copy(update={"models": models})
    if set(execution_manifest.arms) != {"baseline", "candidate"} or set(execution_manifest.transports) != {"http", "stdio"}:
        raise ValueError("run manifest does not contain the approved paired transports and arms")
    for model in execution_manifest.models:
        if not model.routing.allow_fallbacks:
            raise ValueError("selection routing must allow automatic provider fallbacks")
    return execution_manifest


def _parent_rows(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    runs = artifact.get("runs")
    if not isinstance(runs, dict):
        raise ValueError("parent artifact has no run rows")
    rows: list[dict[str, Any]] = []
    for run in runs.values():
        if not isinstance(run, dict):
            raise ValueError("parent artifact contains a malformed run")
        trials = run.get("trials")
        if not isinstance(trials, list) or any(not isinstance(row, dict) for row in trials):
            raise ValueError("parent artifact contains malformed trial rows")
        rows.extend(trials)
    return rows


def _parent_results(
    parents: dict[str, dict[str, Any]],
    selected_trials: list[SelectedTrial],
) -> dict[SelectionIdentity, dict[str, Any]]:
    results: dict[SelectionIdentity, dict[str, Any]] = {}
    selected_by_arm: dict[ArmName, set[SelectionIdentity]] = {
        arm: {trial.identity for trial in selected_trials if trial.arm == arm}
        for arm in ARM_NAMES
    }
    for arm in ARM_NAMES:
        artifact = parents[f"{arm}_artifact"]["document"]
        checkpoint = parents[f"{arm}_checkpoint"]["document"]
        rows = _parent_rows(artifact)
        failed_rows = {
            (
                arm,
                row.get("model_class"),
                row.get("transport"),
                row.get("task_id"),
                row.get("repeat_index"),
            )
            for row in rows
            if row.get("failure_kind") not in (None, "none")
        }
        if failed_rows != selected_by_arm[arm]:
            raise ValueError(f"resume04 {arm} selection does not include exactly every recorded execution failure")
    for trial in selected_trials:
        artifact = parents[f"{trial.arm}_artifact"]["document"]
        checkpoint = parents[f"{trial.arm}_checkpoint"]["document"]
        matching_rows = [
            row
            for row in _parent_rows(artifact)
            if row.get("arm") == trial.arm
            and row.get("model_class") == trial.model_class
            and row.get("transport") == trial.transport
            and row.get("task_id") == trial.task_id
            and row.get("repeat_index") == trial.repeat_index
        ]
        if len(matching_rows) != 1:
            raise ValueError("selected failure does not map to exactly one parent artifact row")
        row = matching_rows[0]
        if row.get("success") is not False or row.get("failure_kind") != trial.failure_kind:
            raise ValueError("selected parent row is not the approved failed outcome")
        statuses = [
            status
            for run_key, run in artifact["runs"].items()
            if run_key == f"{trial.model_class}:{trial.transport}" and isinstance(run, dict)
            for status in run.get("statuses", [])
            if isinstance(status, dict)
            and status.get("task_id") == trial.task_id
            and status.get("repeat_index") == trial.repeat_index
        ]
        if len(statuses) != 1 or statuses[0].get("status") != "failed":
            raise ValueError("selected parent artifact row does not have a failed checkpoint status")
        record = checkpoint["records"].get(trial.parent_record_key)
        if (
            not isinstance(record, dict)
            or record.get("status") != "failed"
            or record.get("record_hash") != trial.parent_record_hash
            or record.get("key", {}).get("arm") != trial.arm
            or record.get("key", {}).get("model_class") != trial.model_class
            or record.get("key", {}).get("transport") != trial.transport
            or record.get("key", {}).get("task_id") != trial.task_id
            or record.get("key", {}).get("repeat_index") != trial.repeat_index
            or record.get("outcome") != row
            or record.get("outcome", {}).get("failure_kind") != trial.failure_kind
        ):
            raise ValueError("selected parent checkpoint record or record hash differs from its artifact row")
        results[trial.identity] = {
            "arm": trial.arm,
            "row": row,
            "record_key": trial.parent_record_key,
            "record_hash": trial.parent_record_hash,
            "status": record["status"],
        }
    return results


def _parent_metrics(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "success",
        "trial_error",
        "failure_kind",
        "error",
        "model_requests",
        "input_tokens",
        "output_tokens",
        "cost_usd",
    )
    return {field: row.get(field) for field in fields}


def _sealed_instructions(
    artifact: dict[str, Any],
    outcome: TrialOutcome,
    task_by_id: dict[str, TaskManifest],
) -> str | None:
    task = task_by_id.get(outcome.task_id)
    catalogs = artifact.get("catalogs")
    if task is None or not isinstance(catalogs, dict):
        return None
    snapshot = catalogs.get(f"{outcome.transport}:{task.fixture.credential_profile}")
    discovery = snapshot.get("server_discover") if isinstance(snapshot, dict) else None
    instructions = discovery.get("instructions") if isinstance(discovery, dict) else None
    return instructions if isinstance(instructions, str) else None


def _provider_names(outcome: TrialOutcome) -> list[str]:
    names: Counter[str] = Counter()
    for evidence in outcome.provider_evidence:
        if not isinstance(evidence, dict):
            continue
        routing = evidence.get("routing")
        if not isinstance(routing, dict):
            continue
        endpoints = routing.get("endpoints")
        available = endpoints.get("available") if isinstance(endpoints, dict) else None
        for endpoint in available if isinstance(available, list) else []:
            if not isinstance(endpoint, dict) or endpoint.get("selected") is not True:
                continue
            provider = endpoint.get("provider")
            if isinstance(provider, str) and provider:
                names[provider] += 1
    return [name for name, _count in sorted(names.items())]


def build_selection_summary(
    selection: ToolGuidanceSelection,
    artifacts: dict[str, dict[str, Any]],
    comparison: dict[str, Any],
    *,
    manifest: BenchmarkRunManifest,
    tasks: list[TaskManifest],
) -> dict[str, Any]:
    expected_by_arm = {
        arm: {identity for identity in selection.identities if identity[0] == arm}
        for arm in ("baseline", "candidate")
    }
    task_by_id = {task.id: task for task in tasks}
    model_ids = {model.class_name: model.model_id for model in manifest.models}
    arm_summaries: dict[str, Any] = {}
    outcomes: dict[SelectionIdentity, TrialOutcome] = {}
    event_lists: list[list[dict[str, Any]]] = []
    expected_new_model_trials = 0
    complete = comparison.get("verdict") == "inconclusive"

    for arm in ARM_NAMES:
        artifact = artifacts.get(arm, {})
        expected = expected_by_arm[arm]
        complete = complete and artifact.get("selection") == selection.artifact_metadata()
        rows: dict[SelectionIdentity, TrialOutcome] = {}
        counts: Counter[SelectionIdentity] = Counter()
        malformed = 0
        runs = artifact.get("runs", {})
        for run in runs.values() if isinstance(runs, dict) else []:
            for raw in run.get("trials", []) if isinstance(run, dict) else []:
                if not isinstance(raw, dict):
                    malformed += 1
                    continue
                model_class = raw.get("model_class")
                transport = raw.get("transport")
                task_id = raw.get("task_id")
                repeat_index = raw.get("repeat_index")
                if not (
                    isinstance(model_class, str)
                    and isinstance(transport, str)
                    and isinstance(task_id, str)
                    and type(repeat_index) is int
                ):
                    malformed += 1
                    continue
                identity: SelectionIdentity = (
                    cast(ArmName, arm),
                    model_class,
                    transport,
                    task_id,
                    repeat_index,
                )
                counts[identity] += 1
                try:
                    validate_model_input_evidence(raw)
                    outcome = TrialOutcome.model_validate(raw)
                except ValueError:
                    malformed += 1
                    continue
                model_class = cast(Literal["primary", "lightweight"], outcome.model_class)
                if outcome.arm != arm or outcome.model_id != model_ids.get(model_class):
                    malformed += 1
                    continue
                rows[identity] = outcome
                outcomes[identity] = outcome

        observed = set(rows)
        missing = sorted(expected - observed)
        duplicates = sorted(identity for identity in expected if counts[identity] > 1)
        extras = sorted(observed - expected)
        instruction_missing: list[SelectionIdentity] = []
        unmeasured: list[SelectionIdentity] = []
        for identity, outcome in rows.items():
            if outcome.error is None:
                task = task_by_id.get(outcome.task_id)
                if task is None or not has_measured_evidence(outcome, task):
                    unmeasured.append(identity)
                instructions = _sealed_instructions(artifact, outcome, task_by_id)
                if not has_server_instructions_evidence(outcome, instructions):
                    instruction_missing.append(identity)

        smoke = artifact.get("smoke_gate")
        smoke_valid = (
            isinstance(smoke, dict)
            and smoke.get("status") == "not_run"
            and smoke.get("required_cells") == []
            and smoke.get("cells") == []
        )
        checkpoint = artifact.get("checkpoint")
        checkpoint_new_trials = checkpoint.get("new_trials") if isinstance(checkpoint, dict) else None
        checkpoint_reused_trials = checkpoint.get("reused_trials") if isinstance(checkpoint, dict) else None
        checkpoint_rerun_trials = checkpoint.get("rerun_trials") if isinstance(checkpoint, dict) else None
        checkpoint_spent = checkpoint.get("spent") if isinstance(checkpoint, dict) else None
        checkpoint_spent_cost = Decimal("NaN")
        checkpoint_spent_valid = False
        if isinstance(checkpoint_spent, dict):
            try:
                checkpoint_spent_cost = Decimal(str(checkpoint_spent.get("cost_usd")))
                checkpoint_spent_valid = (
                    all(
                        type(checkpoint_spent.get(field)) is int and checkpoint_spent[field] >= 0
                        for field in (
                            "model_requests",
                            "provider_setup_requests",
                            "input_tokens",
                            "output_tokens",
                        )
                    )
                    and checkpoint_spent["provider_setup_requests"] <= checkpoint_spent["model_requests"]
                    and checkpoint_spent_cost.is_finite()
                    and checkpoint_spent_cost >= 0
                )
            except (ArithmeticError, KeyError, TypeError, ValueError):
                checkpoint_spent_valid = False
        checkpoint_valid = (
            isinstance(checkpoint, dict)
            and checkpoint.get("record_count") == len(expected)
            and checkpoint.get("lifecycle") == "finalized"
            and checkpoint.get("reserved_cost_usd") == 0
            and checkpoint.get("smoke_status") is None
            and checkpoint.get("smoke_cell_count") == 0
            and type(checkpoint_new_trials) is int
            and type(checkpoint_reused_trials) is int
            and type(checkpoint_rerun_trials) is int
            and checkpoint_new_trials >= 0
            and checkpoint_reused_trials >= 0
            and checkpoint_rerun_trials >= 0
            and checkpoint_new_trials + checkpoint_reused_trials == len(expected)
            and checkpoint_rerun_trials <= checkpoint_new_trials
            and checkpoint_spent_valid
        )
        if checkpoint_valid:
            expected_new_model_trials += cast(int, checkpoint_new_trials)
        arm_complete = (
            not missing
            and not duplicates
            and not extras
            and malformed == 0
            and not unmeasured
            and not instruction_missing
            and smoke_valid
            and checkpoint_valid
        )
        complete = complete and arm_complete
        execution = artifact.get("paired_execution")
        if isinstance(execution, dict) and isinstance(execution.get("events"), list):
            event_lists.append(execution["events"])
            complete = complete and execution.get("complete") is True
        else:
            complete = False

        new_failure_kinds = Counter(
            outcome.failure_kind
            for identity, outcome in rows.items()
            if identity in expected and outcome.failure_kind != "none"
        )
        arm_summaries[arm] = {
            "artifact_status": artifact.get("status"),
            "expected_selected_trials": len(expected),
            "observed_selected_trials": len(rows),
            "successful_tasks": sum(outcome.success for outcome in rows.values()),
            "task_error_trials": sum(outcome.trial_error for outcome in rows.values()),
            "execution_failure_trials": sum(outcome.error is not None for outcome in rows.values()),
            "new_failure_kind_counts": dict(sorted(new_failure_kinds.items())),
            "missing_identities": [list(item[1:]) for item in missing],
            "duplicate_identities": [list(item[1:]) for item in duplicates],
            "unexpected_identities": [list(item[1:]) for item in extras],
            "malformed_trials": malformed,
            "unmeasured_completed_trials": [list(item[1:]) for item in unmeasured],
            "missing_instruction_proof": [list(item[1:]) for item in instruction_missing],
            "new_model_requests": (
                checkpoint_spent["model_requests"] - checkpoint_spent["provider_setup_requests"]
                if checkpoint_spent_valid and isinstance(checkpoint_spent, dict)
                else sum(outcome.model_requests for outcome in rows.values())
            ),
            "new_input_tokens": (
                checkpoint_spent["input_tokens"]
                if checkpoint_spent_valid and isinstance(checkpoint_spent, dict)
                else sum(outcome.input_tokens for outcome in rows.values())
            ),
            "new_output_tokens": (
                checkpoint_spent["output_tokens"]
                if checkpoint_spent_valid and isinstance(checkpoint_spent, dict)
                else sum(outcome.output_tokens for outcome in rows.values())
            ),
            "new_cost_usd": (
                str(checkpoint_spent_cost)
                if checkpoint_spent_valid
                else str(sum((Decimal(str(outcome.cost_usd)) for outcome in rows.values()), start=Decimal("0")))
            ),
            "observed_upstreams": sorted({name for outcome in rows.values() for name in _provider_names(outcome)}),
            "smoke_gate_status": smoke.get("status") if isinstance(smoke, dict) else None,
            "checkpoint_valid": checkpoint_valid,
            "checkpoint_new_trials": checkpoint_new_trials,
            "checkpoint_reused_trials": checkpoint_reused_trials,
            "checkpoint_rerun_trials": checkpoint_rerun_trials,
        }

    expected_events = {
        (f"{model_class}:{transport}", task_id, repeat_index, arm)
        for arm, model_class, transport, task_id, repeat_index in selection.identities
    }
    event_count = 0
    observed_unique_trials = 0
    unknown_start_gaps: list[dict[str, Any]] = []
    if len(event_lists) == 2 and event_lists[0] == event_lists[1]:
        events = event_lists[0]
        event_count = len(events)
        keys: set[tuple[str, str, int, str]] = set()
        event_chains: dict[tuple[str, str, int, str], list[dict[str, Any]]] = {}
        sequences: list[int] = []
        event_shape_valid = True
        try:
            for event in events:
                sequence = event.get("sequence")
                event_identity = (
                    event.get("cell"),
                    event.get("task_id"),
                    event.get("repeat_index"),
                    event.get("arm"),
                )
                if (
                    type(sequence) is not int
                    or not isinstance(event_identity[0], str)
                    or not isinstance(event_identity[1], str)
                    or type(event_identity[2]) is not int
                    or event_identity[3] not in ARM_NAMES
                    or type(event.get("order_position")) is not int
                    or event.get("status") not in {"started", "completed", "failed", "incomplete"}
                ):
                    event_shape_valid = False
                    continue
                typed_event_identity = cast(tuple[str, str, int, str], event_identity)
                keys.add(typed_event_identity)
                sequences.append(sequence)
                event_chains.setdefault(typed_event_identity, []).append(event)
                expected_order = planned_arm_order(
                    event_identity[1],
                    event_identity[2],
                    manifest.paired_order_seed,
                ).index(event_identity[3])
                if event["order_position"] != expected_order:
                    event_shape_valid = False
        except (KeyError, TypeError, ValueError):
            event_shape_valid = False

        observed_unique_trials = len(keys)
        declared_gaps = artifacts.get("baseline", {}).get("paired_execution", {}).get("unknown_start_gaps")
        if not isinstance(declared_gaps, list):
            declared_gaps = []
            event_shape_valid = False
        unknown_start_gaps = declared_gaps
        unique_sequences = set(sequences)
        missing_sequences = sorted(set(range(1, max(unique_sequences, default=0) + 1)) - unique_sequences)
        expected_gaps = [{"sequence": sequence, "status": "unknown_start"} for sequence in missing_sequences]
        if declared_gaps != expected_gaps or sequences != sorted(sequences):
            event_shape_valid = False

        attempts_valid = True
        latest_sequence_by_identity: dict[tuple[str, str, int, str], int] = {}
        for chain_identity, chain in event_chains.items():
            previous_sequence: int | None = None
            for attempt, event in enumerate(chain, start=1):
                if event.get("attempt", 1) != attempt:
                    attempts_valid = False
                if attempt == 1:
                    if event.get("retry_of_sequence") is not None:
                        attempts_valid = False
                elif event.get("retry_of_sequence") != previous_sequence:
                    attempts_valid = False
                sequence = event.get("sequence")
                if type(sequence) is int:
                    previous_sequence = sequence
            if chain:
                sequence = chain[-1].get("sequence")
                if type(sequence) is int:
                    latest_sequence_by_identity[chain_identity] = sequence

        latest_outcomes_valid = True
        for expected_identity in expected_events:
            cell, task_id, repeat_index, arm = expected_identity
            model_class, transport = cell.split(":", 1)
            trial_identity: SelectionIdentity = (
                arm,
                model_class,
                transport,
                task_id,
                repeat_index,
            )
            latest_outcome = outcomes.get(trial_identity)
            if latest_outcome is None or latest_outcome.paired_execution_sequence != latest_sequence_by_identity.get(
                expected_identity
            ):
                latest_outcomes_valid = False

        unfinished = artifacts.get("baseline", {}).get("paired_execution", {}).get("unfinished_starts")
        expected_unfinished = [event for event in events if event.get("status") == "started"]
        paired_evidence_equal = artifacts.get("baseline", {}).get("paired_execution") == artifacts.get(
            "candidate", {}
        ).get("paired_execution")
        events_valid = (
            event_shape_valid
            and attempts_valid
            and latest_outcomes_valid
            and paired_evidence_equal
            and keys == expected_events
            and sequences == sorted(set(sequences))
            and declared_gaps == expected_gaps
            and unfinished == expected_unfinished
        )
        complete = complete and events_valid
    else:
        events_valid = False
        complete = False

    identity_results: list[dict[str, Any]] = []
    for trial in selection.selected_trials:
        parent = selection.parent_results[trial.identity]
        new_outcome = outcomes.get(trial.identity)
        identity_results.append(
            {
                "arm": trial.arm,
                "identity": list(trial.identity[1:]),
                "role": trial.role,
                "parent_lineage": trial.parent_lineage,
                "parent_record_key": trial.parent_record_key,
                "parent_record_hash": trial.parent_record_hash,
                "parent_failure_kind": trial.failure_kind,
                "parent": _parent_metrics(parent["row"]),
                "new": (
                    {
                        "success": new_outcome.success,
                        "trial_error": new_outcome.trial_error,
                        "failure_kind": new_outcome.failure_kind,
                        "error": new_outcome.error,
                        "model_requests": new_outcome.model_requests,
                        "input_tokens": new_outcome.input_tokens,
                        "output_tokens": new_outcome.output_tokens,
                        "cost_usd": new_outcome.cost_usd,
                        "upstreams": _provider_names(new_outcome),
                    }
                    if new_outcome is not None
                    else None
                ),
            }
        )
    total_cost = sum(
        (Decimal(str(outcome.cost_usd)) for outcome in outcomes.values()),
        start=Decimal("0"),
    )
    paired_budget = artifacts.get("baseline", {}).get("paired_budget_used")
    try:
        reported_paired_cost = Decimal(str(paired_budget.get("cost_usd"))) if isinstance(paired_budget, dict) else Decimal("NaN")
        paired_cost_valid = (
            reported_paired_cost.is_finite()
            and Decimal("0") <= reported_paired_cost <= selection.budget.new_execution_limit_usd
        )
    except (ArithmeticError, ValueError):
        paired_cost_valid = False
    if (
        not isinstance(paired_budget, dict)
        or artifacts.get("candidate", {}).get("paired_budget_used") != paired_budget
        or not paired_cost_valid
        or paired_budget.get("new_execution_limit_usd") != str(selection.budget.new_execution_limit_usd)
    ):
        complete = False
    return {
        "schema_version": 1,
        "selection": selection.artifact_metadata(),
        "status": "complete" if complete else "incomplete",
        "canonical_comparison_verdict": comparison.get("verdict", "inconclusive"),
        "adopt_eligible": False,
        "parents_merged": False,
        "arms": arm_summaries,
        "identity_results": identity_results,
        "expected_selected_trials_by_arm": selection.expected_trials_by_arm,
        "expected_new_model_trials": expected_new_model_trials,
        "observed_execution_events": event_count,
        "observed_unique_selected_trials": observed_unique_trials,
        "unknown_start_gaps": unknown_start_gaps,
        "paired_execution_events_valid": events_valid,
        "observed_new_smoke_model_requests": 0,
        "new_selected_trial_cost_usd": str(reported_paired_cost) if paired_cost_valid else str(total_cost),
        "new_execution_cost_limit_usd": str(selection.budget.new_execution_limit_usd),
    }
