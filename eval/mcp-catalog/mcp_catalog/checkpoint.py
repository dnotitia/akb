"""Atomic, credential-free checkpoint storage for resumable benchmark runs."""

from __future__ import annotations

import json
import os
import re
import tempfile
import contextlib
from pathlib import Path
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import Field

from .contracts import ArmName, ContractModel, TaskLocale, TaskManifest, hash_json
from .evidence import safe_json, validate_model_input_evidence
from .execution import TrialOutcome, has_measured_evidence, has_server_instructions_evidence


CHECKPOINT_SCHEMA_VERSION: Literal[4] = 4


class CheckpointError(ValueError):
    """Raised when a checkpoint cannot be trusted for an exact-input resume."""


class CheckpointHeader(ContractModel):
    schema_version: Literal[4] = CHECKPOINT_SCHEMA_VERSION
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_corpus_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm: ArmName
    pre_smoke_seal_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_registry_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CheckpointKey(ContractModel):
    """The complete identity of one benchmark trial."""

    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_corpus_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm: ArmName
    model_class: Literal["primary", "lightweight"]
    model_id: str = Field(min_length=1, max_length=200)
    transport: Literal["http", "stdio"]
    task_id: str = Field(min_length=1, max_length=100)
    repeat_index: int = Field(ge=1)
    locale: TaskLocale = "en-US"


class CheckpointBudget(ContractModel):
    model_requests: int = Field(default=0, ge=0)
    provider_setup_requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    wall_seconds: float = Field(default=0.0, ge=0)
    model_work_seconds: float = Field(default=0.0, ge=0)

    def plus(self, outcome: TrialOutcome) -> CheckpointBudget:
        from decimal import Decimal

        return CheckpointBudget(
            model_requests=self.model_requests + outcome.model_requests,
            provider_setup_requests=self.provider_setup_requests,
            input_tokens=self.input_tokens + outcome.input_tokens,
            output_tokens=self.output_tokens + outcome.output_tokens,
            cost_usd=float(Decimal(str(self.cost_usd)) + Decimal(str(outcome.cost_usd))),
            wall_seconds=self.wall_seconds,
            model_work_seconds=float(
                Decimal(str(self.model_work_seconds)) + Decimal(str(outcome.latency_seconds))
            ),
        )


class TimingBreakdown(ContractModel):
    provisioning: float = Field(default=0.0, ge=0)
    credential: float = Field(default=0.0, ge=0)
    catalog_capture: float = Field(default=0.0, ge=0)
    fixture_reset: float = Field(default=0.0, ge=0)
    provider_wait: float = Field(default=0.0, ge=0)
    model_execution: float = Field(default=0.0, ge=0)
    checkpoint: float = Field(default=0.0, ge=0)
    other: float = Field(default=0.0, ge=0)


class TimingAttempt(ContractModel):
    attempt_index: int = Field(ge=1)
    wall_seconds: float = Field(ge=0)
    breakdown: TimingBreakdown = Field(default_factory=TimingBreakdown)


class CheckpointTiming(ContractModel):
    attempts: list[TimingAttempt] = Field(default_factory=list)
    active_attempt: TimingAttempt | None = None
    cumulative_wall_seconds: float = Field(default=0.0, ge=0)
    cumulative_breakdown: TimingBreakdown = Field(default_factory=TimingBreakdown)


TrialStatus = Literal["completed", "failed", "incomplete"]
SmokeModelClass = Literal["primary", "lightweight"]
SmokeTransport = Literal["http", "stdio"]


class TrialCheckpoint(ContractModel):
    schema_version: Literal[4] = CHECKPOINT_SCHEMA_VERSION
    status: TrialStatus
    key: CheckpointKey
    outcome: TrialOutcome
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SmokeCellCheckpoint(ContractModel):
    schema_version: Literal[4] = CHECKPOINT_SCHEMA_VERSION
    status: TrialStatus
    model_class: SmokeModelClass
    model_id: str = Field(min_length=1, max_length=200)
    transport: SmokeTransport
    outcome: TrialOutcome
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CheckpointDocument(ContractModel):
    schema_version: Literal[4] = CHECKPOINT_SCHEMA_VERSION
    lifecycle: Literal["open", "closing", "finalized"] = "open"
    header: CheckpointHeader
    spent: CheckpointBudget = Field(default_factory=CheckpointBudget)
    reserved_cost_usd: float = Field(default=0.0, ge=0)
    reused_trial_count: int = Field(default=0, ge=0)
    rerun_trial_count: int = Field(default=0, ge=0)
    spent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    timing: CheckpointTiming = Field(default_factory=CheckpointTiming)
    timing_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: dict[str, TrialCheckpoint] = Field(default_factory=dict)
    smoke_status: Literal["in_progress", "passed", "failed"] | None = None
    smoke_gate: dict[str, SmokeCellCheckpoint] = Field(default_factory=dict)
    selection_preflight: dict[str, Any] | None = None
    provider_registry_snapshot: dict[str, Any] | None = None
    provider_registry_revalidation: list[dict[str, Any]] = Field(default_factory=list)
    continuation: dict[str, Any] | None = None


def checkpoint_key_digest(key: CheckpointKey) -> str:
    return hash_json(key.model_dump(mode="json"))


def checkpoint_record_hash(status: TrialStatus, key: CheckpointKey, outcome: TrialOutcome) -> str:
    return hash_json(
        {
            "status": status,
            "key": key.model_dump(mode="json"),
            "outcome": outcome.model_dump(mode="json"),
        }
    )


def smoke_record_hash(status: TrialStatus, model_class: str, model_id: str, transport: str, outcome: TrialOutcome) -> str:
    return hash_json(
        {
            "status": status,
            "model_class": model_class,
            "model_id": model_id,
            "transport": transport,
            "outcome": outcome.model_dump(mode="json"),
        }
    )


def spent_hash(spent: CheckpointBudget) -> str:
    return hash_json(spent.model_dump(mode="json"))


def timing_hash(timing: CheckpointTiming) -> str:
    return hash_json(timing.model_dump(mode="json"))


def provider_registry_snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    """Return and verify the digest of the exact credential-free registry snapshot."""

    if snapshot.get("status") == "verified":
        declared = snapshot.get("snapshot_hash")
        calculated = hash_json({key: value for key, value in snapshot.items() if key != "snapshot_hash"})
        if not isinstance(declared, str) or declared != calculated:
            raise CheckpointError("provider registry snapshot digest does not match its contents")
        return declared
    return hash_json(dict(snapshot))


def valid_completed_outcome(outcome: TrialOutcome, task: TaskManifest) -> bool:
    """Only measured successful or behavioral-failure trials are reusable."""

    return has_measured_evidence(outcome, task)


def valid_smoke_outcome(outcome: TrialOutcome, task: TaskManifest) -> bool:
    return smoke_outcome_failure_reason(outcome, task) is None


def smoke_outcome_failure_reason(
    outcome: TrialOutcome,
    task: TaskManifest,
    expected_instructions: str | None = None,
) -> str | None:
    if outcome.error is not None:
        return "contains an execution error"
    if not valid_completed_outcome(outcome, task):
        return "does not contain complete measured execution evidence"
    if not has_server_instructions_evidence(outcome, expected_instructions):
        return "does not prove the sealed server instructions reached every model request"
    if len(outcome.provider_evidence) != outcome.model_requests:
        return "does not contain one provider receipt per model request"
    if not all(_smoke_response_has_usage(item, outcome.model_id) for item in outcome.provider_evidence):
        return "contains a provider receipt without valid model usage"
    if outcome.successful_mcp_tool_calls <= 0:
        return "does not contain a successful MCP tool call"
    if outcome.model_requests < 2:
        return "does not contain a follow-up model request"
    if not outcome.follow_up_terminal_response:
        return "does not contain a follow-up terminal response"
    return None


def _smoke_response_has_usage(evidence: dict[str, object], model_id: str) -> bool:
    usage = evidence.get("usage")
    if evidence.get("model") != model_id or not isinstance(usage, dict):
        return False
    try:
        cost = Decimal(str(usage.get("cost")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not cost.is_finite() or cost <= 0:
        return False
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    return any(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (input_tokens, output_tokens)
    )


def status_for_outcome(outcome: TrialOutcome, task: TaskManifest) -> TrialStatus:
    return "completed" if valid_completed_outcome(outcome, task) else "failed"


class CheckpointStore:
    """Read and atomically rewrite a small JSON checkpoint after each outcome."""

    def __init__(
        self,
        path: Path,
        *,
        header: CheckpointHeader,
        expected_keys: dict[str, CheckpointKey],
        expected_tasks: Mapping[str, TaskManifest],
        expected_smoke_cells: Mapping[str, tuple[SmokeModelClass, str, SmokeTransport]],
        secrets: tuple[str, ...] = (),
        continuation: dict[str, Any] | None = None,
        selection_preflight: dict[str, Any] | None = None,
        resume: bool = False,
        reuse_terminal_trials: bool = False,
    ) -> None:
        self.path = path
        self.header = header
        self.expected_keys = expected_keys
        self.expected_tasks = dict(expected_tasks)
        if not self.expected_tasks or not {
            key.task_id for key in expected_keys.values()
        } <= set(self.expected_tasks):
            raise CheckpointError("checkpoint requires the registered task contracts")
        self.expected_smoke_cells = dict(expected_smoke_cells)
        self.secrets = secrets
        self.continuation = continuation
        self.selection_preflight = selection_preflight
        self.reuse_terminal_trials = reuse_terminal_trials
        self.resumed_from_finalized = False
        if resume:
            if not self.path.is_file():
                raise CheckpointError(f"resume checkpoint does not exist: {self.path}")
            self.document = self._load_existing()
            self.header = self.document.header
            self.resumed_from_finalized = self.document.lifecycle == "finalized"
        else:
            if self.path.exists():
                raise CheckpointError(f"checkpoint already exists; use --resume: {self.path}")
            initial_spent = CheckpointBudget()
            initial_timing = CheckpointTiming()
            self.document = CheckpointDocument(
                header=header,
                spent=initial_spent,
                spent_hash=spent_hash(initial_spent),
                timing=initial_timing,
                timing_hash=timing_hash(initial_timing),
                selection_preflight=selection_preflight,
                continuation=continuation,
            )
        if resume and self.document.lifecycle == "finalized":
            self.document.lifecycle = "open"
            self._write_atomic()

    def set_secrets(self, secrets: tuple[str, ...]) -> None:
        self.secrets = secrets

    def begin_closing(self) -> None:
        if self.document.lifecycle != "open":
            raise CheckpointError(f"checkpoint cannot begin closing from {self.document.lifecycle}")
        self.document.lifecycle = "closing"
        self._write_atomic()

    def set_reserved_cost(self, reserved_cost_usd: float) -> None:
        self._ensure_writable()
        if reserved_cost_usd < 0:
            raise CheckpointError("checkpoint reserved cost cannot be negative")
        self.document.reserved_cost_usd = reserved_cost_usd
        self._write_atomic()

    def set_run_counters(self, *, reused_trials: int, rerun_trials: int) -> None:
        self._ensure_writable()
        if reused_trials < 0 or rerun_trials < 0:
            raise CheckpointError("checkpoint run counters cannot be negative")
        self.document.reused_trial_count = reused_trials
        self.document.rerun_trial_count = rerun_trials
        self._write_atomic()

    def seed_continuation(self, document: CheckpointDocument) -> None:
        """Atomically initialize a new child checkpoint from verified parent evidence."""

        if self.continuation is None or document.continuation != self.continuation:
            raise CheckpointError("continuation checkpoint lineage is missing or does not match")
        if self.path.exists() or self.document.records or self.document.smoke_gate:
            raise CheckpointError("continuation checkpoint seed requires a new empty destination")
        if document.lifecycle != "open" or document.reserved_cost_usd != 0:
            raise CheckpointError("continuation checkpoint seed must be open with no reservation")
        if document.header.model_dump(exclude={"pre_smoke_seal_hash", "provider_registry_hash"}) != self.header.model_dump(
            exclude={"pre_smoke_seal_hash", "provider_registry_hash"}
        ):
            raise CheckpointError("continuation checkpoint header does not match this run")
        if (
            document.provider_registry_snapshot is None
            or document.header.provider_registry_hash != provider_registry_snapshot_hash(document.provider_registry_snapshot)
            or document.header.pre_smoke_seal_hash is None
        ):
            raise CheckpointError("continuation checkpoint is missing its frozen provider registry seal")
        if document.spent_hash != spent_hash(document.spent) or document.timing_hash != timing_hash(document.timing):
            raise CheckpointError("continuation checkpoint accounting digest does not match")
        for trial_record in document.records.values():
            self._validate_key(trial_record.key)
            if trial_record.record_hash != checkpoint_record_hash(
                trial_record.status,
                trial_record.key,
                trial_record.outcome,
            ):
                raise CheckpointError("continuation checkpoint source record digest does not match")
            if (
                trial_record.status == "completed"
                and not valid_completed_outcome(trial_record.outcome, self._task_for_outcome(trial_record.outcome))
            ):
                raise CheckpointError("continuation checkpoint contains an invalid completed trial")
        for cell_key, smoke_record in document.smoke_gate.items():
            if not self._smoke_cell_matches(cell_key, smoke_record):
                raise CheckpointError(f"continuation checkpoint contains an invalid smoke cell: {cell_key}")
            if smoke_record.record_hash != smoke_record_hash(
                smoke_record.status,
                smoke_record.model_class,
                smoke_record.model_id,
                smoke_record.transport,
                smoke_record.outcome,
            ):
                raise CheckpointError("continuation checkpoint smoke record digest does not match")
            if smoke_record.status == "completed" and not valid_smoke_outcome(
                smoke_record.outcome,
                self._task_for_outcome(smoke_record.outcome),
            ):
                raise CheckpointError(f"continuation checkpoint contains an invalid smoke cell: {cell_key}")
        if set(document.smoke_gate) != set(self.expected_smoke_cells) or document.smoke_status != "passed":
            raise CheckpointError("continuation checkpoint requires the complete parent smoke gate")
        self.header = document.header
        self.document = document
        self._write_atomic()

    def status_for(self, key: CheckpointKey) -> TrialStatus | None:
        record = self.document.records.get(checkpoint_key_digest(key))
        return record.status if record is not None else None

    def budget_failure_reason(self) -> str | None:
        terminal_markers = (
            "max_total_cost_usd",
            "preregistered worst-case trial cost",
        )
        outcomes = (
            *(record.outcome for record in self.document.records.values() if record.status != "completed"),
            *(record.outcome for record in self.document.smoke_gate.values() if record.status != "completed"),
        )
        return next(
            (
                outcome.error or "a prior checkpoint outcome exceeded the registered budget"
                for outcome in outcomes
                if outcome.failure_kind == "budget"
                and outcome.error is not None
                and any(marker in outcome.error.casefold() for marker in terminal_markers)
            ),
            None,
        )

    def completed_outcome_for(self, key: CheckpointKey) -> TrialOutcome | None:
        record = self.document.records.get(checkpoint_key_digest(key))
        if record is None or record.status != "completed":
            return None
        if not valid_completed_outcome(record.outcome, self._task_for_key(key)):
            raise CheckpointError("checkpoint contains an invalid completed trial")
        return record.outcome

    def reusable_outcome_for(self, key: CheckpointKey) -> TrialOutcome | None:
        """Return a prior result eligible for reuse in this run mode."""

        if self.continuation is None:
            if self.reuse_terminal_trials:
                record = self.document.records.get(checkpoint_key_digest(key))
                if record is None:
                    return None
                if (
                    record.status == "completed"
                    and (
                        not valid_completed_outcome(record.outcome, self._task_for_key(key))
                        or not has_server_instructions_evidence(record.outcome)
                    )
                ):
                    raise CheckpointError("selection checkpoint contains an invalid completed trial")
                return record.outcome
            record = self.document.records.get(checkpoint_key_digest(key))
            if record is None or record.status == "incomplete":
                return None
            if record.status == "completed" and not valid_completed_outcome(
                record.outcome,
                self._task_for_key(key),
            ):
                raise CheckpointError("checkpoint contains an invalid completed trial")
            return record.outcome
        record = self.document.records.get(checkpoint_key_digest(key))
        if record is None:
            return None
        if (
            record.status == "completed"
            and not valid_completed_outcome(record.outcome, self._task_for_key(key))
        ):
            raise CheckpointError("checkpoint contains an invalid completed trial")
        return record.outcome

    def smoke_outcome_for(self, cell_key: str) -> TrialOutcome | None:
        record = self.document.smoke_gate.get(cell_key)
        if record is None or record.status != "completed":
            return None
        if not self._smoke_cell_matches(cell_key, record):
            raise CheckpointError(f"checkpoint smoke cell identity does not match: {cell_key}")
        if not valid_smoke_outcome(record.outcome, self._task_for_outcome(record.outcome)):
            raise CheckpointError(f"checkpoint contains an invalid completed smoke cell: {cell_key}")
        return record.outcome

    def smoke_is_complete(self) -> bool:
        if self.document.smoke_status != "passed":
            return False
        return all(self.smoke_outcome_for(key) is not None for key in self.expected_smoke_cells)

    def record_trial(
        self,
        key: CheckpointKey,
        outcome: TrialOutcome,
        *,
        status: TrialStatus | None = None,
        timing: dict[str, object] | None = None,
        elapsed_wall_seconds: float | None = None,
        reserved_cost_usd: float = 0.0,
    ) -> bool:
        self._ensure_writable()
        self._ensure_provider_registry_sealed()
        if reserved_cost_usd < 0:
            raise CheckpointError("checkpoint reserved cost cannot be negative")
        self._validate_key(key)
        safe_outcome = self._safe_outcome(outcome)
        if not self._outcome_matches_key(safe_outcome, key):
            raise CheckpointError("trial outcome does not match its checkpoint key")
        task = self._task_for_key(key)
        resolved_status = status or status_for_outcome(safe_outcome, task)
        if resolved_status == "completed" and not valid_completed_outcome(safe_outcome, task):
            raise CheckpointError("a completed checkpoint trial must contain usage and routing evidence")
        digest = checkpoint_key_digest(key)
        existed = digest in self.document.records
        self.document.records[digest] = TrialCheckpoint(
            status=resolved_status,
            key=key,
            outcome=safe_outcome,
            record_hash=checkpoint_record_hash(resolved_status, key, safe_outcome),
        )
        self._recompute_spent()
        if elapsed_wall_seconds is not None:
            if elapsed_wall_seconds < 0:
                raise CheckpointError("checkpoint elapsed wall time cannot be negative")
            self.document.spent.wall_seconds = max(
                self.document.spent.wall_seconds,
                elapsed_wall_seconds,
            )
        self.document.spent_hash = spent_hash(self.document.spent)
        self.document.reserved_cost_usd = reserved_cost_usd
        if timing is not None:
            self._update_timing(timing)
        self._write_atomic()
        return existed

    def record_smoke_cell(
        self,
        cell_key: str,
        outcome: TrialOutcome,
        *,
        status: TrialStatus | None = None,
        timing: dict[str, object] | None = None,
        elapsed_wall_seconds: float | None = None,
        reserved_cost_usd: float = 0.0,
    ) -> bool:
        self._ensure_writable()
        self._ensure_provider_registry_sealed()
        if reserved_cost_usd < 0:
            raise CheckpointError("checkpoint reserved cost cannot be negative")
        expected = self.expected_smoke_cells.get(cell_key)
        if expected is None:
            raise CheckpointError(f"unexpected smoke cell: {cell_key}")
        model_class, model_id, transport = expected
        safe_outcome = self._safe_outcome(outcome)
        if (
            safe_outcome.arm != self.header.arm
            or safe_outcome.model_class != model_class
            or safe_outcome.model_id != model_id
            or safe_outcome.transport != transport
        ):
            raise CheckpointError("smoke outcome does not match its checkpoint cell")
        task = self._task_for_outcome(safe_outcome)
        resolved_status = status or status_for_outcome(safe_outcome, task)
        if resolved_status == "completed" and not valid_smoke_outcome(safe_outcome, task):
            raise CheckpointError("a completed smoke checkpoint must contain usage and routing evidence")
        existed = cell_key in self.document.smoke_gate
        self.document.smoke_status = "in_progress"
        self.document.smoke_gate[cell_key] = SmokeCellCheckpoint(
            status=resolved_status,
            model_class=model_class,
            model_id=model_id,
            transport=transport,
            outcome=safe_outcome,
            record_hash=smoke_record_hash(resolved_status, model_class, model_id, transport, safe_outcome),
        )
        self._recompute_spent()
        if elapsed_wall_seconds is not None:
            if elapsed_wall_seconds < 0:
                raise CheckpointError("checkpoint elapsed wall time cannot be negative")
            self.document.spent.wall_seconds = max(
                self.document.spent.wall_seconds,
                elapsed_wall_seconds,
            )
        self.document.spent_hash = spent_hash(self.document.spent)
        self.document.reserved_cost_usd = reserved_cost_usd
        if timing is not None:
            self._update_timing(timing)
        self._write_atomic()
        return existed

    def next_timing_attempt_index(self) -> int:
        indexes = [attempt.attempt_index for attempt in self.document.timing.attempts]
        if self.document.timing.active_attempt is not None:
            indexes.append(self.document.timing.active_attempt.attempt_index)
        return max(indexes, default=0) + 1

    def update_timing(self, timing: dict[str, object]) -> None:
        self._ensure_writable()
        self._update_timing(timing)
        self._write_atomic()

    def finalize_timing(self, timing: dict[str, object]) -> None:
        if self.document.lifecycle == "open":
            self.document.lifecycle = "closing"
        if self.document.lifecycle != "closing":
            raise CheckpointError(f"checkpoint cannot finalize from {self.document.lifecycle}")
        if self.document.reserved_cost_usd != 0:
            raise CheckpointError("checkpoint has an orphaned reserved cost")
        self._update_timing(timing)
        active = self.document.timing.active_attempt
        if active is not None:
            if any(item.attempt_index == active.attempt_index for item in self.document.timing.attempts):
                raise CheckpointError("checkpoint timing attempt is both active and finalized")
            self.document.timing.attempts.append(active)
            self.document.timing.active_attempt = None
            self._recompute_timing()
            self.document.timing_hash = timing_hash(self.document.timing)
        self.document.lifecycle = "finalized"
        self._write_atomic()

    def set_smoke_status(self, status: Literal["in_progress", "passed", "failed"]) -> None:
        self._ensure_writable()
        if status == "passed" and not all(
            self.smoke_outcome_for(key) is not None for key in self.expected_smoke_cells
        ):
            raise CheckpointError("cannot mark an incomplete smoke gate as passed")
        self.document.smoke_status = status
        self._write_atomic()

    def _load_existing(self) -> CheckpointDocument:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CheckpointError(f"cannot read resume checkpoint: {self.path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CheckpointError(f"resume checkpoint is invalid JSON: {exc}") from exc
        if safe_json(raw, self.secrets) != raw:
            raise CheckpointError("resume checkpoint contains secret-bearing values")
        try:
            validate_model_input_evidence(raw, self.secrets)
        except ValueError as exc:
            raise CheckpointError("resume checkpoint model input evidence is invalid") from exc
        try:
            document = CheckpointDocument.model_validate(raw)
        except Exception as exc:
            raise CheckpointError(f"resume checkpoint contract validation failed: {exc}") from exc
        if document.header.model_dump(exclude={"pre_smoke_seal_hash", "provider_registry_hash"}) != self.header.model_dump(
            exclude={"pre_smoke_seal_hash", "provider_registry_hash"}
        ):
            raise CheckpointError("resume checkpoint exact-input header does not match this run")
        if document.continuation != self.continuation:
            raise CheckpointError("resume checkpoint amendment lineage does not match this run")
        if document.selection_preflight != self.selection_preflight:
            raise CheckpointError("resume checkpoint selection preflight does not match this run")
        if document.provider_registry_snapshot is None:
            if document.header.provider_registry_hash is not None or document.header.pre_smoke_seal_hash is not None:
                raise CheckpointError("resume checkpoint is missing its sealed provider registry snapshot")
            if document.records or document.smoke_gate or document.smoke_status is not None:
                raise CheckpointError("resume checkpoint has paid evidence without a provider registry snapshot")
        else:
            if safe_json(document.provider_registry_snapshot, self.secrets) != document.provider_registry_snapshot:
                raise CheckpointError("resume checkpoint provider registry contains secret-bearing values")
            registry_hash = provider_registry_snapshot_hash(document.provider_registry_snapshot)
            if document.header.provider_registry_hash != registry_hash:
                raise CheckpointError("resume checkpoint provider registry hash does not match its snapshot")
            if document.header.pre_smoke_seal_hash is None and (
                document.records or document.smoke_gate or document.smoke_status is not None
            ):
                raise CheckpointError("resume checkpoint has paid evidence without its pre-smoke seal")
        for evidence in document.provider_registry_revalidation:
            snapshot = evidence.get("snapshot")
            if not isinstance(snapshot, dict) or safe_json(snapshot, self.secrets) != snapshot:
                raise CheckpointError("resume checkpoint provider registry revalidation is malformed")
            if evidence.get("snapshot_hash") != provider_registry_snapshot_hash(snapshot):
                raise CheckpointError("resume checkpoint provider registry revalidation hash does not match")
            invariant_sha256 = evidence.get("invariant_sha256")
            if invariant_sha256 is not None and (
                not isinstance(invariant_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", invariant_sha256)
            ):
                raise CheckpointError("resume checkpoint provider registry revalidation invariant hash is invalid")
        if document.spent_hash != spent_hash(document.spent):
            raise CheckpointError("resume checkpoint spent-usage digest does not match")
        if document.timing_hash != timing_hash(document.timing):
            raise CheckpointError("resume checkpoint timing digest does not match")
        for digest, trial_record in document.records.items():
            if digest != checkpoint_key_digest(trial_record.key):
                raise CheckpointError("resume checkpoint contains a tampered trial key digest")
            self._validate_key(trial_record.key)
            if trial_record.record_hash != checkpoint_record_hash(
                trial_record.status,
                trial_record.key,
                trial_record.outcome,
            ):
                raise CheckpointError("resume checkpoint trial record digest does not match")
            if not self._outcome_matches_key(trial_record.outcome, trial_record.key):
                raise CheckpointError("resume checkpoint trial outcome identity does not match its key")
            if (
                trial_record.status == "completed"
                and not valid_completed_outcome(trial_record.outcome, self._task_for_key(trial_record.key))
            ):
                raise CheckpointError("resume checkpoint contains an invalid completed trial")
        for cell_key, smoke_record in document.smoke_gate.items():
            if not self._smoke_cell_matches(cell_key, smoke_record):
                raise CheckpointError(f"resume checkpoint contains an invalid smoke cell: {cell_key}")
            if smoke_record.record_hash != smoke_record_hash(
                smoke_record.status,
                smoke_record.model_class,
                smoke_record.model_id,
                smoke_record.transport,
                smoke_record.outcome,
            ):
                raise CheckpointError("resume checkpoint smoke record digest does not match")
            if smoke_record.status == "completed" and not valid_smoke_outcome(
                smoke_record.outcome,
                self._task_for_outcome(smoke_record.outcome),
            ):
                raise CheckpointError(f"resume checkpoint contains an invalid completed smoke cell: {cell_key}")
        unknown_smoke = set(document.smoke_gate) - set(self.expected_smoke_cells)
        if unknown_smoke:
            raise CheckpointError(f"resume checkpoint contains unexpected smoke cells: {sorted(unknown_smoke)}")
        if document.lifecycle == "closing":
            raise CheckpointError("resume checkpoint was captured during finalization")
        if document.reserved_cost_usd != 0:
            raise CheckpointError("resume checkpoint contains an orphaned reserved cost")
        finalized_indexes = {item.attempt_index for item in document.timing.attempts}
        active = document.timing.active_attempt
        if document.lifecycle == "finalized" and active is not None:
            raise CheckpointError("finalized checkpoint cannot reopen an active timing attempt")
        if active is not None and active.attempt_index in finalized_indexes:
            raise CheckpointError("resume checkpoint timing attempt is both active and finalized")
        return document

    def bind_pre_smoke_seal(
        self,
        seal_hash: str,
        provider_registry_snapshot: dict[str, Any],
    ) -> None:
        """Bind or verify the exact pre-paid evidence seal before smoke requests."""

        self._ensure_writable()
        if len(seal_hash) != 64 or any(char not in "0123456789abcdef" for char in seal_hash):
            raise CheckpointError("pre-smoke seal must be a SHA-256 digest")
        if safe_json(provider_registry_snapshot, self.secrets) != provider_registry_snapshot:
            raise CheckpointError("provider registry snapshot contains secret-bearing values")
        registry_hash = provider_registry_snapshot_hash(provider_registry_snapshot)
        previous = self.document.header.pre_smoke_seal_hash
        if previous is not None and previous != seal_hash:
            raise CheckpointError("resume checkpoint pre-smoke seal does not match this run")
        previous_registry_hash = self.document.header.provider_registry_hash
        if previous_registry_hash is not None and previous_registry_hash != registry_hash:
            raise CheckpointError("resume checkpoint provider registry snapshot does not match this run")
        previous_snapshot = self.document.provider_registry_snapshot
        if previous_snapshot is not None and previous_snapshot != provider_registry_snapshot:
            raise CheckpointError("resume checkpoint provider registry snapshot changed")
        if previous is None and (self.document.records or self.document.smoke_gate or self.document.smoke_status is not None):
            raise CheckpointError("resume checkpoint with reusable trials is missing its pre-smoke seal")
        self.document.provider_registry_snapshot = provider_registry_snapshot
        self.header = self.header.model_copy(
            update={"pre_smoke_seal_hash": seal_hash, "provider_registry_hash": registry_hash}
        )
        self.document.header = self.header
        self._write_atomic()

    def record_provider_registry_revalidation(
        self,
        snapshot: dict[str, Any],
        *,
        invariant_sha256: str | None,
    ) -> None:
        """Keep fresh setup verification separate from the frozen run snapshot."""

        self._ensure_writable()
        if self.document.provider_registry_snapshot is None:
            raise CheckpointError("provider registry revalidation requires a frozen source snapshot")
        if safe_json(snapshot, self.secrets) != snapshot:
            raise CheckpointError("provider registry revalidation contains secret-bearing values")
        if invariant_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", invariant_sha256):
            raise CheckpointError("provider registry revalidation invariant hash is invalid")
        self.document.provider_registry_revalidation.append(
            {
                "snapshot": snapshot,
                "snapshot_hash": provider_registry_snapshot_hash(snapshot),
                "invariant_sha256": invariant_sha256,
            }
        )
        self._write_atomic()

    def record_provider_setup_requests(self, count: int) -> None:
        self._ensure_writable()
        if count < 0:
            raise CheckpointError("provider setup request count cannot be negative")
        self.document.spent.model_requests += count
        self.document.spent.provider_setup_requests += count
        self.document.spent_hash = spent_hash(self.document.spent)
        self._write_atomic()

    def _recompute_spent(self) -> None:
        """Rebuild usage from current records so a settled-row rewrite is idempotent."""

        previous = self.document.spent
        outcomes = [
            *(record.outcome for record in self.document.records.values()),
            *(record.outcome for record in self.document.smoke_gate.values()),
        ]
        setup_requests = previous.provider_setup_requests
        self.document.spent = CheckpointBudget(
            model_requests=setup_requests + sum(outcome.model_requests for outcome in outcomes),
            provider_setup_requests=setup_requests,
            input_tokens=sum(outcome.input_tokens for outcome in outcomes),
            output_tokens=sum(outcome.output_tokens for outcome in outcomes),
            cost_usd=float(sum((Decimal(str(outcome.cost_usd)) for outcome in outcomes), Decimal("0"))),
            wall_seconds=previous.wall_seconds,
            model_work_seconds=float(
                sum((Decimal(str(outcome.latency_seconds)) for outcome in outcomes), Decimal("0"))
            ),
        )
        self.document.spent_hash = spent_hash(self.document.spent)

    def _ensure_writable(self) -> None:
        if self.document.lifecycle != "open":
            raise CheckpointError(f"checkpoint is {self.document.lifecycle}; late writes are forbidden")

    def _ensure_provider_registry_sealed(self) -> None:
        if (
            self.document.provider_registry_snapshot is None
            or self.document.header.provider_registry_hash is None
            or self.document.header.pre_smoke_seal_hash is None
        ):
            raise CheckpointError("checkpoint cannot record paid evidence before sealing the provider registry")

    def _update_timing(self, raw_timing: dict[str, object]) -> None:
        attempt = TimingAttempt.model_validate(raw_timing)
        active = self.document.timing.active_attempt
        if active is not None and active.attempt_index != attempt.attempt_index:
            self.document.timing.attempts.append(active)
        self.document.timing.active_attempt = attempt
        self._recompute_timing()
        self.document.spent.wall_seconds = max(
            self.document.spent.wall_seconds,
            self.document.timing.cumulative_wall_seconds,
        )
        self.document.spent_hash = spent_hash(self.document.spent)
        self.document.timing_hash = timing_hash(self.document.timing)

    def _recompute_timing(self) -> None:
        attempts = [*self.document.timing.attempts]
        if self.document.timing.active_attempt is not None:
            attempts.append(self.document.timing.active_attempt)
        self.document.timing.cumulative_wall_seconds = sum(attempt.wall_seconds for attempt in attempts)
        self.document.timing.cumulative_breakdown = TimingBreakdown(
            **{
                name: sum(getattr(attempt.breakdown, name) for attempt in attempts)
                for name in TimingBreakdown.model_fields
            }
        )

    def _validate_key(self, key: CheckpointKey) -> None:
        expected = self.expected_keys.get(checkpoint_key_digest(key))
        if expected is None or expected != key:
            raise CheckpointError("resume checkpoint trial key is not an exact planned input")
        if key.source_revision != self.header.source_revision or key.run_manifest_hash != self.header.run_manifest_hash:
            raise CheckpointError("resume checkpoint trial key does not match its header")
        if key.task_corpus_hash != self.header.task_corpus_hash or key.arm != self.header.arm:
            raise CheckpointError("resume checkpoint trial key does not match its header")
        self._task_for_key(key)

    def _task_for_key(self, key: CheckpointKey) -> TaskManifest:
        task = self.expected_tasks.get(key.task_id)
        if task is None or task.locale != key.locale:
            raise CheckpointError("checkpoint trial key does not match its registered task")
        return task

    def _task_for_outcome(self, outcome: TrialOutcome) -> TaskManifest:
        task = self.expected_tasks.get(outcome.task_id)
        if task is None or task.locale != outcome.locale:
            raise CheckpointError("checkpoint outcome does not match its registered task")
        return task

    @staticmethod
    def _outcome_matches_key(outcome: TrialOutcome, key: CheckpointKey) -> bool:
        return (
            outcome.task_id == key.task_id
            and outcome.arm == key.arm
            and outcome.model_class == key.model_class
            and outcome.model_id == key.model_id
            and outcome.transport == key.transport
            and outcome.repeat_index == key.repeat_index
            and outcome.locale == key.locale
        )

    def _smoke_cell_matches(self, cell_key: str, record: SmokeCellCheckpoint) -> bool:
        expected = self.expected_smoke_cells.get(cell_key)
        return expected == (record.model_class, record.model_id, record.transport) and (
            record.outcome.model_class,
            record.outcome.model_id,
            record.outcome.transport,
        ) == expected

    def _safe_outcome(self, outcome: TrialOutcome) -> TrialOutcome:
        try:
            safe_outcome = safe_json(outcome.model_dump(mode="json"), self.secrets)
            validate_model_input_evidence(safe_outcome, self.secrets)
            return TrialOutcome.model_validate(safe_outcome)
        except Exception as exc:
            raise CheckpointError(f"trial outcome cannot be safely checkpointed: {exc}") from exc

    def _refresh_integrity(self) -> None:
        for digest, trial_record in self.document.records.items():
            trial_record.outcome = self._safe_outcome(trial_record.outcome)
            trial_record.record_hash = checkpoint_record_hash(
                trial_record.status,
                trial_record.key,
                trial_record.outcome,
            )
            if digest != checkpoint_key_digest(trial_record.key):
                raise CheckpointError("checkpoint trial key digest changed")
        for cell_key, smoke_record in self.document.smoke_gate.items():
            smoke_record.outcome = self._safe_outcome(smoke_record.outcome)
            smoke_record.record_hash = smoke_record_hash(
                smoke_record.status,
                smoke_record.model_class,
                smoke_record.model_id,
                smoke_record.transport,
                smoke_record.outcome,
            )
            if not self._smoke_cell_matches(cell_key, smoke_record):
                raise CheckpointError("checkpoint smoke cell identity changed")
        self.document.spent_hash = spent_hash(self.document.spent)

    def _write_atomic(self) -> None:
        self._refresh_integrity()
        safe = safe_json(self.document.model_dump(mode="json"), self.secrets)
        try:
            validate_model_input_evidence(safe, self.secrets)
        except ValueError as exc:
            raise CheckpointError("checkpoint model input evidence integrity validation failed") from exc
        encoded = json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        for secret in self.secrets:
            if secret and secret in encoded:
                raise CheckpointError("checkpoint redaction failed")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".mcp-catalog-checkpoint-",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name)
            raise
