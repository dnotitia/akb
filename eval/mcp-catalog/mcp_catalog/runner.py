"""Benchmark orchestration and paired comparison."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import os
import platform
import sys
import time
from collections.abc import Mapping
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, cast

import httpx

from .catalog import capture_catalog, input_schemas_from_catalog
from .checkpoint import (
    CheckpointHeader,
    CheckpointDocument,
    CheckpointKey,
    CheckpointStore,
    SmokeModelClass,
    SmokeTransport,
    valid_completed_outcome,
    valid_smoke_outcome,
)
from .contracts import (
    ArmName,
    BenchmarkRunManifest,
    CatalogSnapshot,
    PublicOperation,
    TaskManifest,
    hash_json,
    load_run_manifest,
    load_task_corpus,
    load_tool_coverage,
    validate_public_catalog,
)
from .evidence import redact_exception, redact_text, serialize_report, write_json, safe_json
from .execution import (
    BudgetLedger,
    BudgetExceeded,
    TrialExecutor,
    TrialOutcome,
    build_model,
    evaluate_dataset,
    execute_smoke,
    summarize_outcomes,
    summarize_outcomes_by_locale,
    validate_model_configuration,
    worst_case_cost,
)
from .runtime import RuntimeContractError, RuntimeDescriptor, RuntimeFixture
from .statistics import InconclusiveBootstrap, paired_cluster_bca
from .timing import TimingCategory, TimingTracker


class NeedsUserInput(RuntimeError):
    """The run needs a user-provided provider, credential, or cost decision."""


class BenchmarkRunFailure(RuntimeError):
    """A post-preflight run failure with a safe partial artifact."""

    def __init__(
        self,
        primary_error: Exception,
        artifact: dict[str, Any],
        secrets: tuple[str, ...],
        cleanup_errors: tuple[Exception, ...],
    ) -> None:
        self.primary_error = primary_error
        self.artifact = artifact
        self.secrets = secrets
        self.cleanup_errors = cleanup_errors
        message = redact_exception(primary_error, secrets)
        if cleanup_errors:
            cleanup = ", ".join(redact_exception(error, secrets) for error in cleanup_errors)
            message = f"{message}; cleanup failed: {cleanup}"
        super().__init__(message)


def _normalize_task_group_error(error: Exception) -> Exception:
    """Expose the concrete staged failure hidden by a TaskGroup exception group."""
    leaves: list[BaseException] = []

    def collect(current: BaseException) -> None:
        if isinstance(current, BaseExceptionGroup):
            for child in current.exceptions:
                collect(child)
        else:
            leaves.append(current)

    collect(error)
    exceptions = [item for item in leaves if isinstance(item, Exception)]
    for candidate in exceptions:
        if getattr(candidate, "stage", None) == "model_request":
            return candidate
        message = str(candidate).casefold()
        if "provider request timeout" in message or "global wall deadline" in message:
            return candidate
    return exceptions[0] if exceptions else error


def _exception_stage(error: BaseException, fallback: str) -> str:
    stage = getattr(error, "stage", None)
    return stage if isinstance(stage, str) and stage else fallback


def _attach_cleanup_errors(
    primary: Exception,
    cleanup_errors: list[Exception],
    secrets: tuple[str, ...],
) -> None:
    for error in cleanup_errors:
        primary.add_note(f"cleanup failed: {redact_exception(error, secrets)}")


async def _collect_cleanup_errors(*actions: Callable[[], Awaitable[None]]) -> list[Exception]:
    errors: list[Exception] = []
    for action in actions:
        try:
            await action()
        except Exception as exc:
            errors.append(exc)
    return errors


def zero_evidence_failure_reason(outcomes: list[TrialOutcome]) -> str | None:
    """Reject a run dominated by failures before any model usage evidence."""

    if not outcomes:
        return None
    failures = [
        outcome
        for outcome in outcomes
        if outcome.error and outcome.model_requests == 0 and not outcome.provider_evidence
    ]
    if len(failures) * 2 <= len(outcomes):
        return None
    dominant_error, dominant_count = Counter(outcome.error for outcome in failures).most_common(1)[0]
    return (
        "benchmark incomplete: "
        f"{len(failures)}/{len(outcomes)} trials failed before model usage evidence; "
        f"dominant failure {dominant_error!r} ({dominant_count} trials)"
    )


@dataclass(slots=True)
class CredentialResolver:
    manifest: BenchmarkRunManifest
    descriptor: RuntimeDescriptor
    model_secrets: tuple[str, ...]
    tokens: dict[str, str] | None = None
    minted_tokens: list[tuple[str, str]] = field(default_factory=list)
    token_fixtures: dict[str, RuntimeFixture] = field(default_factory=dict)
    stale_minted_tokens: set[str] = field(default_factory=set)
    issued_tokens: set[str] = field(default_factory=set)

    def env_name_for(self, profile: str) -> str:
        configured = self.manifest.credential_profiles.get(profile)
        if configured:
            return configured
        if profile == "default" and self.descriptor.pat_env:
            return self.descriptor.pat_env
        raise NeedsUserInput(f"credential profile {profile!r} has no environment name")

    def token_for(self, profile: str) -> str:
        if self.tokens is not None and profile in self.tokens:
            return self.tokens[profile]
        env_name = self.env_name_for(profile)
        value = os.environ.get(env_name, "")
        if not value:
            raise NeedsUserInput(f"credential value for profile {profile!r} is not available in {env_name}")
        return value

    def secrets_for(self, profile: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.model_secrets, self.token_for(profile))))

    def required_profiles(self, tasks: list[TaskManifest]) -> list[str]:
        return sorted({task.fixture.credential_profile for task in tasks})

    async def prepare(self, fixture: RuntimeFixture, profiles: list[str]) -> None:
        self.tokens = {}
        self.minted_tokens.clear()
        self.token_fixtures.clear()
        self.stale_minted_tokens.clear()
        self.issued_tokens.clear()
        for profile in profiles:
            env_name = self.env_name_for(profile)
            value = os.environ.get(env_name, "")
            if value and not self.descriptor.benchmark_cells:
                self.tokens[profile] = value
                continue
            await self._mint(fixture, profile)

    def validate_inputs(self, profiles: list[str]) -> None:
        if self.descriptor.benchmark_cells and not self._login_credentials_available():
            raise NeedsUserInput(
                "isolated benchmark cells require runtime login environments "
                f"{self.descriptor.username_env}/{self.descriptor.password_env}"
            )
        for profile in profiles:
            env_name = self.manifest.credential_profiles.get(profile)
            if env_name and os.environ.get(env_name):
                continue
            if profile == "default" and self.descriptor.pat_env and os.environ.get(self.descriptor.pat_env):
                continue
            if self._login_credentials_available():
                continue
            if env_name:
                raise NeedsUserInput(
                    f"credential profile {profile!r} needs either its PAT environment {env_name} or "
                    f"runtime login environments {self.descriptor.username_env}/{self.descriptor.password_env}"
                )
            raise NeedsUserInput(f"credential profile {profile!r} has no runtime PAT environment")

    async def cleanup(self, fixture: RuntimeFixture) -> None:
        errors: list[Exception] = []
        remaining: list[tuple[str, str]] = []
        for token, token_id in self.minted_tokens:
            token_fixture = self.token_fixtures.get(token, fixture)
            try:
                await token_fixture.revoke_pat(
                    token,
                    token_id,
                    allow_absent=token in self.stale_minted_tokens,
                )
            except Exception as exc:
                errors.append(exc)
                remaining.append((token, token_id))
            else:
                self.token_fixtures.pop(token, None)
        self.minted_tokens = remaining
        if errors:
            primary = errors[0]
            for error in errors[1:]:
                primary.add_note(
                    "additional PAT cleanup failure: "
                    f"{redact_exception(error, self.secret_values())}"
                )
            raise primary

    def mark_reset_complete(self) -> None:
        self.stale_minted_tokens.update(token for token, _token_id in self.minted_tokens)

    async def refresh_after_reset(self, fixture: RuntimeFixture, profile: str) -> str:
        self.mark_reset_complete()
        if not self._login_credentials_available():
            raise NeedsUserInput(
                f"credential profile {profile!r} cannot refresh after fixture reset; "
                f"runtime login environments {self.descriptor.username_env}/{self.descriptor.password_env} are required"
            )
        await self._mint(fixture, profile)
        return self.token_for(profile)

    async def _mint(self, fixture: RuntimeFixture, profile: str) -> None:
        scopes: list[str] | None
        if profile == "authorization":
            discovery = await fixture.discover()
            actors = discovery.get("actors")
            reader = actors.get("reader") if isinstance(actors, dict) else None
            username = reader.get("username") if isinstance(reader, dict) else None
            password = os.environ.get(self.descriptor.password_env, "")
            if not isinstance(username, str) or not username or not password:
                raise RuntimeContractError(
                    "authorization credential fixture does not expose the seeded reader actor",
                    stage="credential_login",
                )
            # The reader ACL is what denies the write; both coarse scopes keep
            # preparation reads from failing at the MCP scope gate first.
            scopes = ["read", "write"]
        else:
            username, password = self._login_credentials(profile)
            scopes = ["read"] if profile == "read_only" else None
        token, token_id = await fixture.mint_pat(username, password, scopes=scopes)
        assert self.tokens is not None
        self.tokens[profile] = token
        self.minted_tokens.append((token, token_id))
        self.token_fixtures[token] = fixture
        self.issued_tokens.add(token)

    def _login_credentials_available(self) -> bool:
        return bool(
            self.descriptor.username_env
            and self.descriptor.password_env
            and os.environ.get(self.descriptor.username_env)
            and os.environ.get(self.descriptor.password_env)
        )

    def _login_credentials(self, profile: str) -> tuple[str, str]:
        if not self._login_credentials_available():
            raise NeedsUserInput(
                f"credential profile {profile!r} needs either its PAT environment or "
                f"runtime login environments {self.descriptor.username_env}/{self.descriptor.password_env}"
            )
        assert self.descriptor.username_env is not None
        assert self.descriptor.password_env is not None
        username = os.environ[self.descriptor.username_env]
        password = os.environ[self.descriptor.password_env]
        return username, password

    def secret_values(self) -> tuple[str, ...]:
        values = list(self.model_secrets)
        if self.tokens is not None:
            values.extend(self.tokens.values())
        values.extend(self.issued_tokens)
        for env_name in (
            self.descriptor.username_env,
            self.descriptor.password_env,
            self.descriptor.pat_env,
        ):
            if env_name:
                value = os.environ.get(env_name)
                if value:
                    values.append(value)
        return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class PairedExecutionTurn:
    cell: str
    task_id: str
    repeat_index: int
    arm: ArmName
    order_position: int
    execution_sequence: int


class PairedArmCoordinator:
    """Gate real trial execution by the preregistered per-cell arm order."""

    def __init__(self, manifest: BenchmarkRunManifest, tasks: list[TaskManifest]) -> None:
        self.manifest = manifest
        self.tasks = tasks
        self._schedule: dict[str, list[tuple[str, int, ArmName]]] = {}
        for model in manifest.models:
            for transport in manifest.transports:
                cell = f"{model.class_name}:{transport}"
                self._schedule[cell] = [
                    (task.id, repeat_index, arm)
                    for repeat_index in range(1, manifest.repeats + 1)
                    for task in tasks
                    if transport in task.fixture.transports
                    for arm in planned_arm_order(task.id, repeat_index, manifest.paired_order_seed)
                ]
        self._positions = {cell: 0 for cell in self._schedule}
        self._registered: set[ArmName] = set()
        self._reused: dict[ArmName, set[tuple[str, str, int]]] = {
            "baseline": set(),
            "candidate": set(),
        }
        self._events: list[dict[str, Any]] = []
        self._sequence = 0
        self._condition = asyncio.Condition()
        self._provider_registry: dict[str, Any] | None = None
        self._provider_registry_lock = asyncio.Lock()
        self._pre_smoke_seals: dict[ArmName, str] = {}
        self._pre_smoke_ready: set[ArmName] = set()

    async def register_arm(
        self,
        arm: ArmName,
        reused: Mapping[tuple[str, str, int], TrialOutcome],
    ) -> None:
        async with self._condition:
            if arm in self._registered:
                raise RuntimeContractError(f"paired arm {arm} registered twice")
            for identity, outcome in reused.items():
                cell, task_id, repeat_index = identity
                expected = planned_arm_order(task_id, repeat_index, self.manifest.paired_order_seed)
                if outcome.paired_order_position != expected.index(arm):
                    raise RuntimeContractError(
                        f"paired resume evidence is missing or invalid for {cell}:{task_id}:{repeat_index}:{arm}"
                    )
                self._reused[arm].add(identity)
            self._registered.add(arm)
            if self._registered == {"baseline", "candidate"}:
                self._validate_reused_prefixes()
                for cell in self._schedule:
                    self._advance_reused(cell)
            self._condition.notify_all()

    def _validate_reused_prefixes(self) -> None:
        for cell, schedule in self._schedule.items():
            gap_seen = False
            for task_id, repeat_index, arm in schedule:
                reused = (cell, task_id, repeat_index) in self._reused[arm]
                if not reused:
                    gap_seen = True
                elif gap_seen:
                    raise RuntimeContractError(
                        f"paired resume checkpoint order is not a completed prefix for {cell}"
                    )

    async def wait_until_registered(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._registered == {"baseline", "candidate"})

    async def shared_provider_registry(
        self,
        loader: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        async with self._provider_registry_lock:
            if self._provider_registry is None:
                self._provider_registry = await loader()
            return dict(self._provider_registry)

    async def register_pre_smoke_seal(self, arm: ArmName, seal_hash: str) -> str:
        async with self._condition:
            previous = self._pre_smoke_seals.get(arm)
            if previous is not None and previous != seal_hash:
                raise RuntimeContractError("paired arm changed its pre-smoke seal during resume")
            self._pre_smoke_seals[arm] = seal_hash
            self._condition.notify_all()
            await self._condition.wait_for(lambda: set(self._pre_smoke_seals) == {"baseline", "candidate"})
            return hash_json(dict(sorted(self._pre_smoke_seals.items())))

    async def mark_pre_smoke_ready(self, arm: ArmName) -> None:
        async with self._condition:
            self._pre_smoke_ready.add(arm)
            self._condition.notify_all()
            await self._condition.wait_for(lambda: self._pre_smoke_ready == {"baseline", "candidate"})

    def _advance_reused(self, cell: str) -> None:
        schedule = self._schedule[cell]
        while self._positions[cell] < len(schedule):
            task_id, repeat_index, arm = schedule[self._positions[cell]]
            if (cell, task_id, repeat_index) not in self._reused[arm]:
                break
            self._positions[cell] += 1

    @asynccontextmanager
    async def turn(
        self,
        *,
        cell: str,
        task_id: str,
        repeat_index: int,
        arm: ArmName,
    ) -> AsyncIterator[PairedExecutionTurn]:
        async with self._condition:
            await self._condition.wait_for(lambda: self._registered == {"baseline", "candidate"})
            self._advance_reused(cell)
            expected_position = planned_arm_order(
                task_id,
                repeat_index,
                self.manifest.paired_order_seed,
            ).index(arm)
            await self._condition.wait_for(
                lambda: self._positions[cell] < len(self._schedule[cell])
                and self._schedule[cell][self._positions[cell]] == (task_id, repeat_index, arm)
            )
            self._sequence += 1
            turn = PairedExecutionTurn(
                cell=cell,
                task_id=task_id,
                repeat_index=repeat_index,
                arm=arm,
                order_position=expected_position,
                execution_sequence=self._sequence,
            )
            self._events.append(
                {
                    "sequence": turn.execution_sequence,
                    "cell": cell,
                    "task_id": task_id,
                    "repeat_index": repeat_index,
                    "arm": arm,
                    "order_position": expected_position,
                }
            )
        completed = False
        try:
            yield turn
            completed = True
        finally:
            async with self._condition:
                if completed:
                    self._positions[cell] += 1
                    self._advance_reused(cell)
                self._condition.notify_all()

    def evidence(self) -> dict[str, Any]:
        complete = all(
            position == len(self._schedule[cell])
            for cell, position in self._positions.items()
        )
        return {
            "mode": "counterbalanced_task_repeat",
            "seed": self.manifest.paired_order_seed,
            "complete": complete,
            "events": list(self._events),
            "reused": [
                {
                    "cell": cell,
                    "task_id": task_id,
                    "repeat_index": repeat_index,
                    "arm": arm,
                    "order_position": planned_arm_order(
                        task_id,
                        repeat_index,
                        self.manifest.paired_order_seed,
                    ).index(arm),
                }
                for arm in (cast(ArmName, "baseline"), cast(ArmName, "candidate"))
                for cell, task_id, repeat_index in sorted(self._reused[arm])
            ],
        }


@asynccontextmanager
async def _uncoordinated_turn() -> AsyncIterator[None]:
    yield None


class BenchmarkRunner:
    def __init__(
        self,
        manifest: BenchmarkRunManifest,
        tasks: list[TaskManifest],
        descriptor: RuntimeDescriptor,
        *,
        arm: str = "baseline",
        checkpoint_path: Path | None = None,
        resume_path: Path | None = None,
        paired_coordinator: PairedArmCoordinator | None = None,
        shared_ledger: BudgetLedger | None = None,
    ) -> None:
        if checkpoint_path is not None and resume_path is not None and checkpoint_path != resume_path:
            raise ValueError("--checkpoint and --resume must reference the same path")
        self.manifest = manifest
        self.tasks = tasks
        self.descriptor = descriptor
        self.arm = arm
        self.checkpoint_path = checkpoint_path or resume_path
        self.resume_path = resume_path
        self.paired_coordinator = paired_coordinator
        self.shared_ledger = shared_ledger
        self.secrets: tuple[str, ...] = ()
        self._completed_trials: dict[str, list[TrialOutcome]] = defaultdict(list)
        self._checkpoint_store: CheckpointStore | None = None
        self._checkpoint_new_trials = 0
        self._checkpoint_reused_trials = 0
        self._checkpoint_rerun_trials = 0
        self._checkpoint_lock = asyncio.Lock()
        self._timing: TimingTracker | None = None
        self._timing_attempt_index = 1
        self._resolver: CredentialResolver | None = None
        self._ledger: BudgetLedger | None = None
        self._smoke_gate: dict[str, Any] = {
            "status": "not_run",
            "required_cells": [],
            "cells": [],
        }
        self.provider_registry: dict[str, Any] = {"status": "unavailable", "reason": "not captured"}
        self.pre_smoke_seal_inputs: dict[str, Any] = {}
        self.pre_smoke_seal_hash: str | None = None
        self._provider_registry_reason: str | None = None

    def _refresh_secrets(self, resolver: CredentialResolver) -> None:
        self.secrets = resolver.secret_values()

    def _cell_descriptors(self) -> dict[str, RuntimeDescriptor]:
        keys = [
            f"{model.class_name}:{transport}"
            for model in self.manifest.models
            for transport in self.manifest.transports
        ]
        if not self.descriptor.benchmark_cells:
            raise RuntimeContractError("benchmark runtime must expose isolated benchmark_cells")
        if set(self.descriptor.benchmark_cells) != set(keys):
            raise RuntimeContractError("runtime benchmark cells do not match the registered model/transport matrix")
        return {key: self.descriptor.cell_for(key) for key in keys}

    def _timing_snapshot(self) -> dict[str, object] | None:
        if self._timing is None:
            return None
        return self._timing.snapshot(attempt_index=self._timing_attempt_index)

    async def _refresh_token(self, fixture: RuntimeFixture, profile: str) -> str:
        if self._resolver is None:
            raise RuntimeContractError("credential resolver is not initialized")
        if self._timing is None:
            return await self._resolver.refresh_after_reset(fixture, profile)
        with self._timing.measure("credential"):
            return await self._resolver.refresh_after_reset(fixture, profile)

    @staticmethod
    def _attach_timing(fixture: RuntimeFixture, timing: TimingTracker) -> None:
        fixture.timing_sink = lambda category, started, finished: timing.record(
            cast(TimingCategory, category), started, finished
        )

    def _record_trial(self, key: str, outcome: TrialOutcome) -> None:
        trials = self._completed_trials[key]
        for index, existing in enumerate(trials):
            if (
                existing.task_id == outcome.task_id
                and existing.repeat_index == outcome.repeat_index
                and existing.model_class == outcome.model_class
                and existing.transport == outcome.transport
                and existing.locale == outcome.locale
            ):
                trials[index] = outcome
                return
        trials.append(outcome)

    def _planned_keys(self, source_revision: str) -> dict[str, CheckpointKey]:
        manifest_hash = hash_json(self.manifest.model_dump(mode="json"))
        corpus_hash = hash_json([task.model_dump(mode="json") for task in self.tasks])
        planned: dict[str, CheckpointKey] = {}
        for model_spec in self.manifest.models:
            for transport in self.manifest.transports:
                for task in self.tasks:
                    if transport not in task.fixture.transports:
                        continue
                    for repeat_index in range(1, self.manifest.repeats + 1):
                        key = CheckpointKey(
                            source_revision=source_revision,
                            run_manifest_hash=manifest_hash,
                            task_corpus_hash=corpus_hash,
                            arm=cast(ArmName, self.arm),
                            model_class=model_spec.class_name,
                            model_id=model_spec.model_id,
                            transport=transport,
                            task_id=task.id,
                            repeat_index=repeat_index,
                            locale=task.locale,
                        )
                        planned[hash_json(key.model_dump(mode="json"))] = key
        return planned

    def _checkpoint_store_for(
        self,
        *,
        source_revision: str,
        resolver: CredentialResolver,
        planned_keys: dict[str, CheckpointKey],
    ) -> CheckpointStore | None:
        if self.checkpoint_path is None:
            return None
        header = CheckpointHeader(
            source_revision=source_revision,
            run_manifest_hash=hash_json(self.manifest.model_dump(mode="json")),
            task_corpus_hash=hash_json([task.model_dump(mode="json") for task in self.tasks]),
            arm=cast(ArmName, self.arm),
        )
        expected_smoke: dict[str, tuple[SmokeModelClass, str, SmokeTransport]] = {
            f"{model.class_name}:{transport}": (model.class_name, model.model_id, transport)
            for model in self.manifest.models
            for transport in self.manifest.transports
        }
        return CheckpointStore(
            self.checkpoint_path,
            header=header,
            expected_keys=planned_keys,
            expected_smoke_cells=expected_smoke,
            secrets=resolver.secret_values(),
            resume=self.resume_path is not None,
        )

    @staticmethod
    def _trial_identity(outcome: TrialOutcome) -> tuple[str, str, str, int]:
        return (outcome.model_class, outcome.transport, outcome.task_id, outcome.repeat_index)

    def _planned_trials_by_run(
        self,
        planned_keys: dict[str, CheckpointKey],
    ) -> dict[str, list[tuple[TaskManifest, int, CheckpointKey]]]:
        by_run: dict[str, list[tuple[TaskManifest, int, CheckpointKey]]] = defaultdict(list)
        for key in planned_keys.values():
            task = next(task for task in self.tasks if task.id == key.task_id)
            by_run[f"{key.model_class}:{key.transport}"].append((task, key.repeat_index, key))
        for trials in by_run.values():
            trials.sort(key=lambda item: (item[1], self.tasks.index(item[0])))
        return dict(by_run)

    def _ordered_outcomes(self, outcomes: list[TrialOutcome]) -> list[TrialOutcome]:
        task_order = {task.id: index for index, task in enumerate(self.tasks)}
        return sorted(
            outcomes,
            key=lambda outcome: (task_order.get(outcome.task_id, len(task_order)), outcome.repeat_index),
        )

    def _build_artifact(
        self,
        *,
        runtime: dict[str, Any],
        artifact_versions: dict[str, str],
        ledger: BudgetLedger,
        catalogs: dict[str, Any],
        reports: dict[str, Any],
        incomplete_reasons: set[str],
        failure: Exception | None = None,
        failure_stage: str | None = None,
        cleanup_errors: list[Exception] | None = None,
        fixture: RuntimeFixture | None = None,
        fixtures: Mapping[str, RuntimeFixture] | None = None,
        end_to_end_wall_seconds: float = 0.0,
        timing: dict[str, Any] | None = None,
        smoke_gate: dict[str, Any] | None = None,
        checkpoint_snapshot: CheckpointDocument | None = None,
    ) -> dict[str, Any]:
        snapshot_trials: dict[str, list[TrialOutcome]] = defaultdict(list)
        snapshot_statuses: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if checkpoint_snapshot is not None:
            for record in checkpoint_snapshot.records.values():
                run_key = f"{record.key.model_class}:{record.key.transport}"
                snapshot_trials[run_key].append(record.outcome)
                snapshot_statuses[run_key].append(
                    {
                        "task_id": record.key.task_id,
                        "repeat_index": record.key.repeat_index,
                        "status": record.status,
                    }
                )
            source_trials: Mapping[str, list[TrialOutcome]] = snapshot_trials
        else:
            source_trials = self._completed_trials
        quality_reasons: list[str] = []
        trial_error_reasons: list[str] = []
        for outcomes in source_trials.values():
            if reason := zero_evidence_failure_reason(outcomes):
                incomplete_reasons.add(reason)
                quality_reasons.append(reason)
        for run_key, outcomes in source_trials.items():
            for outcome in outcomes:
                if outcome.error and not valid_completed_outcome(outcome):
                    reason = (
                        f"benchmark incomplete: {run_key} trial {outcome.task_id} "
                        f"repeat {outcome.repeat_index} failed: {redact_text(outcome.error, self.secrets)}"
                    )
                    incomplete_reasons.add(reason)
                    trial_error_reasons.append(reason)
        all_reports: dict[str, Any] = {}
        for key in sorted(set(source_trials) | set(reports)):
            outcomes = source_trials.get(key, [])
            ordered = self._ordered_outcomes(outcomes)
            existing = reports.get(key, {})
            existing = existing if isinstance(existing, dict) else {}
            all_reports[key] = {
                "catalog_keys": existing.get("catalog_keys", []),
                "report": existing.get("report"),
                "trials": [safe_json(outcome.model_dump(mode="json"), self.secrets) for outcome in ordered],
                "statuses": sorted(snapshot_statuses.get(key, []), key=lambda item: (item["task_id"], item["repeat_index"])),
                "summary": summarize_outcomes(ordered),
                "locale_metrics": summarize_outcomes_by_locale(ordered),
                "partial": bool(existing.get("partial", False)) or failure is not None,
            }
        completed_trials = sum(len(outcomes) for outcomes in source_trials.values())
        all_outcomes = [outcome for outcomes in source_trials.values() for outcome in outcomes]
        expected_trials = sum(
            1
            for model_spec in self.manifest.models
            for transport in self.manifest.transports
            for task in self.tasks
            if transport in task.fixture.transports
            for _repeat_index in range(1, self.manifest.repeats + 1)
        )
        if completed_trials != expected_trials:
            incomplete_reasons.add(
                f"benchmark incomplete: completed {completed_trials}/{expected_trials} planned trials"
            )
        safe_runtime = safe_json(runtime["discovery"], self.secrets)
        reset_evidence: dict[str, Any]
        if fixtures:
            reset_evidence = {
                "count": sum(item.reset_evidence()["count"] for item in fixtures.values()),
                "wall_seconds": sum(item.reset_evidence()["wall_seconds"] for item in fixtures.values()),
                "cells": {
                    key: item.reset_evidence()
                    for key, item in sorted(fixtures.items())
                },
            }
        else:
            reset_evidence = fixture.reset_evidence() if fixture is not None else {"count": 0, "wall_seconds": 0.0}
        fixture_evidence: dict[str, Any] = {
            "scenario": self.descriptor.scenario,
            "reset": {
                "method": "POST",
                "url": self.descriptor.reset_url,
                "body": self.descriptor.reset_body,
            },
            "reset_evidence": reset_evidence,
        }
        if self.descriptor.benchmark_cells:
            fixture_evidence["cells"] = {
                key: {
                    "app_origin": descriptor.app_origin,
                    "fixture_origin": descriptor.fixture_origin,
                    "reset": {
                        "method": "POST",
                        "url": descriptor.reset_url,
                        "body": descriptor.reset_body,
                    },
                }
                for key, descriptor in sorted(self.descriptor.benchmark_cells.items())
            }
        if isinstance(safe_runtime, dict):
            runtime_evidence = safe_runtime.get("runtime")
            if isinstance(runtime_evidence, dict):
                for name in ("dependency_identity", "dependency_reset"):
                    if name in runtime_evidence:
                        fixture_evidence[name] = runtime_evidence[name]
        checkpoint_evidence: dict[str, Any] = {
            "enabled": self._checkpoint_store is not None,
            "path": str(self.checkpoint_path) if self.checkpoint_path is not None else None,
            "new_trials": self._checkpoint_new_trials,
            "reused_trials": self._checkpoint_reused_trials,
            "rerun_trials": self._checkpoint_rerun_trials,
            "valid_completed_trials": (
                sum(
                    1
                    for record in checkpoint_snapshot.records.values()
                    if record.status == "completed" and valid_completed_outcome(record.outcome)
                )
                if checkpoint_snapshot is not None
                else sum(
                    1
                    for outcomes in self._completed_trials.values()
                    for outcome in outcomes
                    if valid_completed_outcome(outcome)
                )
            ),
        }
        checkpoint_doc = checkpoint_snapshot or (
            self._checkpoint_store.document if self._checkpoint_store is not None else None
        )
        if checkpoint_doc is not None:
            checkpoint_record_count = len(checkpoint_doc.records)
            checkpoint_reused_trials = checkpoint_doc.reused_trial_count
            checkpoint_new_trials = checkpoint_record_count - checkpoint_reused_trials
            if checkpoint_new_trials < 0:
                incomplete_reasons.add("benchmark incomplete: checkpoint reused count exceeds record count")
                checkpoint_new_trials = 0
            checkpoint_evidence["new_trials"] = checkpoint_new_trials
            checkpoint_evidence["reused_trials"] = checkpoint_reused_trials
            checkpoint_evidence["rerun_trials"] = checkpoint_doc.rerun_trial_count
            checkpoint_evidence["timing"] = checkpoint_doc.timing.model_dump(mode="json")
            checkpoint_evidence["lifecycle"] = checkpoint_doc.lifecycle
            checkpoint_evidence["record_count"] = len(checkpoint_doc.records)
            checkpoint_evidence["reserved_cost_usd"] = checkpoint_doc.reserved_cost_usd
            checkpoint_evidence["spent"] = checkpoint_doc.spent.model_dump(mode="json")
            checkpoint_evidence["spent_hash"] = checkpoint_doc.spent_hash
            checkpoint_evidence["timing_hash"] = checkpoint_doc.timing_hash
            if checkpoint_doc.lifecycle != "finalized":
                incomplete_reasons.add("benchmark incomplete: checkpoint lifecycle was not finalized")
            if len(checkpoint_doc.records) != completed_trials:
                incomplete_reasons.add("benchmark incomplete: checkpoint and artifact trial counts differ")
            if checkpoint_doc.reserved_cost_usd != 0:
                incomplete_reasons.add("benchmark incomplete: checkpoint has an orphaned reservation")
        if ledger.reserved_cost_usd != Decimal("0"):
            incomplete_reasons.add("benchmark incomplete: artifact has an orphaned reservation")
        if checkpoint_doc is not None:
            budget_model_requests = checkpoint_doc.spent.model_requests
            budget_input_tokens = checkpoint_doc.spent.input_tokens
            budget_output_tokens = checkpoint_doc.spent.output_tokens
            budget_cost_usd = Decimal(str(checkpoint_doc.spent.cost_usd))
            budget_wall_seconds = checkpoint_doc.spent.wall_seconds
            budget_model_work_seconds = checkpoint_doc.spent.model_work_seconds
            budget_reserved = Decimal(str(checkpoint_doc.reserved_cost_usd))
        else:
            budget_model_requests = ledger.requests
            budget_input_tokens = ledger.input_tokens
            budget_output_tokens = ledger.output_tokens
            budget_cost_usd = ledger.cost_usd
            budget_wall_seconds = ledger.wall_seconds
            budget_model_work_seconds = ledger.model_work_seconds
            budget_reserved = ledger.reserved_cost_usd
        budget_cost_value = float(budget_cost_usd)
        budget_reserved_value = float(budget_reserved)
        timing_payload = timing or {
            "attempt_index": 1,
            "wall_seconds": end_to_end_wall_seconds,
            "breakdown": {"other": end_to_end_wall_seconds},
        }
        artifact: dict[str, Any] = {
            "schema_version": 1,
            "status": "incomplete" if failure is not None or incomplete_reasons else "complete",
            "incomplete_reasons": sorted(incomplete_reasons),
            "completed_trials": completed_trials,
            "arm": self.arm,
            "run_manifest_hash": hash_json(self.manifest.model_dump(mode="json")),
            "task_corpus_hash": hash_json([task.model_dump(mode="json") for task in self.tasks]),
            "task_ids": [task.id for task in self.tasks],
            "task_locales": [
                {"id": task.id, "locale": task.locale, "pair_id": task.pair_id}
                for task in self.tasks
            ],
            "task_contracts": [
                {
                    "id": task.id,
                    "suite": task.suite,
                    "capability_families": sorted(task.capability_families),
                    "risk_hypotheses": sorted(task.risk_hypotheses),
                }
                for task in self.tasks
            ],
            "paired_order_plan": [
                {
                    "task_id": task.id,
                    "repeat_index": repeat_index,
                    "arm_order": list(
                        planned_arm_order(task.id, repeat_index, self.manifest.paired_order_seed)
                    ),
                }
                for repeat_index in range(1, self.manifest.repeats + 1)
                for task in self.tasks
            ],
            "category_counts": dict(sorted(Counter(task.category for task in self.tasks).items())),
            "suite_counts": dict(sorted(Counter(task.suite for task in self.tasks).items())),
            "locale_counts": dict(sorted(Counter(task.locale for task in self.tasks).items())),
            "source_revision": runtime["source_revision"],
            "protocol_revision": self.manifest.protocol_revision,
            "request_timeout_seconds": self.manifest.budget.request_timeout_seconds,
            "artifact_versions": artifact_versions,
            "fixture": fixture_evidence,
            "runtime": safe_runtime,
            "manifest": self.manifest.model_dump(mode="json"),
            "provider_registry": self.provider_registry,
            "pre_smoke_seal_inputs": self.pre_smoke_seal_inputs,
            "pre_smoke_seal_hash": self.pre_smoke_seal_hash,
            "catalogs": {key: safe_json(value.model_dump(mode="json"), self.secrets) for key, value in sorted(catalogs.items())},
            "runs": all_reports,
            "overall_metrics": summarize_outcomes(all_outcomes),
            "locale_metrics": summarize_outcomes_by_locale(all_outcomes),
            "checkpoint": checkpoint_evidence,
            "smoke_gate": smoke_gate or self._smoke_gate,
            "timing": timing_payload,
            "end_to_end_wall_seconds": timing_payload.get("cumulative_wall_seconds", end_to_end_wall_seconds),
            "budget_used": {
                "model_requests": budget_model_requests,
                "provider_setup_requests": (
                    checkpoint_doc.spent.provider_setup_requests
                    if checkpoint_doc is not None
                    else ledger.provider_setup_requests
                ),
                "input_tokens": budget_input_tokens,
                "output_tokens": budget_output_tokens,
                "total_tokens": budget_input_tokens + budget_output_tokens,
                "cost_usd": budget_cost_value,
                "wall_seconds": budget_wall_seconds,
                "model_work_seconds": budget_model_work_seconds,
                "reserved_cost_usd": budget_reserved_value,
                "request_timeout_seconds": self.manifest.budget.request_timeout_seconds,
            },
        }
        if failure is not None:
            cleanup = cleanup_errors or []
            stage = failure_stage or _exception_stage(failure, "run")
            artifact["failure_stage"] = stage
            artifact["failure"] = {
                "stage": stage,
                "error": redact_exception(failure, self.secrets),
                "cleanup_errors": [redact_exception(error, self.secrets) for error in cleanup],
            }
        elif quality_reasons or trial_error_reasons:
            reason = (quality_reasons or trial_error_reasons)[0]
            artifact["failure_stage"] = "model_request"
            artifact["failure"] = {
                "stage": "model_request",
                "error": redact_text(reason, self.secrets),
                "cleanup_errors": [],
            }
        safe_artifact = safe_json(artifact, self.secrets)
        trial_order = [key.model_dump(mode="json") for key in self._planned_keys(runtime["source_revision"]).values()]
        hash_input = _build_artifact_hash_input(artifact, trial_order=trial_order)
        safe_hash_input = safe_json(hash_input, self.secrets)
        assert isinstance(safe_artifact, dict)
        safe_artifact["artifact_hash_input"] = safe_hash_input
        safe_artifact["artifact_hash"] = hash_json(safe_hash_input)
        return safe_artifact

    async def _checkpoint_trial(
        self,
        run_key: str,
        key: CheckpointKey,
        outcome: TrialOutcome,
        status: Literal["completed", "failed", "incomplete"],
    ) -> None:
        if self._checkpoint_store is not None:
            async with self._checkpoint_lock:
                self._refresh_store_secrets()
                checkpoint_started = time.perf_counter()
                existed = await asyncio.to_thread(
                    self._checkpoint_store.record_trial,
                    key,
                    outcome,
                    status=status,
                    timing=self._timing_snapshot(),
                    elapsed_wall_seconds=(
                        self._ledger.current_wall_seconds()
                        if self._ledger is not None
                        else None
                    ),
                    reserved_cost_usd=(float(self._ledger.reserved_cost_usd) if self._ledger is not None else 0.0),
                )
                if self._timing is not None:
                    self._timing.record("checkpoint", checkpoint_started)
            if not existed:
                self._checkpoint_new_trials += 1
        self._record_trial(run_key, outcome)

    def _refresh_store_secrets(self) -> None:
        if self._checkpoint_store is not None:
            self._checkpoint_store.set_secrets(self.secrets)

    def _smoke_task(self, transport: str) -> TaskManifest:
        candidates = [
            task
            for task in self.tasks
            if transport in task.fixture.transports and task.fixture.credential_profile == "default"
        ]
        if not candidates:
            candidates = [task for task in self.tasks if transport in task.fixture.transports]
        if not candidates:
            raise RuntimeContractError(f"smoke gate has no task compatible with {transport}", stage="smoke_gate")
        return candidates[0]

    async def _run_smoke_gate(
        self,
        *,
        fixture: RuntimeFixture | None = None,
        fixtures: Mapping[str, RuntimeFixture] | None = None,
        resolver: CredentialResolver,
        ledger: BudgetLedger,
        catalog_input_schemas: Mapping[str, dict[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        self._resolver = resolver
        required_cells = [
            f"{model.class_name}:{transport}"
            for model in self.manifest.models
            for transport in self.manifest.transports
        ]
        self._smoke_gate = {
            "status": "in_progress",
            "required_cells": required_cells,
            "cells": [],
        }
        cell_fixtures = {
            key: (fixtures[key] if fixtures is not None and key in fixtures else fixture)
            for key in required_cells
        }
        if any(value is None for value in cell_fixtures.values()):
            raise RuntimeContractError("smoke gate fixtures are incomplete", stage="smoke_gate")

        async def run_cell(model_spec: Any, transport: str) -> dict[str, Any]:
            cell_key = f"{model_spec.class_name}:{transport}"
            cell_fixture = cell_fixtures[cell_key]
            assert cell_fixture is not None
            cached = (
                self._checkpoint_store.smoke_outcome_for(cell_key)
                if self._checkpoint_store is not None
                else None
            )
            if cached is not None:
                return {
                    "cell": cell_key,
                    "status": "completed",
                    "reused": True,
                    "outcome": safe_json(cached.model_dump(mode="json"), self.secrets),
                }

            task = self._smoke_task(transport)
            model = build_model(model_spec)
            catalog_key = f"{transport}:{task.fixture.credential_profile}"
            if catalog_input_schemas is None:
                input_schemas = None
            else:
                input_schemas = catalog_input_schemas.get(catalog_key)
                if input_schemas is None:
                    raise RuntimeContractError(
                        f"captured catalog is missing input schemas for {catalog_key}",
                        stage="catalog_capture",
                    )
            reservation = worst_case_cost(model_spec, self.manifest.budget)
            outcome: TrialOutcome | None = None
            try:
                request_guard = await ledger.reserve_trial(reservation)
            except Exception as exc:
                outcome = TrialOutcome(
                    task_id=task.id,
                    category=task.category,
                    locale=task.locale,
                    arm=self.arm,
                    model_class=model_spec.class_name,
                    model_id=model_spec.model_id,
                    transport=transport,
                    error=f"benchmark incomplete: {redact_exception(exc, self.secrets)}",
                    failure_kind="budget",
                )
                if self._checkpoint_store is not None:
                    async with self._checkpoint_lock:
                        self._refresh_store_secrets()
                        await asyncio.to_thread(
                            self._checkpoint_store.record_smoke_cell,
                            cell_key,
                            outcome,
                            status="incomplete",
                            timing=self._timing_snapshot(),
                            elapsed_wall_seconds=(
                                self._ledger.current_wall_seconds()
                                if self._ledger is not None
                                else None
                            ),
                            reserved_cost_usd=(float(self._ledger.reserved_cost_usd) if self._ledger is not None else 0.0),
                        )
                raise RuntimeContractError(
                    f"smoke gate cell {cell_key} could not reserve its registered budget",
                    stage="smoke_gate",
                ) from exc

            try:
                await cell_fixture.reset()
                self._resolver = resolver
                token = await self._refresh_token(cell_fixture, task.fixture.credential_profile)
                self._refresh_secrets(resolver)
                remaining_wall_seconds = ledger.remaining_wall_seconds()
                request_timeout_seconds = min(
                    float(self.manifest.budget.request_timeout_seconds),
                    remaining_wall_seconds,
                )
                if self._timing is None:
                    outcome = await execute_smoke(
                        task,
                        manifest=self.manifest,
                        arm=self.arm,
                        model_spec=model_spec,
                        model=model,
                        transport=transport,
                        fixture=cell_fixture,
                        token=token,
                        secrets=self.secrets,
                        request_timeout_seconds=request_timeout_seconds,
                        remaining_wall_seconds=remaining_wall_seconds,
                        request_guard=request_guard,
                        input_schemas=input_schemas,
                        timing_sink=self._timing.record if self._timing is not None else None,
                    )
                else:
                    with self._timing.measure("model_execution"):
                        outcome = await execute_smoke(
                            task,
                            manifest=self.manifest,
                            arm=self.arm,
                            model_spec=model_spec,
                            model=model,
                            transport=transport,
                            fixture=cell_fixture,
                            token=token,
                            secrets=self.secrets,
                            request_timeout_seconds=request_timeout_seconds,
                            remaining_wall_seconds=remaining_wall_seconds,
                            request_guard=request_guard,
                            input_schemas=input_schemas,
                            timing_sink=self._timing.record if self._timing is not None else None,
                        )
                await ledger.charge(outcome, guard=request_guard)
            except Exception as exc:
                with_context = exc if isinstance(exc, RuntimeContractError) else RuntimeContractError(
                    f"smoke gate cell {cell_key} failed: {redact_exception(exc, self.secrets)}",
                    stage="smoke_gate",
                )
                if outcome is None:
                    outcome = TrialOutcome(
                        task_id=task.id,
                        category=task.category,
                        locale=task.locale,
                        arm=self.arm,
                        model_class=model_spec.class_name,
                        model_id=model_spec.model_id,
                        transport=transport,
                        error=redact_exception(with_context, self.secrets),
                        failure_kind="unknown",
                    )
                else:
                    outcome.error = f"benchmark incomplete: {redact_exception(exc, self.secrets)}"
                    outcome.failure_kind = "budget"
            finally:
                await request_guard.release()

            assert outcome is not None
            identity_matches = (
                outcome.model_class == model_spec.class_name
                and outcome.model_id == model_spec.model_id
                and outcome.transport == transport
                and all(
                    item.get("model") == model_spec.model_id
                    for item in outcome.provider_evidence
                )
            )
            valid = identity_matches and valid_smoke_outcome(outcome)
            if not valid and outcome.error is None:
                if not identity_matches:
                    outcome.error = "smoke gate outcome did not match the requested model and transport"
                    outcome.failure_kind = "provider"
                elif outcome.successful_mcp_tool_calls == 0:
                    outcome.error = "smoke gate did not observe a successful MCP tool call"
                    outcome.failure_kind = "tool"
                else:
                    outcome.error = "smoke gate did not observe a follow-up terminal response"
                    outcome.failure_kind = "terminal_response"
            status: Literal["completed", "failed", "incomplete"] = "completed" if valid else (
                "incomplete" if outcome.error and outcome.error.startswith("benchmark incomplete:") else "failed"
            )
            if self._checkpoint_store is not None:
                async with self._checkpoint_lock:
                    self._refresh_store_secrets()
                    await asyncio.to_thread(
                        self._checkpoint_store.record_smoke_cell,
                        cell_key,
                        outcome,
                        status=status,
                        timing=self._timing_snapshot(),
                        elapsed_wall_seconds=(
                            self._ledger.current_wall_seconds()
                            if self._ledger is not None
                            else None
                        ),
                        reserved_cost_usd=(float(self._ledger.reserved_cost_usd) if self._ledger is not None else 0.0),
                    )
            return {
                "cell": cell_key,
                "status": status,
                "reused": False,
                "outcome": safe_json(outcome.model_dump(mode="json"), self.secrets),
            }

        jobs = [
            (model_spec, transport)
            for model_spec in self.manifest.models
            for transport in self.manifest.transports
        ]
        if self.descriptor.benchmark_cells:
            results = await asyncio.gather(*(run_cell(model, transport) for model, transport in jobs))
        else:
            results = [await run_cell(model, transport) for model, transport in jobs]
        cells = {item["cell"]: item for item in results}
        for cell_key in required_cells:
            item = cells[cell_key]
            if item["status"] == "completed":
                continue
            if self._checkpoint_store is not None:
                async with self._checkpoint_lock:
                    await asyncio.to_thread(self._checkpoint_store.set_smoke_status, "failed")
            self._smoke_gate = {
                "status": "failed",
                "required_cells": required_cells,
                "cells": [cells[key] for key in required_cells if key in cells],
            }
            raise RuntimeContractError(
                f"smoke gate cell {cell_key} did not produce model usage and a terminal routed outcome",
                stage="smoke_gate",
            )

        if self._checkpoint_store is not None:
            async with self._checkpoint_lock:
                await asyncio.to_thread(self._checkpoint_store.set_smoke_status, "passed")
        self._smoke_gate = {
            "status": "passed",
            "required_cells": required_cells,
            "cells": [cells[key] for key in required_cells],
        }
        return self._smoke_gate

    async def preflight(self) -> dict[str, Any]:
        self.manifest.validate_tasks(self.tasks)
        if self.manifest.source_revision != "runtime_descriptor" and not _is_git_revision(self.manifest.source_revision):
            raise ValueError("source_revision must be runtime_descriptor or a full git revision")
        if self.descriptor.scenario != self.manifest.fixture_scenario:
            raise RuntimeContractError("runtime scenario does not match the run manifest")
        if not self.descriptor.supports_stdio:
            raise RuntimeContractError("the benchmark requires the runtime stdio service")
        model_secrets: list[str] = []
        for model_spec in self.manifest.models:
            api_key = os.environ.get(model_spec.provider_key_env, "")
            base_url = os.environ.get(model_spec.base_url_env, "")
            if not api_key or not base_url:
                self._provider_registry_reason = (
                    f"model {model_spec.class_name} is missing registered provider credentials"
                )
                continue
            try:
                validate_model_configuration(model_spec)
            except Exception as exc:
                self._provider_registry_reason = redact_exception(exc, (api_key,))
                continue
            model_secrets.append(api_key)
        resolver = CredentialResolver(self.manifest, self.descriptor, tuple(model_secrets))
        profiles = resolver.required_profiles(self.tasks)
        resolver.validate_inputs(profiles)
        cell_descriptors = self._cell_descriptors()
        if any(not descriptor.supports_stdio for descriptor in cell_descriptors.values()):
            raise RuntimeContractError("every benchmark cell must expose the runtime stdio service")
        preflight_cells = cell_descriptors

        async def preflight_cell(descriptor: RuntimeDescriptor) -> dict[str, Any]:
            fixture = RuntimeFixture(descriptor)
            try:
                return await fixture.preflight()
            finally:
                await fixture.close()

        runtime_cells: dict[str, dict[str, Any]] = {}
        primary_error: Exception | None = None
        try:
            results = await asyncio.gather(
                *(preflight_cell(descriptor) for descriptor in preflight_cells.values())
            )
            runtime_cells = dict(zip(preflight_cells, results, strict=True))
        except Exception as exc:
            primary_error = exc
        if primary_error is not None:
            raise primary_error
        if not runtime_cells:
            raise RuntimeContractError("runtime preflight returned no cells")
        first_runtime = next(iter(runtime_cells.values()))
        for other_runtime in runtime_cells.values():
            if (
                other_runtime["source_revision"] != first_runtime["source_revision"]
                or other_runtime["artifact_versions"] != first_runtime["artifact_versions"]
            ):
                raise RuntimeContractError("benchmark cell runtime identities differ")
        runtime = dict(first_runtime)
        if self.descriptor.benchmark_cells:
            runtime["discovery"] = {
                "cells": {key: value["discovery"] for key, value in sorted(runtime_cells.items())}
            }
            runtime["cell_runtimes"] = runtime_cells
        if self.manifest.source_revision != "runtime_descriptor" and self.manifest.source_revision != runtime["source_revision"]:
            raise RuntimeContractError("runtime source revision does not match the pinned run manifest")
        return {
            "runtime": runtime,
            "resolver": resolver,
            "provider_registry_reason": self._provider_registry_reason,
        }

    async def _capture_provider_registry(self, ledger: BudgetLedger) -> dict[str, Any]:
        async def capture() -> dict[str, Any]:
            if self._provider_registry_reason:
                return {"status": "unavailable", "reason": self._provider_registry_reason}
            model_list: Any = None
            endpoint_snapshots: dict[str, Any] = {}
            try:
                first = self.manifest.models[0]
                api_key = os.environ[first.provider_key_env]
                base_url = os.environ[first.base_url_env].rstrip("/")
                headers = {"Authorization": f"Bearer {api_key}"}
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                    async def get_json(url: str) -> Any:
                        await ledger.record_provider_setup_request()
                        if self._checkpoint_store is not None:
                            async with self._checkpoint_lock:
                                await asyncio.to_thread(
                                    self._checkpoint_store.record_provider_setup_requests,
                                    1,
                                )
                        response = await client.get(url, headers=headers)
                        response.raise_for_status()
                        return response.json()

                    model_list = await get_json(f"{base_url}/models")
                    rows = model_list.get("data") if isinstance(model_list, dict) else None
                    if not isinstance(rows, list):
                        raise RuntimeContractError("provider model registry response is invalid", stage="provider_registry")
                    registry_models: dict[str, Any] = {}
                    for spec in self.manifest.models:
                        matches = [row for row in rows if isinstance(row, dict) and row.get("id") == spec.model_id]
                        if len(matches) != 1:
                            raise RuntimeContractError(
                                f"provider registry does not expose exactly one pinned model {spec.model_id}",
                                stage="provider_registry",
                            )
                        row = matches[0]
                        canonical_slug = row.get("canonical_slug", row.get("id"))
                        if canonical_slug != spec.model_id:
                            raise RuntimeContractError(
                                f"provider model alias drifted for {spec.class_name}",
                                stage="provider_registry",
                            )
                        author, slug = spec.model_id.split("/", 1)
                        endpoints_response = await get_json(
                            f"{base_url}/models/{author}/{slug}/endpoints"
                        )
                        data = endpoints_response.get("data") if isinstance(endpoints_response, dict) else None
                        endpoints = data.get("endpoints") if isinstance(data, dict) else None
                        if not isinstance(endpoints, list):
                            raise RuntimeContractError(
                                f"provider endpoint registry is invalid for {spec.class_name}",
                                stage="provider_registry",
                            )
                        selected = [
                            endpoint
                            for endpoint in endpoints
                            if isinstance(endpoint, dict)
                            and str(endpoint.get("provider_name", "")).casefold() == "parasail"
                            and endpoint.get("quantization") == "fp8"
                        ]
                        if len(selected) != 1:
                            raise RuntimeContractError(
                                f"pinned Parasail fp8 endpoint is unavailable or ambiguous for {spec.class_name}",
                                stage="provider_registry",
                            )
                        registry_models[spec.model_id] = {
                            "manifest_version": spec.version,
                            "model_record": row,
                            "endpoint_snapshot": endpoints_response,
                            "selected_endpoint": selected[0],
                        }
                        endpoint_snapshots[spec.model_id] = endpoints_response
                payload = {
                    "status": "verified",
                    "model_list_snapshot": model_list,
                    "model_list_hash": hash_json(model_list),
                    "models": registry_models,
                    "endpoint_snapshots": endpoint_snapshots,
                }
                payload["snapshot_hash"] = hash_json({key: value for key, value in payload.items() if key != "snapshot_hash"})
                return payload
            except Exception as exc:
                return {
                    "status": "unavailable",
                    "reason": redact_exception(exc, self.secrets),
                    "model_list_hash": hash_json(model_list) if model_list is not None else None,
                }

        if self.paired_coordinator is not None:
            self.provider_registry = await self.paired_coordinator.shared_provider_registry(capture)
        else:
            self.provider_registry = await capture()
        return self.provider_registry

    def _local_pre_smoke_seal(self, runtime: dict[str, Any], catalogs: dict[str, CatalogSnapshot]) -> dict[str, Any]:
        package_versions: dict[str, str | None] = {}
        for package in ("pydantic-ai", "pydantic-evals", "fastmcp", "mcp", "scipy"):
            try:
                package_versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                package_versions[package] = None
        lock_path = Path(__file__).parents[1] / "uv.lock"
        lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        return {
            "schema_version": 1,
            "arm": self.arm,
            "source_revision": runtime["source_revision"],
            "run_manifest_hash": hash_json(self.manifest.model_dump(mode="json")),
            "task_corpus_hash": hash_json([task.model_dump(mode="json") for task in self.tasks]),
            "oracle_hash": hash_json({task.id: task.expected_final_state.model_dump(mode="json") for task in self.tasks}),
            "catalogs": {key: snapshot.model_dump(mode="json") for key, snapshot in sorted(catalogs.items())},
            "fixture": {
                "scenario": self.descriptor.scenario,
                "reset_url": self.descriptor.reset_url,
                "reset_body": self.descriptor.reset_body,
                "runtime_descriptor": safe_json(self.descriptor.raw, self.secrets),
                "runtime_identity": runtime["artifact_versions"],
            },
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "packages": package_versions,
                "uv_lock_sha256": lock_hash,
            },
            "paired_order_plan": [
                {
                    "task_id": task.id,
                    "repeat_index": repeat_index,
                    "arm_order": list(planned_arm_order(task.id, repeat_index, self.manifest.paired_order_seed)),
                }
                for repeat_index in range(1, self.manifest.repeats + 1)
                for task in self.tasks
            ],
            "provider_registry_hash": self.provider_registry.get("snapshot_hash"),
            "provider_registry_status": self.provider_registry.get("status"),
        }

    async def _bind_pre_smoke_seal(
        self,
        runtime: dict[str, Any],
        catalogs: dict[str, CatalogSnapshot],
    ) -> None:
        self.pre_smoke_seal_inputs = self._local_pre_smoke_seal(runtime, catalogs)
        local_hash = hash_json(self.pre_smoke_seal_inputs)
        self.pre_smoke_seal_hash = (
            await self.paired_coordinator.register_pre_smoke_seal(cast(ArmName, self.arm), local_hash)
            if self.paired_coordinator is not None
            else hash_json({self.arm: local_hash})
        )
        if self._checkpoint_store is not None:
            async with self._checkpoint_lock:
                await asyncio.to_thread(
                    self._checkpoint_store.bind_pre_smoke_seal,
                    self.pre_smoke_seal_hash,
                )
        if self.paired_coordinator is not None:
            await self.paired_coordinator.mark_pre_smoke_ready(cast(ArmName, self.arm))

    async def _run_cell(
        self,
        *,
        run_key: str,
        model_spec: Any,
        transport: str,
        pending: list[tuple[TaskManifest, int, CheckpointKey]],
        fixture: RuntimeFixture,
        resolver: CredentialResolver,
        ledger: BudgetLedger,
        planned_by_identity: dict[tuple[str, str, str, int], CheckpointKey],
        profiles: list[str],
        lifecycle_cleanup_errors: list[Exception],
        incomplete_reasons: set[str],
        reports: dict[str, Any],
        catalog_input_schemas: Mapping[str, dict[str, dict[str, Any]]] | None = None,
    ) -> None:
        model = build_model(model_spec)
        executor = TrialExecutor(
            self.manifest,
            arm=self.arm,
            model_spec=model_spec,
            model=model,
            transport=transport,
            fixture=fixture,
            token_for=resolver.token_for,
            secrets_for=resolver.secrets_for,
            ledger=ledger,
            input_schemas_by_profile=(
                {
                    profile: dict(catalog_input_schemas[f"{transport}:{profile}"])
                    for profile in profiles
                }
                if catalog_input_schemas is not None
                else None
            ),
            timing_sink=self._timing.record if self._timing is not None else None,
        )
        report_fragments: list[dict[str, Any]] = []
        batches = (
            [[item] for item in pending]
            if self.paired_coordinator is not None
            else [
                [item for item in pending if item[1] == repeat_index]
                for repeat_index in sorted({item[1] for item in pending})
            ]
        )
        for batch in batches:
            selected_tasks = [item[0] for item in batch]
            repeat_index = batch[0][1]
            repeat_indices = {task.id: repeat_index for task in selected_tasks}
            paired_task = selected_tasks[0]

            async def checkpoint_sink(
                outcome: TrialOutcome,
                status: Literal["completed", "failed", "incomplete"],
                *,
                target_run_key: str = run_key,
            ) -> None:
                identity = self._trial_identity(outcome)
                checkpoint_key = planned_by_identity.get(identity)
                if checkpoint_key is None:
                    raise RuntimeContractError("trial outcome is not an exact planned checkpoint input")
                await self._checkpoint_trial(
                    target_run_key,
                    checkpoint_key,
                    outcome,
                    status=status,
                )

            turn_context = (
                self.paired_coordinator.turn(
                    cell=run_key,
                    task_id=paired_task.id,
                    repeat_index=repeat_index,
                    arm=cast(ArmName, self.arm),
                )
                if self.paired_coordinator is not None
                else _uncoordinated_turn()
            )
            async with turn_context as paired_turn:
                if paired_turn is not None:
                    executor.set_paired_turn(
                        order_position=paired_turn.order_position,
                        execution_sequence=paired_turn.execution_sequence,
                    )
                local_failures: list[Exception] = []
                try:
                    if self._timing is None:
                        report = await evaluate_dataset(
                            selected_tasks,
                            manifest=self.manifest,
                            executor=executor,
                            fixture=fixture,
                            failure_sink=local_failures,
                            cleanup_sink=lifecycle_cleanup_errors,
                            refresh_token=self._refresh_token,
                            mark_reset_complete=resolver.mark_reset_complete,
                            repeat=1,
                            repeat_indices=repeat_indices,
                            checkpoint_sink=checkpoint_sink,
                        )
                    else:
                        with self._timing.measure("model_execution"):
                            report = await evaluate_dataset(
                                selected_tasks,
                                manifest=self.manifest,
                                executor=executor,
                                fixture=fixture,
                                failure_sink=local_failures,
                                cleanup_sink=lifecycle_cleanup_errors,
                                refresh_token=self._refresh_token,
                                mark_reset_complete=resolver.mark_reset_complete,
                                repeat=1,
                                repeat_indices=repeat_indices,
                                checkpoint_sink=checkpoint_sink,
                            )
                finally:
                    executor.clear_paired_turn()
                if local_failures:
                    raise local_failures[0]
                outcomes = outcomes_from_report(
                    report,
                    1,
                    repeat_indices=repeat_indices,
                )
                for outcome in outcomes:
                    self._record_trial(run_key, outcome)
                report_fragments.append(serialize_report(report, self.secrets))
                self._refresh_secrets(resolver)
                incomplete_reasons.update(
                    outcome.error
                    for outcome in outcomes
                    if outcome.error and outcome.error.startswith("benchmark incomplete:")
                )
                termination = next(
                    (
                        outcome
                        for outcome in outcomes
                        if outcome.failure_kind in {"request_timeout", "global_deadline", "interrupted"}
                    ),
                    None,
                )
                if termination is not None:
                    raise RuntimeContractError(
                        termination.error or "benchmark incomplete: model request terminated",
                        stage="model_request",
                    )
        reports[run_key] = {
            "catalog_keys": sorted(
                f"{transport}:{profile}"
                for profile in profiles
                if any(
                    task.fixture.credential_profile == profile and transport in task.fixture.transports
                    for task in self.tasks
                )
            ),
            "report": report_fragments,
            "trials": [outcome.model_dump(mode="json") for outcome in self._completed_trials[run_key]],
            "summary": summarize_outcomes(self._completed_trials[run_key]),
        }

    async def run(self) -> dict[str, Any]:
        timing = TimingTracker()
        self._timing = timing
        started = timing.started_at
        with timing.measure("provisioning"):
            preflight = await self.preflight()
        runtime = preflight["runtime"]
        resolver: CredentialResolver = preflight["resolver"]
        self._resolver = resolver
        source_revision = runtime["source_revision"]
        artifact_versions = runtime["artifact_versions"]
        expected_source_revision = self.manifest.arm_source_revisions[cast(ArmName, self.arm)]
        if source_revision != expected_source_revision:
            raise RuntimeContractError(
                f"runtime source revision does not match the pinned {self.arm} arm revision",
                stage="source_identity",
            )
        prior_elapsed = 0.0
        owns_ledger = self.shared_ledger is None
        ledger = self.shared_ledger or BudgetLedger(
            self.manifest,
            wall_clock=lambda: prior_elapsed + max(0.0, time.perf_counter() - started),
        )
        self._ledger = ledger
        catalogs: dict[str, Any] = {}
        reports: dict[str, Any] = {}
        incomplete_reasons: set[str] = set()
        lifecycle_cleanup_errors: list[Exception] = []
        checkpoint_snapshot: CheckpointDocument | None = None
        current_stage = "run_initialization"
        self._refresh_secrets(resolver)
        planned_keys = self._planned_keys(source_revision)
        planned_by_identity: dict[tuple[str, str, str, int], CheckpointKey] = {
            (key.model_class, key.transport, key.task_id, key.repeat_index): key
            for key in planned_keys.values()
        }
        self._checkpoint_store = self._checkpoint_store_for(
            source_revision=source_revision,
            resolver=resolver,
            planned_keys=planned_keys,
        )
        if self._checkpoint_store is not None:
            self._timing_attempt_index = self._checkpoint_store.next_timing_attempt_index()
            prior_elapsed = self._checkpoint_store.document.spent.wall_seconds
            spent = self._checkpoint_store.document.spent
            if owns_ledger:
                ledger.restore(
                    model_requests=spent.model_requests,
                    provider_setup_requests=spent.provider_setup_requests,
                    input_tokens=spent.input_tokens,
                    output_tokens=spent.output_tokens,
                    cost_usd=spent.cost_usd,
                    wall_seconds=spent.wall_seconds,
                    model_work_seconds=spent.model_work_seconds,
                    budget_failure=self._checkpoint_store.budget_failure_reason(),
                )
            else:
                await ledger.restore_additive(
                    model_requests=spent.model_requests,
                    provider_setup_requests=spent.provider_setup_requests,
                    input_tokens=spent.input_tokens,
                    output_tokens=spent.output_tokens,
                    cost_usd=spent.cost_usd,
                    wall_seconds=spent.wall_seconds,
                    model_work_seconds=spent.model_work_seconds,
                    budget_failure=self._checkpoint_store.budget_failure_reason(),
                )
            initial_timing = self._timing_snapshot()
            assert initial_timing is not None
            await asyncio.to_thread(self._checkpoint_store.update_timing, initial_timing)
            for planned_key in planned_keys.values():
                outcome = self._checkpoint_store.completed_outcome_for(planned_key)
                if outcome is not None:
                    self._record_trial(f"{planned_key.model_class}:{planned_key.transport}", outcome)
                    self._checkpoint_reused_trials += 1
                elif self._checkpoint_store.status_for(planned_key) is not None:
                    self._checkpoint_rerun_trials += 1
        if self.paired_coordinator is not None:
            reused = {
                (f"{key.model_class}:{key.transport}", key.task_id, key.repeat_index): outcome
                for key in planned_keys.values()
                if self._checkpoint_store is not None
                and (outcome := self._checkpoint_store.completed_outcome_for(key)) is not None
            }
            await self.paired_coordinator.register_arm(cast(ArmName, self.arm), reused)
            await self.paired_coordinator.wait_until_registered()
        planned_by_run = self._planned_trials_by_run(planned_keys)
        pending_by_run = {
            run_key: [
                (task, repeat_index, key)
                for task, repeat_index, key in trials
                if self._checkpoint_store is None or self._checkpoint_store.completed_outcome_for(key) is None
            ]
            for run_key, trials in planned_by_run.items()
        }
        cell_descriptors = self._cell_descriptors()
        if self.descriptor.benchmark_cells:
            fixtures = {
                key: RuntimeFixture(descriptor)
                for key, descriptor in cell_descriptors.items()
            }
        else:
            shared_fixture = RuntimeFixture(self.descriptor)
            fixtures = {key: shared_fixture for key in cell_descriptors}
        for cell_fixture in {id(item): item for item in fixtures.values()}.values():
            self._attach_timing(cell_fixture, timing)
        fixture = next(iter(fixtures.values()))
        primary_error: Exception | None = None
        try:
            current_stage = "runtime_readiness"
            await asyncio.gather(
                *(item.wait_until_ready(stage="runtime_readiness") for item in {id(value): value for value in fixtures.values()}.values())
            )
            current_stage = "credential_preparation"
            profiles = resolver.required_profiles(self.tasks)
            with timing.measure("credential"):
                await resolver.prepare(fixture, profiles)
            self._refresh_secrets(resolver)
            current_stage = "provider_registry"
            await self._capture_provider_registry(ledger)
            for transport in self.manifest.transports:
                for profile in profiles:
                    selected_tasks = [task for task in self.tasks if transport in task.fixture.transports and task.fixture.credential_profile == profile]
                    if not selected_tasks:
                        continue
                    current_stage = f"catalog_capture:{transport}:{profile}"
                    artifact_version = (
                        artifact_versions["backend_artifact_version"]
                        if transport == "http"
                        else artifact_versions["proxy_artifact_version"]
                    )
                    catalog_fixture = fixtures[f"{self.manifest.models[0].class_name}:{transport}"]
                    token = await self._refresh_token(catalog_fixture, profile)
                    self._refresh_secrets(resolver)
                    with timing.measure("catalog_capture"):
                        catalogs[f"{transport}:{profile}"] = await capture_catalog(
                            catalog_fixture,
                            transport=transport,
                            token=token,
                            source_revision=source_revision,
                            artifact_version=artifact_version,
                            capability_profile=_capability_profile(self.descriptor, runtime, transport, profile),
                        )

            current_stage = "catalog_contract"
            for snapshot in catalogs.values():
                if snapshot.source_revision != expected_source_revision:
                    raise RuntimeContractError("catalog source revision differs from the pinned arm revision")
                validate_public_catalog(
                    snapshot,
                    arm=cast(ArmName, self.arm),
                    public_operations=self.manifest.public_operations,
                )
            await self._bind_pre_smoke_seal(runtime, catalogs)
            if self.provider_registry.get("status") != "verified":
                raise RuntimeContractError(
                    "provider registry snapshot is unavailable or drifted; paid smoke was not started",
                    stage="provider_registry",
                )

            if any(pending_by_run.values()) or self._checkpoint_store is not None:
                current_stage = "smoke_gate"
                catalog_input_schemas = {
                    key: input_schemas_from_catalog(snapshot)
                    for key, snapshot in catalogs.items()
                }
                await self._run_smoke_gate(
                    fixtures=fixtures,
                    resolver=resolver,
                    ledger=ledger,
                    catalog_input_schemas=catalog_input_schemas,
                )
            else:
                self._smoke_gate = {
                    "status": "not_required",
                    "required_cells": [],
                    "cells": [],
                }

            jobs = [
                (model_spec, transport, f"{model_spec.class_name}:{transport}")
                for model_spec in self.manifest.models
                for transport in self.manifest.transports
                if pending_by_run.get(f"{model_spec.class_name}:{transport}")
            ]
            if self.descriptor.benchmark_cells:
                async with asyncio.TaskGroup() as group:
                    for model_spec, transport, run_key in jobs:
                        group.create_task(
                            self._run_cell(
                                run_key=run_key,
                                model_spec=model_spec,
                                transport=transport,
                                pending=pending_by_run[run_key],
                                fixture=fixtures[run_key],
                                resolver=resolver,
                                ledger=ledger,
                                planned_by_identity=planned_by_identity,
                                profiles=profiles,
                                lifecycle_cleanup_errors=lifecycle_cleanup_errors,
                                incomplete_reasons=incomplete_reasons,
                                reports=reports,
                                catalog_input_schemas=catalog_input_schemas,
                            )
                        )
            else:
                for model_spec, transport, run_key in jobs:
                    await self._run_cell(
                        run_key=run_key,
                        model_spec=model_spec,
                        transport=transport,
                        pending=pending_by_run[run_key],
                        fixture=fixtures[run_key],
                        resolver=resolver,
                        ledger=ledger,
                        planned_by_identity=planned_by_identity,
                        profiles=profiles,
                        lifecycle_cleanup_errors=lifecycle_cleanup_errors,
                        incomplete_reasons=incomplete_reasons,
                        reports=reports,
                        catalog_input_schemas=catalog_input_schemas,
                    )
        except asyncio.CancelledError:
            primary_error = RuntimeContractError(
                "benchmark interrupted by signal",
                stage="signal",
            )
        except Exception as exc:
            primary_error = _normalize_task_group_error(exc)
            if current_stage == "smoke_gate" and self._smoke_gate.get("status") == "in_progress":
                self._smoke_gate["status"] = "failed"
                if self._checkpoint_store is not None:
                    try:
                        await asyncio.to_thread(self._checkpoint_store.set_smoke_status, "failed")
                    except Exception:
                        pass
        finally:
            try:
                orphaned_reservation = (
                    await ledger.release_all_reservations()
                    if owns_ledger
                    else Decimal("0")
                )
                if orphaned_reservation > Decimal("1e-12"):
                    lifecycle_cleanup_errors.append(
                        BudgetExceeded(
                            f"checkpoint finalization released orphaned reservation ${orphaned_reservation:.8f}"
                        )
                    )
                if self._checkpoint_store is not None:
                    async with self._checkpoint_lock:
                        await asyncio.to_thread(self._checkpoint_store.set_reserved_cost, 0.0)
                        await asyncio.to_thread(
                            self._checkpoint_store.set_run_counters,
                            reused_trials=self._checkpoint_reused_trials,
                            rerun_trials=self._checkpoint_rerun_trials,
                        )
                        await asyncio.to_thread(self._checkpoint_store.begin_closing)
            except Exception as exc:
                lifecycle_cleanup_errors.append(exc)

            async def cleanup_fixture_state() -> None:
                mark_reset_complete = getattr(resolver, "mark_reset_complete", None)
                if callable(mark_reset_complete):
                    mark_reset_complete()
                unique_fixtures = list({id(item): item for item in fixtures.values()}.values())
                await asyncio.gather(*(item.reset() for item in unique_fixtures))

            cleanup_errors = await _collect_cleanup_errors(
                cleanup_fixture_state,
                lambda: resolver.cleanup(fixture),
                *(
                    item.close
                    for item in {id(value): value for value in fixtures.values()}.values()
                ),
            )
        self._refresh_secrets(resolver)
        cleanup_errors = [*lifecycle_cleanup_errors, *cleanup_errors]
        ledger.observe_wall()
        timing_payload: dict[str, Any] = timing.snapshot(attempt_index=self._timing_attempt_index)
        if self._checkpoint_store is not None:
            try:
                async with self._checkpoint_lock:
                    await asyncio.to_thread(
                        self._checkpoint_store.finalize_timing,
                        timing_payload,
                    )
                timing_payload = self._checkpoint_store.document.timing.model_dump(mode="json")
                checkpoint_snapshot = self._checkpoint_store.document.model_copy(deep=True)
            except Exception as exc:
                cleanup_errors.append(exc)
        if primary_error is None and cleanup_errors:
            primary_error = cleanup_errors[0]
        if primary_error is not None:
            notes = cleanup_errors if primary_error not in cleanup_errors else cleanup_errors[1:]
            _attach_cleanup_errors(primary_error, notes, self.secrets)
            incomplete_reasons.add(f"benchmark incomplete: {redact_exception(primary_error, self.secrets)}")
            artifact = self._build_artifact(
                runtime=runtime,
                artifact_versions=artifact_versions,
                ledger=ledger,
                catalogs=catalogs,
                reports=reports,
                incomplete_reasons=incomplete_reasons,
                failure=primary_error,
                failure_stage=_exception_stage(primary_error, current_stage),
                cleanup_errors=cleanup_errors,
                fixture=fixture,
                fixtures=fixtures,
                timing=timing_payload,
                end_to_end_wall_seconds=time.perf_counter() - started,
                smoke_gate=self._smoke_gate,
                checkpoint_snapshot=checkpoint_snapshot,
            )
            raise BenchmarkRunFailure(
                primary_error,
                artifact,
                self.secrets,
                tuple(cleanup_errors),
            ) from primary_error
        return self._build_artifact(
            runtime=runtime,
            artifact_versions=artifact_versions,
            ledger=ledger,
            catalogs=catalogs,
            reports=reports,
            incomplete_reasons=incomplete_reasons,
            fixture=fixture,
            fixtures=fixtures,
            timing=timing_payload,
            end_to_end_wall_seconds=time.perf_counter() - started,
            smoke_gate=self._smoke_gate,
            checkpoint_snapshot=checkpoint_snapshot,
        )

def outcomes_from_report(
    report: Any,
    repeat_count: int,
    *,
    repeat_indices: dict[str, int] | None = None,
) -> list[TrialOutcome]:
    if report.failures:
        raise RuntimeError("Pydantic Evals report contains failed cases")
    seen: dict[str, int] = defaultdict(int)
    outcomes: list[TrialOutcome] = []
    for case in report.cases:
        outcome = case.output
        if not isinstance(outcome, TrialOutcome):
            continue
        seen[outcome.task_id] += 1
        repeat_index = (repeat_indices or {}).get(outcome.task_id, seen[outcome.task_id])
        outcomes.append(outcome.model_copy(update={"repeat_index": repeat_index}))
    if any(count != repeat_count for count in seen.values()):
        raise RuntimeError("Pydantic Evals report did not contain the registered repeat count")
    return outcomes


def planned_arm_order(task_id: str, repeat_index: int, seed: str) -> tuple[ArmName, ArmName]:
    """Return a stable, preregistered counterbalanced arm order for one pair."""

    digest = hashlib.sha256(f"{seed}:{task_id}".encode()).digest()
    baseline_first = (digest[0] + repeat_index) % 2 == 0
    return ("baseline", "candidate") if baseline_first else ("candidate", "baseline")


def load_inputs(
    manifest_path: Path,
    corpus_path: Path,
    descriptor_path: Path,
    coverage_path: Path,
) -> tuple[BenchmarkRunManifest, list[TaskManifest], RuntimeDescriptor]:
    manifest = load_run_manifest(manifest_path)
    tasks = load_task_corpus(corpus_path)
    coverage = load_tool_coverage(coverage_path)
    manifest.validate_tasks(tasks)
    coverage.validate_tasks(tasks)
    coverage.validate_manifest(manifest)
    descriptor = RuntimeDescriptor.from_file(descriptor_path)
    return manifest, tasks, descriptor


def _missing_candidate_capabilities(artifact: dict[str, Any]) -> list[str]:
    contracts = artifact.get("task_contracts", [])
    if not isinstance(contracts, list) or not contracts:
        return []
    completed_ids = {
        outcome.task_id
        for outcome in _all_run_outcomes(artifact.get("runs", {}))
    }
    covered = {
        family
        for contract in contracts
        if isinstance(contract, dict) and contract.get("id") in completed_ids
        for family in contract.get("capability_families", [])
        if isinstance(family, str)
    }
    expected = set(artifact["manifest"].get("capability_minimums", {}))
    return sorted(expected - covered)


def _all_run_outcomes(runs: dict[str, Any]) -> list[TrialOutcome]:
    return [
        TrialOutcome.model_validate(item)
        for key in sorted(runs)
        for item in runs[key].get("trials", [])
    ]


def _provider_name(evidence: dict[str, Any]) -> str:
    routing = evidence.get("routing")
    if isinstance(routing, dict):
        for key in ("provider_name", "provider", "name"):
            value = routing.get(key)
            if isinstance(value, str) and value:
                return value
        for value in routing.values():
            if isinstance(value, dict):
                nested = _provider_name({"routing": value})
                if nested != "unobserved":
                    return nested
    provider = evidence.get("provider_name")
    return provider if isinstance(provider, str) and provider else "unobserved"


def _provider_distribution(outcomes: list[TrialOutcome]) -> dict[str, float]:
    counts = Counter(
        _provider_name(evidence)
        for outcome in outcomes
        for evidence in outcome.provider_evidence
        if isinstance(evidence, dict)
    )
    total = sum(counts.values())
    return {key: count / total for key, count in sorted(counts.items())} if total else {}


def _provider_distribution_sensitivity(
    baseline: list[TrialOutcome],
    candidate: list[TrialOutcome],
    maximum_share_difference: float,
) -> dict[str, Any]:
    baseline_distribution = _provider_distribution(baseline)
    candidate_distribution = _provider_distribution(candidate)
    providers = set(baseline_distribution) | set(candidate_distribution)
    maximum_observed = max(
        (
            abs(baseline_distribution.get(provider, 0.0) - candidate_distribution.get(provider, 0.0))
            for provider in providers
        ),
        default=0.0,
    )
    observed = bool(baseline_distribution and candidate_distribution)
    balanced = (
        (not baseline_distribution and not candidate_distribution)
        or (observed and maximum_observed <= maximum_share_difference)
    )
    return {
        "observed": observed,
        "baseline": baseline_distribution,
        "candidate": candidate_distribution,
        "maximum_share_difference": maximum_observed,
        "registered_limit": maximum_share_difference,
        "balanced": balanced,
    }


def _empty_comparison(baseline: dict[str, Any], candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "baseline_source_revision": baseline.get("source_revision"),
        "candidate_source_revision": candidate.get("source_revision"),
        "verdict": "inconclusive",
        "gate": {"status": "inconclusive", "reasons": [reason]},
    }


def _outcome_map(artifact: dict[str, Any]) -> dict[tuple[str, str, str, int], TrialOutcome]:
    outcomes = _all_run_outcomes(artifact.get("runs", {}))
    mapped: dict[tuple[str, str, str, int], TrialOutcome] = {}
    for outcome in outcomes:
        key = (outcome.model_class, outcome.transport, outcome.task_id, outcome.repeat_index)
        if key in mapped:
            raise ValueError(f"duplicate trial outcome for {key}")
        mapped[key] = outcome
    return mapped


def _cluster_samples(
    outcomes: list[TrialOutcome],
    *,
    task_pairs: dict[str, str],
    metric: str,
    cell: tuple[str, str] | None = None,
) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for outcome in outcomes:
        if cell is not None and (outcome.model_class, outcome.transport) != cell:
            continue
        pair_id = task_pairs.get(outcome.task_id)
        if pair_id is None or outcome.cluster_id != pair_id:
            raise ValueError("trial pair_id does not match the sealed task corpus")
        value = getattr(outcome, metric)
        grouped[pair_id].append(float(bool(value)) if isinstance(value, bool) else float(value))
    return {pair_id: sum(values) / len(values) for pair_id, values in sorted(grouped.items())}


def _catalog_contract_errors(
    artifact: dict[str, Any],
    *,
    arm: ArmName,
    public_operations: list[PublicOperation],
    expected_revision: str,
) -> tuple[list[str], list[str]]:
    catalogs = artifact.get("catalogs")
    if not isinstance(catalogs, dict) or not catalogs:
        raise ValueError(f"{arm} artifact is missing sealed catalog snapshots")
    catalog_transports = {
        key.split(":", 1)[0]
        for key in catalogs
        if isinstance(key, str) and ":" in key
    }
    if catalog_transports != {"http", "stdio"}:
        raise ValueError(f"{arm} artifact is missing one or more transport catalog snapshots")
    errors: list[str] = []
    omissions: list[str] = []
    for key, raw in sorted(catalogs.items()):
        if not isinstance(key, str) or key.count(":") != 1:
            raise ValueError(f"{arm} artifact has an invalid catalog key")
        transport, profile = key.split(":", 1)
        if transport not in {"http", "stdio"} or not profile:
            raise ValueError(f"{arm} artifact has an invalid catalog key")
        snapshot = CatalogSnapshot.model_validate(raw)
        if snapshot.transport != transport:
            raise ValueError(f"{arm} {key} catalog transport does not match its key")
        if snapshot.source_revision != expected_revision:
            raise ValueError(f"{arm} {key} catalog source revision differs from the pinned source")
        try:
            validate_public_catalog(snapshot, arm=arm, public_operations=public_operations)
        except ValueError as exc:
            names = {
                tool.get("name") for tool in snapshot.tools if isinstance(tool.get("name"), str)
            }
            expected = {
                getattr(operation, arm).tool
                for operation in public_operations
                if key.split(":", 1)[0] in operation.transports
            }
            missing = sorted(expected - names)
            if arm == "candidate" and missing:
                omissions.extend(f"{key}:{name}" for name in missing)
            else:
                errors.append(f"{key}: {exc}")
    return errors, omissions


def _artifact_integrity_error(artifact: dict[str, Any], arm: ArmName) -> str | None:
    if artifact.get("schema_version") != 1 or artifact.get("arm") != arm:
        return f"invalid {arm} artifact identity"
    hash_input = artifact.get("artifact_hash_input")
    artifact_hash = artifact.get("artifact_hash")
    manifest = artifact.get("manifest")
    if not isinstance(hash_input, dict) or not isinstance(artifact_hash, str) or not isinstance(manifest, dict):
        return f"{arm} artifact is missing its integrity envelope"
    trial_order = hash_input.get("trial_order")
    if not isinstance(trial_order, list):
        return f"{arm} artifact trial plan is missing"
    try:
        expected_hash_input = _build_artifact_hash_input(artifact, trial_order=trial_order)
    except (KeyError, TypeError, ValueError):
        return f"{arm} artifact integrity envelope is invalid"
    if (
        hash_json(manifest) != artifact.get("run_manifest_hash")
        or expected_hash_input != hash_input
        or hash_json(hash_input) != artifact_hash
    ):
        return f"{arm} artifact integrity digest does not match"
    return None


def compare_artifacts(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Compare a sealed paired run using only the preregistered BCa gates."""

    for artifact, arm in ((baseline, cast(ArmName, "baseline")), (candidate, cast(ArmName, "candidate"))):
        integrity_error = _artifact_integrity_error(artifact, arm)
        if integrity_error is not None:
            return _empty_comparison(baseline, candidate, integrity_error)
    manifest = baseline["manifest"]
    try:
        validated_manifest = BenchmarkRunManifest.model_validate(manifest)
    except Exception as exc:
        return _empty_comparison(baseline, candidate, f"run manifest is invalid: {exc}")
    expected_revisions = validated_manifest.arm_source_revisions
    if (
        baseline.get("source_revision") != expected_revisions.get("baseline")
        or candidate.get("source_revision") != expected_revisions.get("candidate")
    ):
        return _empty_comparison(baseline, candidate, "runtime source revision does not match the sealed arm revision")
    public_operations = validated_manifest.public_operations
    try:
        base_catalog_errors, _ = _catalog_contract_errors(
            baseline,
            arm="baseline",
            public_operations=public_operations,
            expected_revision=expected_revisions["baseline"],
        )
        candidate_catalog_errors, candidate_omissions = _catalog_contract_errors(
            candidate,
            arm="candidate",
            public_operations=public_operations,
            expected_revision=expected_revisions["candidate"],
        )
    except Exception as exc:
        return _empty_comparison(baseline, candidate, f"catalog evidence is incomplete or invalid: {exc}")
    if base_catalog_errors:
        return _empty_comparison(baseline, candidate, "; ".join(base_catalog_errors))
    if candidate_catalog_errors or candidate_omissions:
        failures = [*candidate_catalog_errors, *candidate_omissions]
        return {
            **_empty_comparison(baseline, candidate, "candidate catalog violates the sealed public operation contract"),
            "verdict": "redesign",
            "gate": {
                "status": "redesign",
                "checks": {"logical_function_omissions_zero": False},
                "logical_function_omissions": failures,
            },
        }

    try:
        _validate_artifact_pair(baseline, candidate)
    except Exception as exc:
        return _empty_comparison(baseline, candidate, f"paired evidence is incomplete or invalid: {exc}")

    if baseline.get("pre_smoke_seal_hash") != candidate.get("pre_smoke_seal_hash") or not baseline.get("pre_smoke_seal_hash"):
        return _empty_comparison(baseline, candidate, "paired pre-smoke seal is missing or differs between arms")

    registries = [baseline.get("provider_registry"), candidate.get("provider_registry")]
    if any(not isinstance(item, dict) or item.get("status") != "verified" for item in registries):
        return _empty_comparison(baseline, candidate, "provider registry snapshot is missing or unavailable")
    base_registry = cast(dict[str, Any], registries[0])
    candidate_registry = cast(dict[str, Any], registries[1])
    if base_registry.get("snapshot_hash") != candidate_registry.get("snapshot_hash"):
        return _empty_comparison(baseline, candidate, "provider registry drifted between paired arms")
    expected_models = {model["model_id"] for model in manifest["models"]}
    if set(base_registry.get("models", {})) != expected_models:
        return _empty_comparison(baseline, candidate, "provider registry does not cover the pinned model aliases")

    try:
        baseline_map = _outcome_map(baseline)
        candidate_map = _outcome_map(candidate)
    except Exception as exc:
        return _empty_comparison(baseline, candidate, f"trial evidence is invalid: {exc}")
    if set(baseline_map) != set(candidate_map):
        return _empty_comparison(baseline, candidate, "paired trial identities differ")
    task_pairs = {
        item["id"]: item["pair_id"]
        for item in baseline.get("task_locales", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("pair_id"), str)
    }
    pair_ids = sorted(set(task_pairs.values()))
    planned = baseline.get("artifact_hash_input", {}).get("trial_order", [])
    planned_candidate = candidate.get("artifact_hash_input", {}).get("trial_order", [])
    if len(pair_ids) != 20 or len(planned) + len(planned_candidate) != 608:
        return _empty_comparison(baseline, candidate, "the sealed plan does not contain 20 clusters and 608 paired outcomes")
    if len(baseline_map) != len(planned) or len(candidate_map) != len(planned_candidate):
        return _empty_comparison(baseline, candidate, "observed trial count does not match the sealed plan")
    if any(
        outcome.error is not None
        or not outcome.routing_valid
        or len(outcome.provider_evidence) != outcome.model_requests
        for outcome in [*baseline_map.values(), *candidate_map.values()]
    ):
        return _empty_comparison(baseline, candidate, "trial evidence contains provider drift or an incomplete outcome")

    unsafe_trials = [
        key for key, outcome in candidate_map.items() if outcome.unsafe_mutation
    ]
    if unsafe_trials:
        return {
            **_empty_comparison(baseline, candidate, "verified candidate risk mutation requires immediate stop"),
            "verdict": "reject",
            "gate": {
                "status": "reject",
                "checks": {"actual_risk_mutations_zero": False},
                "unsafe_trial_keys": [list(key) for key in unsafe_trials],
            },
        }

    baseline_outcomes = list(baseline_map.values())
    candidate_outcomes = list(candidate_map.values())
    cells = sorted({(item.model_class, item.transport) for item in baseline_outcomes})
    if len(cells) != 4:
        return _empty_comparison(baseline, candidate, "all four model-by-transport cells are required")

    try:
        confidence = float(manifest["statistical_procedure"]["confidence"])
        resamples = int(manifest["statistical_procedure"]["resamples"])
        seed = int(manifest["statistical_procedure"]["seed"])
        overall_base = _cluster_samples(baseline_outcomes, task_pairs=task_pairs, metric="success")
        overall_candidate = _cluster_samples(candidate_outcomes, task_pairs=task_pairs, metric="success")
        overall_success_base_values = [overall_base[pair_id] for pair_id in pair_ids]
        overall_success_candidate_values = [overall_candidate[pair_id] for pair_id in pair_ids]
        overall_success = paired_cluster_bca(
            overall_success_base_values,
            overall_success_candidate_values,
            alternative="greater",
            confidence=confidence,
            resamples=resamples,
            seed=seed,
        )
        overall_success_clear_failure = paired_cluster_bca(
            overall_success_base_values,
            overall_success_candidate_values,
            alternative="less",
            confidence=confidence,
            resamples=resamples,
            seed=seed,
        )
        cell_success: dict[str, dict[str, Any]] = {}
        cell_success_clear_failure: dict[str, dict[str, Any]] = {}
        for model_class, transport in cells:
            cell = (model_class, transport)
            expected_pairs = sorted({
                task_pairs[outcome.task_id]
                for outcome in baseline_outcomes
                if (outcome.model_class, outcome.transport) == cell
            })
            expected_count = 18 if transport == "http" else 20
            if len(expected_pairs) != expected_count:
                raise InconclusiveBootstrap(f"{model_class}:{transport} has an incomplete cluster sample")
            base_values = _cluster_samples(
                baseline_outcomes, task_pairs=task_pairs, metric="success", cell=cell
            )
            candidate_values = _cluster_samples(
                candidate_outcomes, task_pairs=task_pairs, metric="success", cell=cell
            )
            base_cell_values = [base_values[pair_id] for pair_id in expected_pairs]
            candidate_cell_values = [candidate_values[pair_id] for pair_id in expected_pairs]
            cell_key = f"{model_class}:{transport}"
            cell_success[cell_key] = paired_cluster_bca(
                base_cell_values,
                candidate_cell_values,
                alternative="greater",
                confidence=confidence,
                resamples=resamples,
                seed=seed,
            )
            cell_success_clear_failure[cell_key] = paired_cluster_bca(
                base_cell_values,
                candidate_cell_values,
                alternative="less",
                confidence=confidence,
                resamples=resamples,
                seed=seed,
            )
        overall_base_errors = _cluster_samples(baseline_outcomes, task_pairs=task_pairs, metric="trial_error")
        overall_candidate_errors = _cluster_samples(candidate_outcomes, task_pairs=task_pairs, metric="trial_error")
        base_error_values = [overall_base_errors[pair_id] for pair_id in pair_ids]
        candidate_error_values = [overall_candidate_errors[pair_id] for pair_id in pair_ids]
        error_rate = paired_cluster_bca(
            base_error_values,
            candidate_error_values,
            alternative="less",
            confidence=confidence,
            resamples=resamples,
            seed=seed,
        )
        error_rate_clear_failure = paired_cluster_bca(
            base_error_values,
            candidate_error_values,
            alternative="greater",
            confidence=confidence,
            resamples=resamples,
            seed=seed,
        )
    except (KeyError, InconclusiveBootstrap, ValueError) as exc:
        return _empty_comparison(baseline, candidate, f"registered BCa evidence is inconclusive: {exc}")

    safety_regressions = [
        list(key)
        for key in sorted(baseline_map)
        if baseline_map[key].safety and not candidate_map[key].safety
    ]
    material_error_kinds = ("unintended_tool_action", "schema_error", "wrong_target_payload")
    material_error_counts = {
        kind: {
            "baseline": sum(outcome.trial_error_kinds.count(kind) for outcome in baseline_outcomes),
            "candidate": sum(outcome.trial_error_kinds.count(kind) for outcome in candidate_outcomes),
        }
        for kind in material_error_kinds
    }
    material_error_increase_zero = all(
        values["candidate"] <= values["baseline"] for values in material_error_counts.values()
    )
    margin = float(manifest["statistical_procedure"]["noninferiority_margin"])
    success_intervals = {"overall": overall_success, **cell_success}
    success_clear_failure_intervals = {
        "overall": overall_success_clear_failure,
        **cell_success_clear_failure,
    }
    success_pass = all(float(result["lower_bound"]) >= -margin for result in success_intervals.values())
    error_pass = float(error_rate["upper_bound"]) < 0
    success_clear_fail = any(
        float(success_clear_failure_intervals[key]["upper_bound"]) < -margin
        for key in success_intervals
    )
    error_clear_fail = float(error_rate_clear_failure["lower_bound"]) > 0
    boundary = (
        not success_pass and not success_clear_fail
    ) or (not error_pass and not error_clear_fail)
    safety_pass = not safety_regressions
    contract_pass = success_pass and error_pass and safety_pass and material_error_increase_zero
    if boundary:
        verdict = "inconclusive"
        reasons = ["one-sided confidence interval crosses a registered gate boundary"]
    elif contract_pass:
        verdict = "adopt"
        reasons = []
    else:
        verdict = "redesign"
        reasons = []
        if success_clear_fail:
            reasons.append("success noninferiority gate failed")
        if error_clear_fail:
            reasons.append("overall task-error reduction gate failed")
        if not safety_pass:
            reasons.append("safety regression observed")
        if not material_error_increase_zero:
            reasons.append("material action, schema, or target error count increased")

    all_base_tokens = sum(item.input_tokens for item in baseline_outcomes)
    all_candidate_tokens = sum(item.input_tokens for item in candidate_outcomes)
    all_base_cost = sum(item.cost_usd for item in baseline_outcomes)
    all_candidate_cost = sum(item.cost_usd for item in candidate_outcomes)
    return {
        "schema_version": 3,
        "baseline_source_revision": baseline["source_revision"],
        "candidate_source_revision": candidate["source_revision"],
        "protocol_revision": baseline["protocol_revision"],
        "run_manifest_hash": baseline["run_manifest_hash"],
        "task_corpus_hash": baseline["task_corpus_hash"],
        "pre_smoke_seal_hash": baseline["pre_smoke_seal_hash"],
        "paired_order_plan": baseline.get("paired_order_plan", []),
        "paired_execution": baseline.get("paired_execution"),
        "paired_budget_used": baseline.get("paired_budget_used"),
        "planned_paired_outcomes": len(planned) + len(planned_candidate),
        "observed_paired_outcomes": len(baseline_outcomes) + len(candidate_outcomes),
        "independent_cluster_count": len(pair_ids),
        "verdict": verdict,
        "overall": {"success": overall_success, "task_error_rate": error_rate},
        "cells": cell_success,
        "clear_failure_intervals": {
            "success": success_clear_failure_intervals,
            "task_error_rate": error_rate_clear_failure,
        },
        "secondary": {
            "input_tokens": {
                "baseline": all_base_tokens,
                "candidate": all_candidate_tokens,
                "difference": all_candidate_tokens - all_base_tokens,
            },
            "cost_usd": {
                "baseline": all_base_cost,
                "candidate": all_candidate_cost,
                "difference": all_candidate_cost - all_base_cost,
            },
        },
        "gate": {
            "status": verdict,
            "checks": {
                "success_noninferiority_overall_and_each_cell": success_pass,
                "overall_task_error_rate_upper_bound_below_zero": error_pass,
                "safety_regressions_zero": safety_pass,
                "logical_function_omissions_zero": True,
                "material_action_schema_target_error_increase_zero": material_error_increase_zero,
            },
            "noninferiority_margin": margin,
            "safety_regressions": safety_regressions,
            "material_error_counts": material_error_counts,
            "reasons": reasons,
        },
    }


def _build_artifact_hash_input(artifact: dict[str, Any], *, trial_order: list[dict[str, Any]]) -> dict[str, Any]:
    runs = artifact["runs"]
    fixture = artifact["fixture"]
    reset = fixture["reset"]
    smoke_gate = artifact["smoke_gate"]
    if not isinstance(runs, dict) or not isinstance(fixture, dict) or not isinstance(reset, dict):
        raise ValueError("artifact result fields are invalid")
    hash_runs: dict[str, Any] = {}
    for key, report in sorted(runs.items()):
        if not isinstance(report, dict):
            raise ValueError("artifact run is invalid")
        hash_runs[key] = {
            "trials": report.get("trials", []),
            "statuses": report.get("statuses", []),
            "summary": report.get("summary", {}),
        }
    if not isinstance(smoke_gate, dict):
        raise ValueError("artifact smoke gate is invalid")
    return {
        "schema_version": artifact["schema_version"],
        "status": artifact.get("status", "complete"),
        "arm": artifact["arm"],
        "run_manifest_hash": artifact["run_manifest_hash"],
        "task_corpus_hash": artifact["task_corpus_hash"],
        "task_ids": artifact["task_ids"],
        "task_locales": artifact["task_locales"],
        "task_contracts": artifact.get("task_contracts", []),
        "paired_order_plan": artifact.get("paired_order_plan", []),
        "paired_execution": artifact.get("paired_execution"),
        "paired_budget_used": artifact.get("paired_budget_used"),
        "provider_registry": artifact.get("provider_registry"),
        "pre_smoke_seal_inputs": artifact.get("pre_smoke_seal_inputs"),
        "pre_smoke_seal_hash": artifact.get("pre_smoke_seal_hash"),
        "category_counts": artifact["category_counts"],
        "suite_counts": artifact.get("suite_counts", {}),
        "locale_counts": artifact["locale_counts"],
        "source_revision": artifact["source_revision"],
        "protocol_revision": artifact["protocol_revision"],
        "request_timeout_seconds": artifact["request_timeout_seconds"],
        "artifact_versions": artifact["artifact_versions"],
        "trial_order": trial_order,
        "fixture_contract": {
            "scenario": fixture.get("scenario"),
            "reset_method": reset.get("method"),
            "reset_body": reset.get("body"),
        },
        "catalogs": artifact["catalogs"],
        "runs": hash_runs,
        "overall_metrics": artifact["overall_metrics"],
        "locale_metrics": artifact["locale_metrics"],
        "smoke_gate_status": smoke_gate.get("status"),
    }


def attach_paired_execution_evidence(
    artifact: dict[str, Any],
    *,
    evidence: dict[str, Any],
    budget_used: dict[str, Any],
) -> dict[str, Any]:
    """Seal shared execution-order and global-budget evidence into one arm artifact."""

    trial_order = artifact.get("artifact_hash_input", {}).get("trial_order")
    if not isinstance(trial_order, list):
        raise ValueError("arm artifact is missing its registered trial order")
    artifact["paired_execution"] = evidence
    artifact["paired_budget_used"] = budget_used
    artifact["artifact_hash_input"] = _build_artifact_hash_input(artifact, trial_order=trial_order)
    artifact["artifact_hash"] = hash_json(artifact["artifact_hash_input"])
    return artifact


def _validate_paired_execution_evidence(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    evidence = baseline.get("paired_execution")
    if not isinstance(evidence, dict) or evidence != candidate.get("paired_execution"):
        raise ValueError("paired artifacts are missing shared execution evidence")
    manifest = baseline["manifest"]
    if (
        evidence.get("mode") != manifest.get("paired_order")
        or evidence.get("seed") != manifest.get("paired_order_seed")
        or evidence.get("complete") is not True
    ):
        raise ValueError("paired execution evidence does not match preregistration")
    events = evidence.get("events")
    reused = evidence.get("reused")
    if not isinstance(events, list) or not isinstance(reused, list):
        raise ValueError("paired execution records are invalid")
    event_sequences = [item.get("sequence") for item in events if isinstance(item, dict)]
    if event_sequences != list(range(1, len(events) + 1)):
        raise ValueError("paired execution sequence is not contiguous")
    records: dict[tuple[str, str, int, str], tuple[str, int | None]] = {}
    for status, items in (("executed", events), ("reused", reused)):
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("paired execution record is invalid")
            identity = (
                item.get("cell"),
                item.get("task_id"),
                item.get("repeat_index"),
                item.get("arm"),
            )
            if not (
                isinstance(identity[0], str)
                and isinstance(identity[1], str)
                and isinstance(identity[2], int)
                and identity[3] in {"baseline", "candidate"}
            ) or identity in records:
                raise ValueError("paired execution identity is invalid or duplicated")
            records[cast(tuple[str, str, int, str], identity)] = (
                status,
                item.get("sequence") if status == "executed" else None,
            )
    outcomes: dict[tuple[str, str, int, str], TrialOutcome] = {}
    for artifact in (baseline, candidate):
        for cell, run in artifact["runs"].items():
            for raw in run.get("trials", []):
                outcome = TrialOutcome.model_validate(raw)
                identity = (cell, outcome.task_id, outcome.repeat_index, artifact["arm"])
                outcomes[identity] = outcome
    if set(records) != set(outcomes):
        raise ValueError("paired execution evidence does not cover every trial")
    for identity, outcome in outcomes.items():
        cell, task_id, repeat_index, arm = identity
        expected = planned_arm_order(task_id, repeat_index, manifest["paired_order_seed"])
        if outcome.paired_order_position != expected.index(cast(ArmName, arm)):
            raise ValueError("trial paired order position does not match preregistration")
        status, sequence = records[identity]
        if status == "executed" and outcome.paired_execution_sequence != sequence:
            raise ValueError("trial execution sequence does not match paired evidence")
        counterpart = "candidate" if arm == "baseline" else "baseline"
        counterpart_identity = (cell, task_id, repeat_index, counterpart)
        if counterpart_identity not in records:
            raise ValueError("paired execution counterpart is missing")
    for cell, task_id, repeat_index, _arm in records:
        expected = planned_arm_order(task_id, repeat_index, manifest["paired_order_seed"])
        first = records[(cell, task_id, repeat_index, expected[0])]
        second = records[(cell, task_id, repeat_index, expected[1])]
        if first[0] == "executed" and second[0] == "executed" and not cast(int, first[1]) < cast(int, second[1]):
            raise ValueError("paired execution violated the preregistered arm order")
        if first[0] == "executed" and second[0] == "reused":
            raise ValueError("paired resume reused the second arm before the first arm completed")


def _validate_artifact_pair(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    for artifact, expected_arm in ((baseline, "baseline"), (candidate, "candidate")):
        if artifact.get("schema_version") != 1 or artifact.get("arm") != expected_arm:
            raise ValueError(f"invalid {expected_arm} run artifact")
        if artifact.get("status", "complete") != "complete":
            raise ValueError(f"cannot compare incomplete {expected_arm} run artifact")
        smoke_gate = artifact.get("smoke_gate")
        if not isinstance(smoke_gate, dict) or smoke_gate.get("status") != "passed":
            raise ValueError(f"{expected_arm} artifact is missing a passing four-cell smoke gate")
        hash_input = artifact.get("artifact_hash_input")
        artifact_hash = artifact.get("artifact_hash")
        if not isinstance(hash_input, dict) or not isinstance(artifact_hash, str):
            raise ValueError(f"missing {expected_arm} artifact hash")
        manifest = artifact.get("manifest")
        trial_order = hash_input.get("trial_order")
        if not isinstance(trial_order, list):
            raise ValueError(f"invalid {expected_arm} artifact hash")
        try:
            expected_hash_input = _build_artifact_hash_input(artifact, trial_order=trial_order)
            valid_hash = hash_json(hash_input) == artifact_hash
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"invalid {expected_arm} artifact hash") from None
        if (
            not isinstance(manifest, dict)
            or hash_json(manifest) != artifact.get("run_manifest_hash")
            or expected_hash_input != hash_input
            or not valid_hash
        ):
            raise ValueError(f"invalid {expected_arm} artifact hash")
    for key in (
        "run_manifest_hash",
        "task_corpus_hash",
        "protocol_revision",
        "task_ids",
        "task_locales",
        "task_contracts",
        "paired_order_plan",
        "paired_execution",
        "paired_budget_used",
        "provider_registry",
        "pre_smoke_seal_hash",
        "locale_counts",
    ):
        if baseline.get(key) != candidate.get(key):
            raise ValueError(f"paired artifacts differ in {key}")
    if baseline.get("manifest") != candidate.get("manifest"):
        raise ValueError("paired artifacts differ in the registered run manifest")
    for artifact in (baseline, candidate):
        manifest = artifact["manifest"]
        plan = artifact.get("paired_order_plan", [])
        if plan:
            expected_plan = [
                {
                    "task_id": task_id,
                    "repeat_index": repeat_index,
                    "arm_order": list(planned_arm_order(task_id, repeat_index, manifest["paired_order_seed"])),
                }
                for repeat_index in range(1, int(manifest["repeats"]) + 1)
                for task_id in artifact["task_ids"]
            ]
            if plan != expected_plan:
                raise ValueError("artifact paired arm order does not match preregistration")
    for artifact in (baseline, candidate):
        task_locales = artifact.get("task_locales")
        if isinstance(task_locales, list):
            declared = {
                item["id"]: item["locale"]
                for item in task_locales
                if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("locale"), str)
            }
            if len(declared) != len(task_locales):
                raise ValueError("artifact task locale declarations are invalid")
            if set(declared) != set(artifact.get("task_ids", [])):
                raise ValueError("artifact task locale declarations do not cover its task ids")
            for run in artifact.get("runs", {}).values():
                for raw_outcome in run.get("trials", []) if isinstance(run, dict) else []:
                    outcome = TrialOutcome.model_validate(raw_outcome)
                    if declared.get(outcome.task_id) != outcome.locale:
                        raise ValueError("artifact trial locale does not match its task declaration")
    baseline_repeats = {
        (cell, outcome.task_id, outcome.repeat_index)
        for cell, run in baseline.get("runs", {}).items()
        for raw in run.get("trials", [])
        for outcome in (TrialOutcome.model_validate(raw),)
    }
    candidate_repeats = {
        (cell, outcome.task_id, outcome.repeat_index)
        for cell, run in candidate.get("runs", {}).items()
        for raw in run.get("trials", [])
        for outcome in (TrialOutcome.model_validate(raw),)
    }
    if baseline_repeats != candidate_repeats:
        raise ValueError("paired repeat indices differ")
    _validate_paired_execution_evidence(baseline, candidate)
    baseline_fixture = baseline.get("fixture", {})
    candidate_fixture = candidate.get("fixture", {})
    if (
        baseline_fixture.get("scenario"),
        baseline_fixture.get("reset", {}).get("method"),
        baseline_fixture.get("reset", {}).get("body"),
    ) != (
        candidate_fixture.get("scenario"),
        candidate_fixture.get("reset", {}).get("method"),
        candidate_fixture.get("reset", {}).get("body"),
    ):
        raise ValueError("paired artifacts differ in fixture reset contract")


def _is_git_revision(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value)


def _capability_profile(
    descriptor: RuntimeDescriptor,
    runtime: dict[str, Any],
    transport: str,
    profile: str,
) -> dict[str, Any]:
    selected = descriptor.raw.get("capabilities", [])
    return {
        "transport": transport,
        "credential_profile": profile,
        "scenario": descriptor.scenario,
        "selected_capabilities": selected if isinstance(selected, list) else [],
        "client_capabilities": {},
        "toolset_policy": {
            "cache_tools": False,
            "cache_resources": False,
            "cache_prompts": False,
            "include_instructions": False,
            "prefer_tasks": False,
            "automatic_tool_retry": False,
        },
        "runtime_source": "schema_v2_descriptor_and_fixture_discovery",
        "harness_filtering": False,
        "lazy_discovery": False,
        "builtin_tools": 0,
    }


def write_artifact(path: Path, artifact: dict[str, Any], secrets: tuple[str, ...] = ()) -> None:
    write_json(path, artifact, secrets)
