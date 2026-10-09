"""Pinned selection and reporting for the AKB-361 paired provider recovery."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import ArmName, BenchmarkRunManifest, ContractModel, TaskManifest, Transport, hash_json
from .execution import TrialOutcome, has_measured_evidence

WORK_ITEM = "AKB-361"
AUTHORIZATION_REVISION = "491294516f1d7a337e8e0ee334bfa99c5f5ae4e4"  # pragma: allowlist secret
AUTHORIZATION_CONTENT_SHA256 = "9496f8c5f05391cb5e7bfde780508c4ea5b2091d371371374e1093a4a9c193fb"  # pragma: allowlist secret
AUTHORIZED_SELECTION_SHA256 = "df32620ad0560c81ba4cceab816e7970660c1683a2651127b3796ad1605491ee"  # pragma: allowlist secret
EXPECTED_SELECTED_IDENTITIES = 65
EXPECTED_UNMEASURED_PARENT_OUTCOMES = 90
SELECTION_BASIS = (
    "parent provider failure without measured evidence; both arms freshly execute each selected paired identity; "
    "do not combine provider regimes"
)
TOTAL_BUDGET_USD = Decimal("50")
RECEIPT_TOTAL_USD = Decimal("8.04371078")
ACCOUNT_WEEKLY_USAGE_USD = Decimal("8.10218784")
PRIOR_SPEND_BASIS_USD = max(RECEIPT_TOTAL_USD, ACCOUNT_WEEKLY_USAGE_USD)
NEW_EXECUTION_LIMIT_USD = TOTAL_BUDGET_USD - PRIOR_SPEND_BASIS_USD

RecoveryIdentity = tuple[str, str, str, int]


class RecoveryParent(ContractModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecoveryTrial(ContractModel):
    model_class: Literal["primary", "lightweight"]
    transport: Transport
    task_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    repeat_index: int = Field(ge=1)

    @property
    def identity(self) -> RecoveryIdentity:
        return (self.model_class, self.transport, self.task_id, self.repeat_index)


class RecoveryParentOutcome(RecoveryTrial):
    arm: ArmName


class RecoverySelectionDocument(ContractModel):
    schema_version: Literal[1]
    mode: Literal["paired_provider_recovery"]
    parents: dict[ArmName, RecoveryParent]
    selection_basis: str
    selected_trials: list[RecoveryTrial] = Field(min_length=1)
    unmeasured_parent_provider_outcomes: list[RecoveryParentOutcome] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_selection_shape(self) -> RecoverySelectionDocument:
        if set(self.parents) != {"baseline", "candidate"}:
            raise ValueError("recovery selection must pin both paired parent artifacts")
        identities = [trial.identity for trial in self.selected_trials]
        if len(set(identities)) != len(identities):
            raise ValueError("recovery selection contains duplicate trial identities")
        if self.selection_basis != SELECTION_BASIS:
            raise ValueError("recovery selection basis differs from the authorized issue")
        if len(self.unmeasured_parent_provider_outcomes) != EXPECTED_UNMEASURED_PARENT_OUTCOMES:
            raise ValueError("recovery selection must pin the 90 unmeasured parent outcomes")
        parent_keys = [
            (item.arm, *item.identity)
            for item in self.unmeasured_parent_provider_outcomes
        ]
        if len(set(parent_keys)) != len(parent_keys):
            raise ValueError("recovery selection contains duplicate parent outcomes")
        if {item.identity for item in self.unmeasured_parent_provider_outcomes} != set(identities):
            raise ValueError("the 90 parent outcomes must identify exactly the 65 paired recovery trials")
        return self


@dataclass(frozen=True, slots=True)
class RecoverySelection:
    path: Path
    sha256: str
    parents: dict[str, dict[str, str]]
    selection_basis: str
    selected_trials: tuple[RecoveryTrial, ...]
    unmeasured_parent_provider_outcomes: tuple[RecoveryParentOutcome, ...]

    @property
    def identities(self) -> frozenset[RecoveryIdentity]:
        return frozenset(trial.identity for trial in self.selected_trials)

    def contains(self, model_class: str, transport: str, task_id: str, repeat_index: int) -> bool:
        return (model_class, transport, task_id, repeat_index) in self.identities

    def artifact_metadata(self) -> dict[str, Any]:
        return {
            "work_item": WORK_ITEM,
            "authorization_revision": AUTHORIZATION_REVISION,
            "authorization_content_sha256": AUTHORIZATION_CONTENT_SHA256,
            "mode": "paired_provider_recovery",
            "selection_sha256": self.sha256,
            "selection_basis": self.selection_basis,
            "parents": {arm: dict(parent) for arm, parent in sorted(self.parents.items())},
            "selected_trials": [trial.model_dump(mode="json") for trial in self.selected_trials],
            "unmeasured_parent_provider_outcomes": [
                item.model_dump(mode="json") for item in self.unmeasured_parent_provider_outcomes
            ],
            "selected_identity_count": len(self.selected_trials),
            "expected_model_trials": 2 * len(self.selected_trials),
            "budget": {
                "registered_total_cap_usd": str(TOTAL_BUDGET_USD),
                "receipt_total_usd": str(RECEIPT_TOTAL_USD),
                "account_weekly_usage_usd": str(ACCOUNT_WEEKLY_USAGE_USD),
                "prior_spend_basis_usd": str(PRIOR_SPEND_BASIS_USD),
                "new_execution_limit_usd": str(NEW_EXECUTION_LIMIT_USD),
                "request_count_limit": None,
                "overall_wall_time_limit_seconds": None,
            },
        }

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        manifest: BenchmarkRunManifest,
        tasks: list[TaskManifest],
    ) -> RecoverySelection:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError("recovery selection file is unavailable") from exc
        selection_sha256 = hashlib.sha256(raw).hexdigest()
        if selection_sha256 != AUTHORIZED_SELECTION_SHA256:
            raise ValueError("recovery selection does not match the authorized pinned file")
        try:
            document = RecoverySelectionDocument.model_validate_json(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"recovery selection is invalid: {exc}") from exc
        if len(document.selected_trials) != EXPECTED_SELECTED_IDENTITIES:
            raise ValueError("authorized recovery selection must contain exactly 65 paired identities")
        _validate_recovery_budget(manifest)
        _validate_selected_trials(document.selected_trials, manifest=manifest, tasks=tasks)
        parents = _validate_parents(
            document.parents,
            selected_trials=document.selected_trials,
            unmeasured_parent_provider_outcomes=document.unmeasured_parent_provider_outcomes,
            manifest=manifest,
            tasks=tasks,
        )
        return cls(
            path,
            selection_sha256,
            parents,
            document.selection_basis,
            tuple(document.selected_trials),
            tuple(document.unmeasured_parent_provider_outcomes),
        )


def _validate_recovery_budget(manifest: BenchmarkRunManifest) -> None:
    budget = manifest.budget
    if (
        Decimal(str(budget.max_total_cost_usd)) != TOTAL_BUDGET_USD
        or Decimal(str(budget.max_cost_per_trial_usd)) != Decimal("0.10")
        or budget.request_timeout_seconds != 300
        or NEW_EXECUTION_LIMIT_USD != Decimal("41.89781216")
    ):
        raise ValueError("run manifest does not match the authorized paired recovery budget")


def _validate_selected_trials(
    selected_trials: list[RecoveryTrial],
    *,
    manifest: BenchmarkRunManifest,
    tasks: list[TaskManifest],
) -> None:
    models = {model.class_name: model for model in manifest.models}
    expected_routes = {"primary": "deepinfra", "lightweight": "akashml"}
    for model_class, model in models.items():
        if model.routing.order != [expected_routes[model_class]] or model.routing.allow_fallbacks:
            raise ValueError("recovery requires the pinned DeepInfra/AkashML routes without fallback")
    task_by_id = {task.id: task for task in tasks}
    for trial in selected_trials:
        if trial.model_class not in models or trial.transport not in manifest.transports:
            raise ValueError("recovery selection contains an unregistered model or transport")
        task = task_by_id.get(trial.task_id)
        if task is None or trial.transport not in task.fixture.transports:
            raise ValueError("recovery selection contains a task outside the registered corpus or transport scope")
        if trial.repeat_index > manifest.repeats:
            raise ValueError("recovery selection contains a repeat outside the registered run plan")


def _manifest_core(value: dict[str, Any], *, prior_route: bool) -> dict[str, Any]:
    result = json.loads(json.dumps(value))
    models = result.get("models")
    if not isinstance(models, list):
        raise ValueError("recovery manifest has no registered models")
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("routing"), dict):
            raise ValueError("recovery manifest has an invalid provider route")
        if prior_route and (
            model["routing"].get("order") != ["parasail"]
            or model["routing"].get("allow_fallbacks") is not False
        ):
            raise ValueError("recovery parents must be sealed to the prior Parasail provider route")
        model.pop("routing")
        model.pop("input_cost_per_million_usd", None)
        model.pop("output_cost_per_million_usd", None)
    return result


def _validate_parents(
    parents: dict[ArmName, RecoveryParent],
    *,
    selected_trials: list[RecoveryTrial],
    unmeasured_parent_provider_outcomes: list[RecoveryParentOutcome],
    manifest: BenchmarkRunManifest,
    tasks: list[TaskManifest],
) -> dict[str, dict[str, str]]:
    current_core = _manifest_core(manifest.model_dump(mode="json"), prior_route=False)
    corpus_hash = hash_json([task.model_dump(mode="json") for task in tasks])
    task_ids = [task.id for task in tasks]
    task_contracts = [task.model_dump(mode="json") for task in tasks]
    task_by_id = {task.id: task for task in tasks}
    selected = {trial.identity for trial in selected_trials}
    metadata: dict[str, dict[str, str]] = {}
    prior_manifest_hash: str | None = None

    for arm in ("baseline", "candidate"):
        parent = parents[arm]
        parent_path = Path(parent.path).expanduser()
        try:
            raw = parent_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"{arm} recovery parent artifact is unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != parent.sha256:
            raise ValueError(f"{arm} recovery parent artifact SHA-256 does not match the selection")
        try:
            artifact = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"{arm} recovery parent artifact is invalid JSON") from exc
        if not isinstance(artifact, dict) or artifact.get("arm") != arm:
            raise ValueError(f"recovery parent artifact is not the pinned {arm} arm")
        parent_manifest = artifact.get("manifest")
        manifest_hash = artifact.get("run_manifest_hash")
        if not isinstance(parent_manifest, dict) or not isinstance(manifest_hash, str):
            raise ValueError(f"{arm} recovery parent has no sealed run manifest")
        if hash_json(parent_manifest) != manifest_hash:
            raise ValueError(f"{arm} recovery parent manifest hash does not match its artifact")
        if prior_manifest_hash is not None and manifest_hash != prior_manifest_hash:
            raise ValueError("paired recovery parents do not share the same prior run manifest")
        prior_manifest_hash = manifest_hash
        if _manifest_core(parent_manifest, prior_route=True) != current_core:
            raise ValueError("recovery parent preregistration differs outside provider route and price")
        if artifact.get("source_revision") != manifest.arm_source_revisions[arm]:
            raise ValueError(f"{arm} recovery parent source revision differs from the pinned arm revision")
        if (
            artifact.get("task_corpus_hash") != corpus_hash
            or artifact.get("task_ids") != task_ids
            or artifact.get("task_contracts") != task_contracts
        ):
            raise ValueError(f"{arm} recovery parent does not contain the full registered task corpus")
        runs = artifact.get("runs")
        if not isinstance(runs, dict):
            raise ValueError(f"{arm} recovery parent trial records are missing")
        occurrences: Counter[RecoveryIdentity] = Counter()
        source_rows: dict[RecoveryIdentity, list[dict[str, Any]]] = {}
        for run in runs.values():
            if not isinstance(run, dict) or not isinstance(run.get("trials", []), list):
                raise ValueError(f"{arm} recovery parent contains an invalid trial run")
            for trial in run.get("trials", []):
                if not isinstance(trial, dict):
                    raise ValueError(f"{arm} recovery parent contains an invalid trial record")
                identity = (
                    trial.get("model_class"),
                    trial.get("transport"),
                    trial.get("task_id"),
                    trial.get("repeat_index"),
                )
                if (
                    isinstance(identity[0], str)
                    and isinstance(identity[1], str)
                    and isinstance(identity[2], str)
                    and isinstance(identity[3], int)
                ):
                    typed_identity = (identity[0], identity[1], identity[2], identity[3])
                    source_rows.setdefault(typed_identity, []).append(trial)
                    if typed_identity in selected:
                        occurrences[typed_identity] += 1
        if any(occurrences[identity] != 1 for identity in selected):
            raise ValueError(f"{arm} recovery parent does not contain exactly one selected trial")
        for source in unmeasured_parent_provider_outcomes:
            if source.arm != arm:
                continue
            rows = source_rows.get(source.identity, [])
            if len(rows) != 1:
                raise ValueError(f"{arm} parent provider outcome does not identify exactly one trial")
            try:
                outcome = TrialOutcome.model_validate(rows[0])
                measured = has_measured_evidence(outcome, task_by_id[outcome.task_id])
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{arm} parent provider outcome cannot be measured") from exc
            if outcome.failure_kind != "provider" or outcome.error is None or measured:
                raise ValueError(f"{arm} parent outcome is not an unmeasured provider failure")
        metadata[arm] = {"path": str(parent_path), "sha256": parent.sha256}
    return metadata


def build_recovery_summary(
    selection: RecoverySelection,
    artifacts: dict[str, dict[str, Any]],
    comparison: dict[str, Any],
    *,
    manifest: BenchmarkRunManifest,
    tasks: list[TaskManifest],
) -> dict[str, Any]:
    task_by_id = {task.id: task for task in tasks}
    expected = selection.identities
    expected_smoke_cells = [
        f"{model.class_name}:{transport}"
        for model in manifest.models
        for transport in manifest.transports
    ]
    arms: dict[str, Any] = {}
    observed: dict[str, set[RecoveryIdentity]] = {}
    complete = comparison.get("verdict") == "inconclusive"
    smoke_requests = 0
    paired_events = 0
    paired_complete = True
    paired_event_lists: list[list[dict[str, Any]]] = []
    for arm in ("baseline", "candidate"):
        artifact = artifacts.get(arm, {})
        complete = complete and artifact.get("recovery_selection") == selection.artifact_metadata()
        counts: Counter[RecoveryIdentity] = Counter()
        selected_requests = unselected_requests = measured = unmeasured = 0
        measured_error_trials = measured_task_failures = malformed_trial_records = 0
        cost = Decimal("0")
        all_identities: set[RecoveryIdentity] = set()
        runs = artifact.get("runs", {})
        for run in runs.values() if isinstance(runs, dict) else []:
            if not isinstance(run, dict):
                continue
            for row in run.get("trials", []):
                if not isinstance(row, dict):
                    continue
                identity = (
                    row.get("model_class"),
                    row.get("transport"),
                    row.get("task_id"),
                    row.get("repeat_index"),
                )
                if (
                    not isinstance(identity[0], str)
                    or not isinstance(identity[1], str)
                    or not isinstance(identity[2], str)
                    or not isinstance(identity[3], int)
                ):
                    malformed_trial_records += 1
                    continue
                typed_identity = (identity[0], identity[1], identity[2], identity[3])
                all_identities.add(typed_identity)
                counts[typed_identity] += 1
                requests = int(row.get("model_requests", 0))
                if typed_identity not in expected:
                    unselected_requests += requests
                    continue
                selected_requests += requests
                cost += Decimal(str(row.get("cost_usd", 0)))
                try:
                    outcome = TrialOutcome.model_validate(row)
                    has_measurement = has_measured_evidence(outcome, task_by_id[outcome.task_id])
                    if has_measurement:
                        measured += 1
                        measured_error_trials += int(outcome.error is not None)
                        measured_task_failures += int(not outcome.success)
                    else:
                        unmeasured += 1
                except (KeyError, ValueError):
                    unmeasured += 1
        observed[arm] = all_identities & expected
        missing = sorted(expected - observed[arm])
        duplicates = sorted(key for key in expected if counts[key] > 1)
        extra = sorted(all_identities - expected)
        arm_complete = (
            not missing
            and not duplicates
            and not extra
            and malformed_trial_records == 0
            and unmeasured == 0
            and measured == len(expected)
        )
        complete = complete and arm_complete
        smoke_gate = artifact.get("smoke_gate", {})
        smoke = smoke_gate.get("cells", []) if isinstance(smoke_gate, dict) else []
        smoke_cells = [cell.get("cell") for cell in smoke if isinstance(cell, dict)]
        complete = complete and (
            isinstance(smoke_gate, dict)
            and smoke_gate.get("status") == "passed"
            and smoke_gate.get("required_cells") == expected_smoke_cells
            and smoke_cells == expected_smoke_cells
            and all(
                isinstance(cell, dict)
                and cell.get("status") == "completed"
                and isinstance(cell.get("outcome"), dict)
                for cell in smoke
            )
        )
        for cell in smoke if isinstance(smoke, list) else []:
            smoke_outcome = cell.get("outcome") if isinstance(cell, dict) else None
            if isinstance(smoke_outcome, dict):
                smoke_requests += int(smoke_outcome.get("model_requests", 0))
        execution = artifact.get("paired_execution", {})
        if isinstance(execution, dict):
            events = execution.get("events", [])
            if isinstance(events, list):
                paired_events = max(paired_events, len(events))
                if all(isinstance(event, dict) for event in events):
                    paired_event_lists.append(events)
            paired_complete = paired_complete and execution.get("complete") is True
        else:
            paired_complete = False
        arms[arm] = {
            "run_artifact_status": artifact.get("status"),
            "selected_trial_records": sum(counts[key] for key in expected),
            "expected_selected_trial_records": len(expected),
            "measured_selected_trials": measured,
            "unmeasured_trials": unmeasured,
            "measured_error_trials": measured_error_trials,
            "measured_task_failures": measured_task_failures,
            "malformed_trial_records": malformed_trial_records,
            "selected_trial_model_requests": selected_requests,
            "unselected_trial_model_requests": unselected_requests,
            "selected_trial_cost_usd": str(cost),
            "missing_identities": [list(key) for key in missing],
            "duplicate_identities": [list(key) for key in duplicates],
            "unexpected_identities": [list(key) for key in extra],
        }
    complete = complete and observed.get("baseline") == expected and observed.get("candidate") == expected
    expected_events = {
        (f"{model_class}:{transport}", task_id, repeat_index, arm)
        for model_class, transport, task_id, repeat_index in expected
        for arm in ("baseline", "candidate")
    }
    event_keys: set[tuple[str, str, int, str]] = set()
    if len(paired_event_lists) == 2 and paired_event_lists[0] == paired_event_lists[1]:
        events = paired_event_lists[0]
        for event in events:
            cell = event.get("cell")
            task_id = event.get("task_id")
            repeat_index = event.get("repeat_index")
            event_arm = event.get("arm")
            sequence = event.get("sequence")
            order_position = event.get("order_position")
            if (
                isinstance(cell, str)
                and isinstance(task_id, str)
                and isinstance(repeat_index, int)
                and isinstance(event_arm, str)
                and isinstance(sequence, int)
                and isinstance(order_position, int)
                and order_position in {0, 1}
            ):
                event_keys.add((cell, task_id, repeat_index, event_arm))
        complete = complete and (
            paired_complete
            and len(events) == 2 * len(expected)
            and [event.get("sequence") for event in events] == list(range(1, 2 * len(expected) + 1))
            and event_keys == expected_events
        )
    else:
        complete = False
    return {
        "schema_version": 2,
        "work_item": WORK_ITEM,
        "selection": selection.artifact_metadata(),
        "status": "complete" if complete else "incomplete",
        "canonical_comparison_verdict": comparison.get("verdict", "inconclusive"),
        "adopt_eligible": False,
        "parent_results_merged": False,
        "arms": arms,
        "expected_paired_identities": len(expected),
        "expected_new_model_trials": 2 * len(expected),
        "observed_paired_execution_events": paired_events,
        "paired_execution_complete": paired_complete,
        "smoke_cells_per_arm": len(expected_smoke_cells),
        "observed_new_smoke_model_requests": smoke_requests,
        "unselected_trial_model_requests": sum(item["unselected_trial_model_requests"] for item in arms.values()),
        "new_execution_cost_limit_usd": str(NEW_EXECUTION_LIMIT_USD),
    }
