"""The explicitly approved AKB-361 tool-guidance regression sample."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import BenchmarkRunManifest, ContractModel, TaskManifest, Transport
from .evidence import validate_model_input_evidence
from .execution import TrialOutcome, has_measured_evidence, has_server_instructions_evidence
from .pairing import planned_arm_order

SelectionIdentity = tuple[str, str, str, int]
ParentLineage = Literal["previous"]
ParentName = Literal["previous_baseline", "previous_candidate"]

TOTAL_BUDGET_USD = Decimal("50")
PRIOR_SPEND_USD = Decimal("9.2804328442")
NEW_EXECUTION_LIMIT_USD = TOTAL_BUDGET_USD - PRIOR_SPEND_USD
PER_TRIAL_LIMIT_USD = Decimal("0.10")
REQUEST_TIMEOUT_SECONDS = 300
PAIRED_ORDER_MODE = "counterbalanced_task_repeat"
PAIRED_ORDER_SEED = "akb-358-v1"
APPROVED_AUTHORIZATION = {
    "uri": "akb://reef-akb/coll/issues/doc/akb-361.md",
    "revision": "41ff509113638ffe9ec0af1f803b42439be87405",  # pragma: allowlist secret
    "content_sha256": "b11b8f36d5c8fa36fe3eb6b453a56b612b15487f3d7b26b939d903c7b9f357a7",  # pragma: allowlist secret
}
APPROVED_PARENT_SHA256: dict[ParentName, str] = {
    "previous_baseline": "276e0dfdfdad58ce083f65b3ea0c21a22675f31d8a393995dace62b77eaa43a4",  # pragma: allowlist secret
    "previous_candidate": "3195db132b810dfd37b58a4e1d8169ba92dca965ffa5140ec5c2a4624f6d647c",  # pragma: allowlist secret
}

_ROLE_BY_IDENTITY: dict[SelectionIdentity, Literal["failure", "control"]] = {
    ("primary", "http", "collection-relation-lifecycle-ko", 2): "failure",
    ("primary", "http", "table-schema-conflict-en", 2): "failure",
    ("primary", "stdio", "stdio-image-files-ko", 1): "failure",
    ("primary", "stdio", "table-publication-en", 1): "failure",
    ("primary", "stdio", "table-publication-ko", 1): "failure",
    ("primary", "stdio", "table-schema-conflict-en", 1): "failure",
    ("primary", "http", "exhaustive-inspection-ko", 1): "control",
    ("primary", "http", "ambiguous-clarification-ko", 2): "control",
    ("primary", "http", "exhaustive-inspection-en", 2): "control",
    ("primary", "http", "ambiguous-clarification-en", 2): "control",
    ("primary", "stdio", "exhaustive-inspection-ko", 2): "control",
    ("primary", "stdio", "stdio-local-ko", 2): "control",
    ("primary", "stdio", "exhaustive-inspection-en", 2): "control",
    ("primary", "stdio", "read-vaults-en", 2): "control",
}


class AuthorizationPin(ContractModel):
    uri: str = Field(pattern=r"^akb://[a-z0-9][a-z0-9-]*/coll/issues/doc/[a-z0-9][a-z0-9-]*\.md$")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ParentArtifact(ContractModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SelectionBudget(ContractModel):
    session_max_usd: Decimal
    prior_conservative_spend_usd: Decimal
    new_execution_limit_usd: Decimal
    per_trial_usd: Decimal
    request_timeout_seconds: int


class SelectedTrial(ContractModel):
    model_class: Literal["primary", "lightweight"]
    transport: Transport
    task_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    repeat_index: int = Field(ge=1)
    role: Literal["failure", "control"]
    parent_lineage: ParentLineage

    @property
    def identity(self) -> SelectionIdentity:
        return (self.model_class, self.transport, self.task_id, self.repeat_index)


class SelectionDocument(ContractModel):
    schema_version: Literal[1]
    authorization: AuthorizationPin
    parents: dict[ParentName, ParentArtifact]
    selected_trials: list[SelectedTrial] = Field(min_length=14, max_length=14)
    budget: SelectionBudget

    @model_validator(mode="after")
    def validate_approved_selection(self) -> SelectionDocument:
        if self.authorization.model_dump(mode="json") != APPROVED_AUTHORIZATION:
            raise ValueError("selection authorization differs from the approved AKB-361 revision")
        if set(self.parents) != set(APPROVED_PARENT_SHA256):
            raise ValueError("selection must pin the approved previous baseline and candidate artifacts")
        if {name: parent.sha256 for name, parent in self.parents.items()} != APPROVED_PARENT_SHA256:
            raise ValueError("selection parent artifact hashes differ from the approved AKB-361 artifacts")
        identities = [trial.identity for trial in self.selected_trials]
        if len(set(identities)) != len(identities):
            raise ValueError("selection contains duplicate trial identities")
        if set(identities) != set(_ROLE_BY_IDENTITY):
            raise ValueError("selection identities differ from the approved AKB-361 sample")
        for trial in self.selected_trials:
            if trial.role != _ROLE_BY_IDENTITY[trial.identity] or trial.parent_lineage != "previous":
                raise ValueError("selection role or parent lineage differs from the approved sample")
        if Counter(trial.role for trial in self.selected_trials) != Counter(failure=6, control=8):
            raise ValueError("selection must contain six failures and eight controls")
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
    parent_results: dict[SelectionIdentity, dict[str, dict[str, Any]]]

    @property
    def identities(self) -> frozenset[SelectionIdentity]:
        return frozenset(trial.identity for trial in self.selected_trials)

    def contains(self, model_class: str, transport: str, task_id: str, repeat_index: int) -> bool:
        return (model_class, transport, task_id, repeat_index) in self.identities

    def artifact_metadata(self) -> dict[str, Any]:
        return {
            "purpose": "tool_guidance_regression",
            "authorization": self.authorization.model_dump(mode="json"),
            "selection_sha256": self.sha256,
            "parents": {name: dict(parent) for name, parent in sorted(self.parents.items())},
            "selected_trials": [trial.model_dump(mode="json") for trial in self.selected_trials],
            "selected_identity_count": len(self.selected_trials),
            "expected_model_trials": 2 * len(self.selected_trials),
            "budget": {
                "session_max_usd": str(self.budget.session_max_usd),
                "prior_conservative_spend_usd": str(self.budget.prior_conservative_spend_usd),
                "new_execution_limit_usd": str(self.budget.new_execution_limit_usd),
                "per_trial_usd": str(self.budget.per_trial_usd),
                "request_timeout_seconds": self.budget.request_timeout_seconds,
            },
        }

    def checkpoint_preflight(self) -> dict[str, Any]:
        """Return the immutable, read-only input validation recorded in checkpoints."""

        return {
            "read_only": True,
            "selection_path": str(self.path),
            "selection_sha256": self.sha256,
            "authorization": self.authorization.model_dump(mode="json"),
            "parents": {name: dict(parent) for name, parent in sorted(self.parents.items())},
            "parent_count": len(self.parents),
            "selected_trial_count": len(self.selected_trials),
            "role_counts": dict(sorted(Counter(trial.role for trial in self.selected_trials).items())),
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
        _validate_manifest(manifest, document.selected_trials, tasks, document.budget)
        parents = _validate_parents(document.parents, selection_directory=path.parent)
        return cls(
            path=path,
            sha256=selection_sha256,
            authorization=document.authorization,
            budget=document.budget,
            parents=parents,
            selected_trials=tuple(document.selected_trials),
            parent_results=_parent_results(parents, document.selected_trials),
        )


def _validate_manifest(
    manifest: BenchmarkRunManifest,
    selected_trials: list[SelectedTrial],
    tasks: list[TaskManifest],
    selection_budget: SelectionBudget,
) -> None:
    budget = manifest.budget
    if set(manifest.arms) != {"baseline", "candidate"} or set(manifest.transports) != {"http", "stdio"}:
        raise ValueError("run manifest does not contain the approved paired transports and arms")
    if manifest.paired_order != PAIRED_ORDER_MODE or manifest.paired_order_seed != PAIRED_ORDER_SEED:
        raise ValueError("run manifest does not preserve the approved paired arm order and seed")
    if (
        Decimal(str(budget.max_total_cost_usd)) != selection_budget.session_max_usd
        or Decimal(str(budget.max_cost_per_trial_usd)) != selection_budget.per_trial_usd
        or budget.request_timeout_seconds != selection_budget.request_timeout_seconds
    ):
        raise ValueError("run manifest differs from the approved tool-guidance measurement budget")
    expected_routes: dict[Literal["primary", "lightweight"], str] = {
        "primary": "streamlake",
        "lightweight": "akashml",
    }
    models = {model.class_name: model for model in manifest.models}
    if set(models) != set(expected_routes):
        raise ValueError("run manifest does not contain the approved model cells")
    for model_class, provider in expected_routes.items():
        route = models[model_class].routing
        if route.order != [provider] or route.allow_fallbacks:
            raise ValueError("run manifest does not use the approved fixed provider routes")
    task_by_id = {task.id: task for task in tasks}
    for trial in selected_trials:
        if trial.model_class not in models or trial.transport not in manifest.transports:
            raise ValueError("selection contains an unregistered model or transport")
        task = task_by_id.get(trial.task_id)
        if task is None or trial.transport not in task.fixture.transports:
            raise ValueError("selection contains a task outside its registered transport scope")
        if trial.repeat_index > manifest.repeats:
            raise ValueError("selection contains a repeat outside the registered run plan")


def _parent_rows(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    runs = artifact.get("runs")
    if not isinstance(runs, dict):
        raise ValueError("parent artifact has no run rows")
    rows: list[dict[str, Any]] = []
    for run in runs.values():
        if not isinstance(run, dict):
            raise ValueError("parent artifact contains a malformed run")
        trials = run.get("trials")
        if not isinstance(trials, list):
            raise ValueError("parent artifact contains no trial rows")
        if any(not isinstance(row, dict) for row in trials):
            raise ValueError("parent artifact contains a malformed trial row")
        rows.extend(trials)
    return rows


def _validate_parents(
    parents: dict[ParentName, ParentArtifact],
    *,
    selection_directory: Path,
) -> dict[str, dict[str, str]]:
    metadata: dict[str, dict[str, str]] = {}
    for name in APPROVED_PARENT_SHA256:
        parent = parents[name]
        try:
            parent_path = Path(parent.path).expanduser()
            if not parent_path.is_absolute():
                parent_path = selection_directory / parent_path
            parent_path = parent_path.resolve(strict=True)
            raw = parent_path.read_bytes()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{name} parent artifact is unavailable") from exc
        if hashlib.sha256(raw).hexdigest() != parent.sha256:
            raise ValueError(f"{name} parent artifact SHA-256 does not match the selection")
        try:
            artifact = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"{name} parent artifact is invalid JSON") from exc
        expected_arm = "baseline" if name.endswith("_baseline") else "candidate"
        if not isinstance(artifact, dict) or artifact.get("arm") != expected_arm:
            raise ValueError(f"{name} parent artifact has the wrong arm identity")
        _parent_rows(artifact)
        metadata[name] = {"path": str(parent_path), "sha256": parent.sha256}
    return metadata


def _parent_results(
    parents: dict[str, dict[str, str]],
    selected_trials: list[SelectedTrial],
) -> dict[SelectionIdentity, dict[str, dict[str, Any]]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for name, parent in parents.items():
        raw = Path(parent["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != parent["sha256"]:
            raise ValueError(f"{name} parent artifact changed while selection was loading")
        artifacts[name] = json.loads(raw)

    results: dict[SelectionIdentity, dict[str, dict[str, Any]]] = {}
    for trial in selected_trials:
        rows_by_arm: dict[str, dict[str, Any]] = {}
        for arm in ("baseline", "candidate"):
            name = f"{trial.parent_lineage}_{arm}"
            rows = [
                row
                for row in _parent_rows(artifacts[name])
                if all(
                    row.get(field) == value
                    for field, value in zip(
                        ("model_class", "transport", "task_id", "repeat_index"),
                        trial.identity,
                        strict=True,
                    )
                )
            ]
            if len(rows) != 1 or rows[0].get("arm") != arm:
                raise ValueError(f"{name} does not contain exactly one registered parent trial")
            rows_by_arm[arm] = rows[0]
        baseline, candidate = rows_by_arm["baseline"], rows_by_arm["candidate"]
        if trial.role == "failure":
            if candidate.get("success") is not False:
                raise ValueError("selected failure is not a prior candidate failure")
        elif baseline.get("success") is not True or candidate.get("success") is not True:
            raise ValueError("selected control was not successful in both pinned parent arms")
        results[trial.identity] = rows_by_arm
    return results


def _parent_metrics(row: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "success",
        "trial_error",
        "failure_kind",
        "first_action_accuracy",
        "first_material_action_accuracy",
        "argument_validity",
        "nonexistent_tool_attempts",
        "wrong_capability_calls",
        "model_requests",
        "input_tokens",
        "output_tokens",
        "cost_usd",
    )
    return {field: row.get(field) for field in fields}


def _trial_metrics(outcome: TrialOutcome, expected_instructions: str | None) -> dict[str, Any]:
    return {
        "success": outcome.success,
        "trial_error": outcome.trial_error,
        "failure_kind": outcome.failure_kind,
        "first_action_accuracy": outcome.first_action_accuracy,
        "first_material_action_accuracy": outcome.first_material_action_accuracy,
        "argument_validity": outcome.argument_validity,
        "nonexistent_tool_attempts": outcome.nonexistent_tool_attempts,
        "wrong_capability_calls": outcome.wrong_capability_calls,
        "model_requests": outcome.model_requests,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "cost_usd": outcome.cost_usd,
        "server_instructions_sha256": outcome.server_instructions_sha256,
        "model_input_requests": len(outcome.model_input_evidence),
        "server_instructions_delivered": has_server_instructions_evidence(outcome, expected_instructions),
    }


def build_selection_summary(
    selection: ToolGuidanceSelection,
    artifacts: dict[str, dict[str, Any]],
    comparison: dict[str, Any],
    *,
    manifest: BenchmarkRunManifest,
    tasks: list[TaskManifest],
) -> dict[str, Any]:
    expected = selection.identities
    expected_smoke_cells = [
        f"{model.class_name}:{transport}"
        for model in manifest.models
        for transport in manifest.transports
    ]
    arm_summaries: dict[str, Any] = {}
    observed: dict[str, set[SelectionIdentity]] = {}
    outcome_by_arm: dict[str, dict[SelectionIdentity, TrialOutcome]] = {}
    complete = comparison.get("verdict") == "inconclusive"
    smoke_requests = 0
    event_lists: list[list[dict[str, Any]]] = []
    paired_complete = True
    task_by_id = {task.id: task for task in tasks}
    model_ids: dict[str, str] = {model.class_name: model.model_id for model in manifest.models}

    for arm in ("baseline", "candidate"):
        artifact = artifacts.get(arm, {})
        complete = complete and artifact.get("selection") == selection.artifact_metadata()
        rows: dict[SelectionIdentity, TrialOutcome] = {}
        counts: Counter[SelectionIdentity] = Counter()
        all_ids: set[SelectionIdentity] = set()
        malformed = 0
        runs = artifact.get("runs", {})
        for run in runs.values() if isinstance(runs, dict) else []:
            for raw in run.get("trials", []) if isinstance(run, dict) else []:
                if not isinstance(raw, dict):
                    malformed += 1
                    continue
                identity_value = (
                    raw.get("model_class"),
                    raw.get("transport"),
                    raw.get("task_id"),
                    raw.get("repeat_index"),
                )
                if not (
                    isinstance(identity_value[0], str)
                    and isinstance(identity_value[1], str)
                    and isinstance(identity_value[2], str)
                    and isinstance(identity_value[3], int)
                ):
                    malformed += 1
                    continue
                identity = (identity_value[0], identity_value[1], identity_value[2], identity_value[3])
                all_ids.add(identity)
                counts[identity] += 1
                input_evidence_invalid = False
                try:
                    validate_model_input_evidence(raw)
                except ValueError:
                    malformed += 1
                    input_evidence_invalid = True
                try:
                    outcome = TrialOutcome.model_validate(raw)
                except ValueError:
                    if not input_evidence_invalid:
                        malformed += 1
                    continue
                if outcome.arm != arm:
                    malformed += 1
                    continue
                if outcome.model_id != model_ids.get(outcome.model_class):
                    malformed += 1
                    continue
                rows[identity] = outcome
        outcome_by_arm[arm] = rows
        observed[arm] = all_ids & expected
        missing = sorted(expected - observed[arm])
        duplicates = sorted(identity for identity in expected if counts[identity] > 1)
        extras = sorted(all_ids - expected)
        measured = sum(
            has_measured_evidence(outcome, task_by_id[outcome.task_id])
            for outcome in rows.values()
            if outcome.task_id in task_by_id
        )
        proof_missing = [
            identity
            for identity, outcome in rows.items()
            if not _instruction_proof_valid(outcome, artifact, task_by_id)
        ]
        arm_complete = (
            not missing
            and not duplicates
            and not extras
            and malformed == 0
            and measured == len(expected)
            and not proof_missing
        )
        complete = complete and arm_complete

        smoke_gate = artifact.get("smoke_gate", {})
        smoke = smoke_gate.get("cells", []) if isinstance(smoke_gate, dict) else []
        smoke_cells = [cell.get("cell") for cell in smoke if isinstance(cell, dict)]
        smoke_valid = (
            isinstance(smoke_gate, dict)
            and smoke_gate.get("status") == "passed"
            and smoke_gate.get("required_cells") == expected_smoke_cells
            and smoke_cells == expected_smoke_cells
        )
        for cell in smoke if isinstance(smoke, list) else []:
            raw_outcome = cell.get("outcome") if isinstance(cell, dict) else None
            if not isinstance(raw_outcome, dict):
                smoke_valid = False
                continue
            try:
                validate_model_input_evidence(raw_outcome)
            except ValueError:
                smoke_valid = False
            try:
                smoke_outcome = TrialOutcome.model_validate(raw_outcome)
            except ValueError:
                smoke_valid = False
                continue
            smoke_requests += smoke_outcome.model_requests
            smoke_valid = (
                smoke_valid
                and cell.get("status") == "completed"
                and _instruction_proof_valid(smoke_outcome, artifact, task_by_id)
            )
        complete = complete and smoke_valid

        execution = artifact.get("paired_execution", {})
        if isinstance(execution, dict):
            events = execution.get("events", [])
            if isinstance(events, list) and all(isinstance(event, dict) for event in events):
                event_lists.append(events)
            else:
                paired_complete = False
            paired_complete = paired_complete and execution.get("complete") is True
        else:
            paired_complete = False
        arm_summaries[arm] = {
            "artifact_status": artifact.get("status"),
            "observed_selected_trials": len(rows),
            "expected_selected_trials": len(expected),
            "missing_identities": [list(item) for item in missing],
            "duplicate_identities": [list(item) for item in duplicates],
            "unexpected_identities": [list(item) for item in extras],
            "malformed_trials": malformed,
            "missing_instruction_proof": [list(item) for item in proof_missing],
            "unselected_model_requests": sum(
                outcome.model_requests for identity, outcome in rows.items() if identity not in expected
            ),
            "smoke_gate_status": smoke_gate.get("status") if isinstance(smoke_gate, dict) else None,
        }

    expected_events = {
        (f"{model_class}:{transport}", task_id, repeat, arm)
        for model_class, transport, task_id, repeat in expected
        for arm in ("baseline", "candidate")
    }
    event_count = 0
    if len(event_lists) == 2 and event_lists[0] == event_lists[1]:
        events = event_lists[0]
        event_count = len(events)
        keys = {
            (event.get("cell"), event.get("task_id"), event.get("repeat_index"), event.get("arm"))
            for event in events
            if type(event.get("sequence")) is int
        }
        try:
            order_valid = all(
                type(event["repeat_index"]) is int
                and type(event["order_position"]) is int
                and event["order_position"]
                == planned_arm_order(
                    event["task_id"],
                    event["repeat_index"],
                    manifest.paired_order_seed,
                ).index(event["arm"])
                for event in events
            )
        except (KeyError, TypeError, ValueError):
            order_valid = False
        complete = complete and (
            paired_complete
            and len(events) == 2 * len(expected)
            and [event.get("sequence") for event in events] == list(range(1, len(events) + 1))
            and keys == expected_events
            and order_valid
        )
    else:
        complete = False

    comparisons: list[dict[str, Any]] = []
    for trial in selection.selected_trials:
        identity = trial.identity
        previous = selection.parent_results[identity]
        new_baseline = outcome_by_arm.get("baseline", {}).get(identity)
        new_candidate = outcome_by_arm.get("candidate", {}).get(identity)
        comparisons.append(
            {
                "identity": list(identity),
                "role": trial.role,
                "parent_lineage": trial.parent_lineage,
                "prior_baseline": _parent_metrics(previous["baseline"]),
                "prior_candidate": _parent_metrics(previous["candidate"]),
                "new_baseline": (
                    _trial_metrics(
                        new_baseline,
                        _sealed_instructions(artifacts.get("baseline", {}), new_baseline, task_by_id),
                    )
                    if new_baseline is not None
                    else None
                ),
                "new_candidate": (
                    _trial_metrics(
                        new_candidate,
                        _sealed_instructions(artifacts.get("candidate", {}), new_candidate, task_by_id),
                    )
                    if new_candidate is not None
                    else None
                ),
            }
        )
    total_trial_cost = sum(
        (
            Decimal(str(outcome.cost_usd))
            for outcomes in outcome_by_arm.values()
            for identity, outcome in outcomes.items()
            if identity in expected
        ),
        start=Decimal("0"),
    )
    return {
        "schema_version": 1,
        "selection": selection.artifact_metadata(),
        "status": "complete" if complete else "incomplete",
        "canonical_comparison_verdict": comparison.get("verdict", "inconclusive"),
        "adopt_eligible": False,
        "parent_results_merged": False,
        "arms": arm_summaries,
        "identity_comparisons": comparisons,
        "expected_paired_identities": len(expected),
        "expected_new_model_trials": 2 * len(expected),
        "observed_paired_execution_events": event_count,
        "paired_execution_complete": paired_complete,
        "smoke_cells_per_arm": len(expected_smoke_cells),
        "observed_new_smoke_model_requests": smoke_requests,
        "new_selected_trial_cost_usd": str(total_trial_cost),
        "new_execution_cost_limit_usd": str(selection.budget.new_execution_limit_usd),
        "unselected_model_requests": sum(
            summary["unselected_model_requests"] for summary in arm_summaries.values()
        ),
    }


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


def _instruction_proof_valid(
    outcome: TrialOutcome,
    artifact: dict[str, Any],
    task_by_id: dict[str, TaskManifest],
) -> bool:
    return has_server_instructions_evidence(
        outcome,
        _sealed_instructions(artifact, outcome, task_by_id),
    )
