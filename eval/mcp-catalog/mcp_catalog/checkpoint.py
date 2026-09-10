"""Atomic, credential-free checkpoint storage for resumable benchmark runs."""

from __future__ import annotations

import json
import os
import tempfile
import contextlib
from pathlib import Path
from collections.abc import Mapping
from typing import Literal

from pydantic import Field

from .contracts import ArmName, ContractModel, hash_json
from .evidence import safe_json
from .execution import TrialOutcome


CHECKPOINT_SCHEMA_VERSION: Literal[1] = 1


class CheckpointError(ValueError):
    """Raised when a checkpoint cannot be trusted for an exact-input resume."""


class CheckpointHeader(ContractModel):
    schema_version: Literal[1] = CHECKPOINT_SCHEMA_VERSION
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_corpus_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm: ArmName


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


class CheckpointBudget(ContractModel):
    model_requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    wall_seconds: float = Field(default=0.0, ge=0)

    def plus(self, outcome: TrialOutcome) -> CheckpointBudget:
        return CheckpointBudget(
            model_requests=self.model_requests + outcome.model_requests,
            input_tokens=self.input_tokens + outcome.input_tokens,
            output_tokens=self.output_tokens + outcome.output_tokens,
            cost_usd=self.cost_usd + outcome.cost_usd,
            wall_seconds=self.wall_seconds + outcome.latency_seconds,
        )


TrialStatus = Literal["completed", "failed", "incomplete"]
SmokeModelClass = Literal["primary", "lightweight"]
SmokeTransport = Literal["http", "stdio"]


class TrialCheckpoint(ContractModel):
    schema_version: Literal[1] = CHECKPOINT_SCHEMA_VERSION
    status: TrialStatus
    key: CheckpointKey
    outcome: TrialOutcome
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SmokeCellCheckpoint(ContractModel):
    schema_version: Literal[1] = CHECKPOINT_SCHEMA_VERSION
    status: TrialStatus
    model_class: SmokeModelClass
    model_id: str = Field(min_length=1, max_length=200)
    transport: SmokeTransport
    outcome: TrialOutcome
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CheckpointDocument(ContractModel):
    schema_version: Literal[1] = CHECKPOINT_SCHEMA_VERSION
    header: CheckpointHeader
    spent: CheckpointBudget = Field(default_factory=CheckpointBudget)
    spent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    records: dict[str, TrialCheckpoint] = Field(default_factory=dict)
    smoke_status: Literal["in_progress", "passed", "failed"] | None = None
    smoke_gate: dict[str, SmokeCellCheckpoint] = Field(default_factory=dict)


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


def valid_completed_outcome(outcome: TrialOutcome) -> bool:
    """Only a terminal provider response with usage/routing evidence is reusable."""

    return (
        outcome.error is None
        and outcome.model_requests > 0
        and outcome.total_tokens > 0
        and outcome.total_tokens == outcome.input_tokens + outcome.output_tokens
        and bool(outcome.provider_evidence)
        and outcome.routing_observed
        and outcome.routing_valid
    )


def valid_smoke_outcome(outcome: TrialOutcome) -> bool:
    return (
        valid_completed_outcome(outcome)
        and outcome.successful_mcp_tool_calls > 0
        and outcome.model_requests >= 2
        and outcome.follow_up_terminal_response
    )


def status_for_outcome(outcome: TrialOutcome) -> TrialStatus:
    return "completed" if valid_completed_outcome(outcome) else "failed"


class CheckpointStore:
    """Read and atomically rewrite a small JSON checkpoint after each outcome."""

    def __init__(
        self,
        path: Path,
        *,
        header: CheckpointHeader,
        expected_keys: dict[str, CheckpointKey],
        expected_smoke_cells: Mapping[str, tuple[SmokeModelClass, str, SmokeTransport]],
        secrets: tuple[str, ...] = (),
        resume: bool = False,
    ) -> None:
        self.path = path
        self.header = header
        self.expected_keys = expected_keys
        self.expected_smoke_cells = dict(expected_smoke_cells)
        self.secrets = secrets
        if resume:
            if not self.path.is_file():
                raise CheckpointError(f"resume checkpoint does not exist: {self.path}")
            self.document = self._load_existing()
        else:
            if self.path.exists():
                raise CheckpointError(f"checkpoint already exists; use --resume: {self.path}")
            initial_spent = CheckpointBudget()
            self.document = CheckpointDocument(header=header, spent=initial_spent, spent_hash=spent_hash(initial_spent))

    def set_secrets(self, secrets: tuple[str, ...]) -> None:
        self.secrets = secrets

    def status_for(self, key: CheckpointKey) -> TrialStatus | None:
        record = self.document.records.get(checkpoint_key_digest(key))
        return record.status if record is not None else None

    def completed_outcome_for(self, key: CheckpointKey) -> TrialOutcome | None:
        record = self.document.records.get(checkpoint_key_digest(key))
        if record is None or record.status != "completed":
            return None
        if not valid_completed_outcome(record.outcome):
            raise CheckpointError("checkpoint contains an invalid completed trial")
        return record.outcome

    def smoke_outcome_for(self, cell_key: str) -> TrialOutcome | None:
        record = self.document.smoke_gate.get(cell_key)
        if record is None or record.status != "completed":
            return None
        if not self._smoke_cell_matches(cell_key, record):
            raise CheckpointError(f"checkpoint smoke cell identity does not match: {cell_key}")
        if not valid_smoke_outcome(record.outcome):
            raise CheckpointError(f"checkpoint contains an invalid completed smoke cell: {cell_key}")
        return record.outcome

    def smoke_is_complete(self) -> bool:
        if self.document.smoke_status != "passed":
            return False
        return all(self.smoke_outcome_for(key) is not None for key in self.expected_smoke_cells)

    def record_trial(self, key: CheckpointKey, outcome: TrialOutcome, *, status: TrialStatus | None = None) -> bool:
        self._validate_key(key)
        safe_outcome = self._safe_outcome(outcome)
        if not self._outcome_matches_key(safe_outcome, key):
            raise CheckpointError("trial outcome does not match its checkpoint key")
        resolved_status = status or status_for_outcome(safe_outcome)
        if resolved_status == "completed" and not valid_completed_outcome(safe_outcome):
            raise CheckpointError("a completed checkpoint trial must contain usage and routing evidence")
        digest = checkpoint_key_digest(key)
        existed = digest in self.document.records
        self.document.records[digest] = TrialCheckpoint(
            status=resolved_status,
            key=key,
            outcome=safe_outcome,
            record_hash=checkpoint_record_hash(resolved_status, key, safe_outcome),
        )
        self.document.spent = self.document.spent.plus(safe_outcome)
        self.document.spent_hash = spent_hash(self.document.spent)
        self._write_atomic()
        return existed

    def record_smoke_cell(
        self,
        cell_key: str,
        outcome: TrialOutcome,
        *,
        status: TrialStatus | None = None,
    ) -> bool:
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
        resolved_status = status or status_for_outcome(safe_outcome)
        if resolved_status == "completed" and not valid_smoke_outcome(safe_outcome):
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
        self.document.spent = self.document.spent.plus(safe_outcome)
        self.document.spent_hash = spent_hash(self.document.spent)
        self._write_atomic()
        return existed

    def set_smoke_status(self, status: Literal["in_progress", "passed", "failed"]) -> None:
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
            document = CheckpointDocument.model_validate(raw)
        except Exception as exc:
            raise CheckpointError(f"resume checkpoint contract validation failed: {exc}") from exc
        if document.header != self.header:
            raise CheckpointError("resume checkpoint exact-input header does not match this run")
        if document.spent_hash != spent_hash(document.spent):
            raise CheckpointError("resume checkpoint spent-usage digest does not match")
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
            if trial_record.status == "completed" and not valid_completed_outcome(trial_record.outcome):
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
            if smoke_record.status == "completed" and not valid_smoke_outcome(smoke_record.outcome):
                raise CheckpointError(f"resume checkpoint contains an invalid completed smoke cell: {cell_key}")
        unknown_smoke = set(document.smoke_gate) - set(self.expected_smoke_cells)
        if unknown_smoke:
            raise CheckpointError(f"resume checkpoint contains unexpected smoke cells: {sorted(unknown_smoke)}")
        return document

    def _validate_key(self, key: CheckpointKey) -> None:
        expected = self.expected_keys.get(checkpoint_key_digest(key))
        if expected is None or expected != key:
            raise CheckpointError("resume checkpoint trial key is not an exact planned input")
        if key.source_revision != self.header.source_revision or key.run_manifest_hash != self.header.run_manifest_hash:
            raise CheckpointError("resume checkpoint trial key does not match its header")
        if key.task_corpus_hash != self.header.task_corpus_hash or key.arm != self.header.arm:
            raise CheckpointError("resume checkpoint trial key does not match its header")

    @staticmethod
    def _outcome_matches_key(outcome: TrialOutcome, key: CheckpointKey) -> bool:
        return (
            outcome.task_id == key.task_id
            and outcome.arm == key.arm
            and outcome.model_class == key.model_class
            and outcome.model_id == key.model_id
            and outcome.transport == key.transport
            and outcome.repeat_index == key.repeat_index
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
            return TrialOutcome.model_validate(
                safe_json(outcome.model_dump(mode="json"), self.secrets)
            )
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
