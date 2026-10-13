"""Benchmark orchestration and paired comparison."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
import time
from collections.abc import Mapping
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, cast

import httpx

from .catalog import capture_catalog, input_schemas_from_catalog
from .amendment import AmendmentPlan, validate_artifact_lineage
from .checkpoint import (
    CheckpointHeader,
    CheckpointDocument,
    CheckpointKey,
    CheckpointStore,
    SmokeModelClass,
    SmokeTransport,
    checkpoint_key_digest,
    checkpoint_record_hash,
    provider_registry_snapshot_hash,
    smoke_record_hash,
    spent_hash,
    valid_completed_outcome,
    smoke_outcome_failure_reason,
    timing_hash,
)
from .contracts import (
    ArmName,
    BenchmarkRunManifest,
    CatalogSnapshot,
    ModelSpec,
    PublicOperation,
    TaskManifest,
    hash_json,
    load_run_manifest,
    load_task_corpus,
    load_tool_coverage,
    validate_public_catalog,
)
from .evidence import (
    redact_exception,
    redact_text,
    safe_json,
    serialize_report,
    validate_model_input_evidence,
    write_json,
)
from .execution import (
    BudgetLedger,
    BudgetExceeded,
    TrialExecutor,
    TrialOutcome,
    MetricsReportEvaluator,
    TrialEvaluator,
    build_model,
    canonicalize_arguments,
    decode_raw_args,
    evaluate_dataset,
    execute_smoke,
    has_measured_evidence,
    has_server_instructions_evidence,
    _captured_material_positions_by_stage,
    provider_response_cost,
    provider_token_totals,
    registered_route_target,
    summarize_outcomes,
    summarize_outcomes_by_locale,
    validate_routing_evidence,
    validate_model_configuration,
    worst_case_cost,
)
from .selection import ToolGuidanceSelection
from .pairing import planned_arm_order
from .runtime import RuntimeContractError, RuntimeDescriptor, RuntimeFixture, StateObservation
from .statistics import InconclusiveBootstrap, paired_cluster_bca
from .timing import TimingCategory, TimingTracker


_OPENROUTER_CANONICAL_IDENTITIES = {
    ("deepseek/deepseek-v4-flash-0731", "20260731"): "deepseek/deepseek-v4-flash-20260731",
    ("qwen/qwen3.8-27b", "20260814"): "qwen/qwen3.8-27b-20260814",
}


def _expected_openrouter_canonical_slug(spec: ModelSpec) -> str:
    canonical_slug = _OPENROUTER_CANONICAL_IDENTITIES.get((spec.model_id, spec.version))
    if canonical_slug is None:
        raise ValueError(f"provider model alias/version is not pinned for {spec.class_name}")
    return canonical_slug


def _validate_openrouter_model_identity(spec: ModelSpec, model_record: dict[str, Any]) -> None:
    if model_record.get("id") != spec.model_id:
        raise ValueError(f"provider model alias drifted for {spec.class_name}")
    canonical_slug = model_record.get("canonical_slug")
    if canonical_slug != _expected_openrouter_canonical_slug(spec):
        raise ValueError(f"provider model canonical identity drifted for {spec.class_name}")


def _validate_openrouter_endpoint_price_ceiling(spec: ModelSpec, endpoint: dict[str, Any]) -> None:
    """Compare registry per-token prompt/completion prices with manifest per-million ceilings."""
    pricing = endpoint.get("pricing")
    if not isinstance(pricing, dict):
        raise ValueError(f"provider endpoint pricing is missing for {spec.class_name}")

    limits = {
        "prompt": spec.input_cost_per_million_usd,
        "completion": spec.output_cost_per_million_usd,
    }
    for price_name, ceiling_per_million in limits.items():
        if price_name not in pricing:
            raise ValueError(f"provider endpoint {price_name} price is missing for {spec.class_name}")
        try:
            price_per_token = Decimal(str(pricing[price_name]))
        except InvalidOperation as exc:
            raise ValueError(f"provider endpoint {price_name} price is invalid for {spec.class_name}") from exc
        if not price_per_token.is_finite() or price_per_token <= 0:
            raise ValueError(f"provider endpoint {price_name} price must be finite and positive for {spec.class_name}")
        if price_per_token * Decimal(1_000_000) > Decimal(str(ceiling_per_million)):
            raise ValueError(f"provider endpoint {price_name} price exceeds the manifest ceiling for {spec.class_name}")


_DYNAMIC_PROVIDER_REGISTRY_FIELDS = frozenset(
    {
        "throughput",
        "latency",
        "uptime",
        "request_count",
        "latency_last_30m",
        "throughput_last_30m",
        "uptime_last_1d",
        "uptime_last_30m",
        "uptime_last_5m",
        "perf_last_30m_by_workload",
    }
)


def _provider_registry_invariant_snapshot(
    registry: dict[str, Any],
    manifest: BenchmarkRunManifest,
) -> dict[str, Any]:
    if registry.get("status") != "verified":
        raise RuntimeContractError("fresh provider registry verification is unavailable", stage="provider_registry")
    models = registry.get("models")
    expected_model_ids = {spec.model_id for spec in manifest.models}
    if not isinstance(models, dict) or set(models) != expected_model_ids:
        raise RuntimeContractError("provider registry model set drifted", stage="provider_registry")

    def without_dynamic_metrics(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: without_dynamic_metrics(item)
                for key, item in value.items()
                if key not in _DYNAMIC_PROVIDER_REGISTRY_FIELDS
            }
        if isinstance(value, list):
            return [without_dynamic_metrics(item) for item in value]
        return value

    invariant_models: dict[str, Any] = {}
    for spec in manifest.models:
        registered = models[spec.model_id]
        model_record = registered.get("model_record") if isinstance(registered, dict) else None
        selected_endpoint = registered.get("selected_endpoint") if isinstance(registered, dict) else None
        if not isinstance(model_record, dict) or not isinstance(selected_endpoint, dict):
            raise RuntimeContractError(
                f"provider registry identity is incomplete for {spec.class_name}",
                stage="provider_registry",
            )
        try:
            _validate_openrouter_model_identity(spec, model_record)
            _validate_openrouter_endpoint_price_ceiling(spec, selected_endpoint)
        except ValueError as exc:
            raise RuntimeContractError(str(exc), stage="provider_registry") from exc
        if registered_route_target(spec, registered) is None:
            raise RuntimeContractError(
                f"provider registry route or endpoint parameters drifted for {spec.class_name}",
                stage="provider_registry",
            )
        invariant_models[spec.model_id] = {
            "manifest_version": registered.get("manifest_version"),
            "model_record": without_dynamic_metrics(model_record),
            "selected_endpoint": without_dynamic_metrics(selected_endpoint),
        }
    return {"status": "verified", "models": invariant_models}


def _provider_registry_invariant_hash(
    registry: dict[str, Any],
    manifest: BenchmarkRunManifest,
) -> str:
    return hash_json(_provider_registry_invariant_snapshot(registry, manifest))


def _verify_provider_registry_revalidation(
    frozen: dict[str, Any],
    current: dict[str, Any],
    manifest: BenchmarkRunManifest,
) -> str:
    frozen_invariants = _provider_registry_invariant_snapshot(frozen, manifest)
    current_invariants = _provider_registry_invariant_snapshot(current, manifest)
    if current_invariants != frozen_invariants:
        raise RuntimeContractError(
            "fresh provider registry route, model, parameters, prices, or endpoint ceiling drifted",
            stage="provider_registry",
        )
    return hash_json(frozen_invariants)


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
        return tuple(dict.fromkeys((*self.secret_values(), self.token_for(profile))))

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

    def __init__(
        self,
        manifest: BenchmarkRunManifest,
        tasks: list[TaskManifest],
        *,
        initial_events: list[dict[str, Any]] | None = None,
        selection: ToolGuidanceSelection | None = None,
    ) -> None:
        self.manifest = manifest
        self.tasks = tasks
        self.selection = selection
        self._schedule: dict[str, list[tuple[str, int, ArmName]]] = {}
        for model in manifest.models:
            for transport in manifest.transports:
                cell = f"{model.class_name}:{transport}"
                self._schedule[cell] = [
                    (task.id, repeat_index, arm)
                    for repeat_index in range(1, manifest.repeats + 1)
                    for task in tasks
                    if transport in task.fixture.transports
                    and (
                        selection is None
                        or selection.contains(model.class_name, transport, task.id, repeat_index)
                    )
                    for arm in planned_arm_order(task.id, repeat_index, manifest.paired_order_seed)
                ]
        self._positions = {cell: 0 for cell in self._schedule}
        self._registered: set[ArmName] = set()
        self._reused: dict[ArmName, set[tuple[str, str, int]]] = {
            "baseline": set(),
            "candidate": set(),
        }
        self._reused_outcomes: dict[ArmName, dict[tuple[str, str, int], TrialOutcome]] = {
            "baseline": {},
            "candidate": {},
        }
        self._events = [dict(event) for event in (initial_events or [])]
        sequences = [event.get("sequence") for event in self._events]
        if sequences != list(range(1, len(self._events) + 1)):
            raise RuntimeContractError("paired continuation history must have a contiguous execution sequence")
        self._sequence = len(self._events)
        self._events_by_identity: dict[tuple[str, str, int, ArmName], list[dict[str, Any]]] = defaultdict(list)
        self._terminal_event_sequences: set[int] = set()
        for event in self._events:
            identity = (
                event["cell"],
                event["task_id"],
                event["repeat_index"],
                cast(ArmName, event["arm"]),
            )
            self._events_by_identity[identity].append(event)
        self._historical_identities = set(self._events_by_identity)
        self._condition = asyncio.Condition()
        self._provider_registry: dict[str, Any] | None = None
        self._provider_registry_lock = asyncio.Lock()
        self._pre_smoke_seals: dict[ArmName, str] = {}
        self._pre_smoke_ready: set[ArmName] = set()
        self._continued_seal_hash: str | None = None

    async def register_arm(
        self,
        arm: ArmName,
        reused: Mapping[tuple[str, str, int], TrialOutcome],
    ) -> None:
        async with self._condition:
            if arm in self._registered:
                raise RuntimeContractError(f"paired arm {arm} registered twice")
            self._reused_outcomes[arm] = dict(reused)
            for identity, outcome in reused.items():
                cell, task_id, repeat_index = identity
                expected = planned_arm_order(task_id, repeat_index, self.manifest.paired_order_seed)
                if outcome.paired_order_position != expected.index(arm):
                    raise RuntimeContractError(
                        f"paired resume evidence is missing or invalid for {cell}:{task_id}:{repeat_index}:{arm}"
                    )
                self._reused[arm].add(identity)
                sequence = outcome.paired_execution_sequence
                if sequence is not None:
                    event = next(
                        (item for item in self._events if item.get("sequence") == sequence),
                        None,
                    )
                    if event is not None:
                        event_identity = (
                            event.get("cell"),
                            event.get("task_id"),
                            event.get("repeat_index"),
                            event.get("arm"),
                        )
                        if event_identity != (cell, task_id, repeat_index, arm):
                            raise RuntimeContractError("paired resume outcome points at another execution event")
                        self._terminal_event_sequences.add(sequence)
            self._registered.add(arm)
            if self._registered == {"baseline", "candidate"}:
                self._validate_reused_prefixes()
                if self.selection is not None:
                    self._restore_selection_events()
                for cell in self._schedule:
                    self._advance_reused(cell)
            self._condition.notify_all()

    def mark_terminal_outcome(self, outcome: TrialOutcome) -> None:
        """Bind a persisted outcome to the exact paired event that produced it."""

        sequence = outcome.paired_execution_sequence
        if sequence is None:
            return
        event = next((item for item in self._events if item.get("sequence") == sequence), None)
        identity = (
            f"{outcome.model_class}:{outcome.transport}",
            outcome.task_id,
            outcome.repeat_index,
            outcome.arm,
        )
        if event is None or (
            event.get("cell"),
            event.get("task_id"),
            event.get("repeat_index"),
            event.get("arm"),
        ) != identity:
            raise RuntimeContractError("trial checkpoint does not match its paired execution event")
        self._terminal_event_sequences.add(sequence)

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

    def _restore_selection_events(self) -> None:
        restored: list[dict[str, Any]] = []
        for arm, reused in self._reused_outcomes.items():
            for (cell, task_id, repeat_index), outcome in reused.items():
                sequence = outcome.paired_execution_sequence
                order_position = outcome.paired_order_position
                if sequence is None or order_position is None:
                    raise RuntimeContractError("selection resume checkpoint is missing paired execution lineage")
                restored.append(
                    {
                        "sequence": sequence,
                        "cell": cell,
                        "task_id": task_id,
                        "repeat_index": repeat_index,
                        "arm": arm,
                        "order_position": order_position,
                    }
                )
        restored.sort(key=lambda event: event["sequence"])
        sequences = [event["sequence"] for event in restored]
        if sequences != list(range(1, len(restored) + 1)):
            raise RuntimeContractError("selection resume checkpoint has a gap or duplicate in paired execution lineage")
        seen: set[tuple[str, str, int, ArmName]] = set()
        per_cell: dict[str, list[tuple[str, int, ArmName]]] = defaultdict(list)
        for event in restored:
            key = (
                event["cell"],
                event["task_id"],
                event["repeat_index"],
                cast(ArmName, event["arm"]),
            )
            if key in seen:
                raise RuntimeContractError("selection resume checkpoint duplicates a paired execution event")
            seen.add(key)
            per_cell[event["cell"]].append(
                (event["task_id"], event["repeat_index"], cast(ArmName, event["arm"]))
            )
            expected_order = planned_arm_order(
                event["task_id"], event["repeat_index"], self.manifest.paired_order_seed
            ).index(cast(ArmName, event["arm"]))
            if event["order_position"] != expected_order:
                raise RuntimeContractError("selection resume checkpoint changed the paired arm order")
        for cell, restored_schedule in per_cell.items():
            if restored_schedule != self._schedule[cell][: len(restored_schedule)]:
                raise RuntimeContractError("selection resume checkpoint does not preserve the paired schedule prefix")
        self._events = restored
        self._sequence = len(restored)
        self._historical_identities = seen

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

    async def register_pre_smoke_seal(
        self,
        arm: ArmName,
        seal_hash: str,
        *,
        continued_seal_hash: str | None = None,
    ) -> str:
        async with self._condition:
            previous = self._pre_smoke_seals.get(arm)
            if previous is not None and previous != seal_hash:
                raise RuntimeContractError("paired arm changed its pre-smoke seal during resume")
            self._pre_smoke_seals[arm] = seal_hash
            if continued_seal_hash is not None:
                if self._continued_seal_hash is not None and self._continued_seal_hash != continued_seal_hash:
                    raise RuntimeContractError("paired continuation changed the immutable parent seal")
                self._continued_seal_hash = continued_seal_hash
            self._condition.notify_all()
            await self._condition.wait_for(lambda: set(self._pre_smoke_seals) == {"baseline", "candidate"})
            if self._continued_seal_hash is not None:
                return self._continued_seal_hash
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
            identity = (cell, task_id, repeat_index, arm)
            prior_events = self._events_by_identity.get(identity, [])
            event = {
                "sequence": turn.execution_sequence,
                "cell": cell,
                "task_id": task_id,
                "repeat_index": repeat_index,
                "arm": arm,
                "order_position": expected_position,
            }
            if prior_events:
                event["attempt"] = len(prior_events) + 1
                event["retry_of_sequence"] = prior_events[-1]["sequence"]
            self._events.append(event)
            self._events_by_identity[identity].append(event)
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
            "unfinished_starts": [
                dict(event)
                for event in self._events
                if event["sequence"] not in self._terminal_event_sequences
            ],
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
                if (cell, task_id, repeat_index, arm) not in self._historical_identities
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
        continuation: AmendmentPlan | None = None,
        selection: ToolGuidanceSelection | None = None,
        paired_budget_restored: bool = False,
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
        self.continuation = continuation
        self.selection = selection
        self.paired_budget_restored = paired_budget_restored
        if continuation is not None and selection is not None:
            raise ValueError("AKB-361 selection cannot reuse amendment continuation inputs")
        if continuation is not None and paired_coordinator is None:
            raise ValueError("AKB-361 continuation requires the paired execution coordinator")
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
                        if self.selection is not None and not self.selection.contains(
                            model_spec.class_name,
                            transport,
                            task.id,
                            repeat_index,
                        ):
                            continue
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
            expected_tasks={task.id: task for task in self.tasks},
            expected_smoke_cells=expected_smoke,
            secrets=resolver.secret_values(),
            continuation=(self.continuation.checkpoint_lineage[cast(ArmName, self.arm)] if self.continuation else None),
            selection_preflight=(self.selection.checkpoint_preflight() if self.selection is not None else None),
            resume=self.resume_path is not None,
            reuse_terminal_trials=self.selection is not None,
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
        tasks_by_id = {task.id: task for task in self.tasks}
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
                task = tasks_by_id.get(outcome.task_id)
                if task is None:
                    incomplete_reasons.add(f"benchmark incomplete: trial {outcome.task_id} is not registered")
                    continue
                if outcome.error and not valid_completed_outcome(outcome, task):
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
        expected_trials = (
            len(self.selection.selected_trials)
            if self.selection is not None
            else sum(
                1
                for model_spec in self.manifest.models
                for transport in self.manifest.transports
                for task in self.tasks
                if transport in task.fixture.transports
                for _repeat_index in range(1, self.manifest.repeats + 1)
            )
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
                    if record.status == "completed"
                    and valid_completed_outcome(record.outcome, tasks_by_id[record.key.task_id])
                )
                if checkpoint_snapshot is not None
                else sum(
                    1
                    for outcomes in self._completed_trials.values()
                    for outcome in outcomes
                    if valid_completed_outcome(outcome, tasks_by_id[outcome.task_id])
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
            checkpoint_evidence["smoke_status"] = checkpoint_doc.smoke_status
            checkpoint_evidence["smoke_cell_count"] = len(checkpoint_doc.smoke_gate)
            checkpoint_evidence["provider_registry_hash"] = checkpoint_doc.header.provider_registry_hash
            checkpoint_evidence["selection_preflight"] = checkpoint_doc.selection_preflight
            checkpoint_evidence["provider_registry_revalidation"] = [
                {
                    "snapshot_hash": item["snapshot_hash"],
                    "invariant_sha256": item.get("invariant_sha256"),
                }
                for item in checkpoint_doc.provider_registry_revalidation
            ]
            checkpoint_evidence["spent"] = checkpoint_doc.spent.model_dump(mode="json")
            checkpoint_evidence["spent_hash"] = checkpoint_doc.spent_hash
            checkpoint_evidence["timing_hash"] = checkpoint_doc.timing_hash
            if checkpoint_doc.lifecycle != "finalized":
                incomplete_reasons.add("benchmark incomplete: checkpoint lifecycle was not finalized")
            if len(checkpoint_doc.records) != completed_trials:
                incomplete_reasons.add("benchmark incomplete: checkpoint and artifact trial counts differ")
            if checkpoint_doc.reserved_cost_usd != 0:
                incomplete_reasons.add("benchmark incomplete: checkpoint has an orphaned reservation")
            if self.continuation is not None:
                parent_count = self.continuation.parent_trial_count(cast(ArmName, self.arm))
                checkpoint_evidence["reused_trials"] = parent_count
                checkpoint_evidence["new_trials"] = max(0, checkpoint_record_count - parent_count)
                checkpoint_evidence["rerun_trials"] = 0
        if ledger.reserved_cost_usd != Decimal("0"):
            incomplete_reasons.add("benchmark incomplete: artifact has an orphaned reservation")
        if checkpoint_doc is not None:
            budget_provider_setup_requests = checkpoint_doc.spent.provider_setup_requests
            budget_model_requests = checkpoint_doc.spent.model_requests - budget_provider_setup_requests
            budget_input_tokens = checkpoint_doc.spent.input_tokens
            budget_output_tokens = checkpoint_doc.spent.output_tokens
            budget_cost_usd = Decimal(str(checkpoint_doc.spent.cost_usd))
            budget_wall_seconds = checkpoint_doc.spent.wall_seconds
            budget_model_work_seconds = checkpoint_doc.spent.model_work_seconds
            budget_reserved = Decimal(str(checkpoint_doc.reserved_cost_usd))
        else:
            budget_provider_setup_requests = ledger.provider_setup_requests
            budget_model_requests = ledger.requests - budget_provider_setup_requests
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
            "schema_version": 2,
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
            "task_contracts": [task.model_dump(mode="json") for task in self.tasks],
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
            "execution_environment": self.pre_smoke_seal_inputs.get("environment"),
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
                "provider_setup_requests": budget_provider_setup_requests,
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
        if self.selection is not None:
            artifact["selection"] = self.selection.artifact_metadata()
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
        if self.continuation is not None:
            artifact["continuation_lineage"] = self.continuation.lineage
            runtime_provenance = self.continuation.runtime_provenance_for(cast(ArmName, self.arm))
            if runtime_provenance is not None:
                artifact["continuation_runtime_provenance"] = runtime_provenance
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
        cancelled_during_write = False
        if self._checkpoint_store is not None:
            async with self._checkpoint_lock:
                self._refresh_store_secrets()
                checkpoint_started = time.perf_counter()
                write_task = asyncio.create_task(
                    asyncio.to_thread(
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
                )
                try:
                    existed = await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    existed = await write_task
                    cancelled_during_write = True
                if self._timing is not None:
                    self._timing.record("checkpoint", checkpoint_started)
            if not existed:
                self._checkpoint_new_trials += 1
        if self.paired_coordinator is not None:
            self.paired_coordinator.mark_terminal_outcome(outcome)
        self._record_trial(run_key, outcome)
        if cancelled_during_write:
            raise asyncio.CancelledError

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
        catalog_server_instructions: Mapping[str, str] | None = None,
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
            task = self._smoke_task(transport)
            catalog_key = f"{transport}:{task.fixture.credential_profile}"
            server_instructions = (
                catalog_server_instructions.get(catalog_key)
                if catalog_server_instructions is not None
                else None
            )
            if catalog_server_instructions is not None and not server_instructions:
                raise RuntimeContractError(
                    f"captured catalog is missing server instructions for {catalog_key}",
                    stage="catalog_capture",
                )
            cached = (
                self._checkpoint_store.smoke_outcome_for(cell_key)
                if self._checkpoint_store is not None
                else None
            )
            if cached is not None:
                if not has_server_instructions_evidence(cached, server_instructions):
                    raise RuntimeContractError(
                        f"cached smoke gate cell {cell_key} does not prove sealed server instructions reached the model",
                        stage="smoke_gate",
                    )
                registered_model = self.provider_registry["models"][model_spec.model_id]
                route_observed, route_valid = validate_routing_evidence(
                    cached.provider_evidence,
                    model_spec,
                    registered_model,
                )
                if (cached.routing_observed, cached.routing_valid) != (route_observed, route_valid) or not route_valid:
                    raise RuntimeContractError(
                        f"cached smoke gate cell {cell_key} does not match its registered provider route",
                        stage="smoke_gate",
                    )
                return {
                    "cell": cell_key,
                    "status": "completed",
                    "reused": True,
                    "outcome": safe_json(cached.model_dump(mode="json"), self.secrets),
                }

            model = build_model(model_spec)
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

            async def checkpoint_smoke(
                settled_outcome: TrialOutcome,
                status: Literal["completed", "failed", "incomplete"],
            ) -> None:
                if self._checkpoint_store is None:
                    return
                async with self._checkpoint_lock:
                    self._refresh_store_secrets()
                    write_task = asyncio.create_task(
                        asyncio.to_thread(
                            self._checkpoint_store.record_smoke_cell,
                            cell_key,
                            settled_outcome,
                            status=status,
                            timing=self._timing_snapshot(),
                            elapsed_wall_seconds=(
                                self._ledger.current_wall_seconds()
                                if self._ledger is not None
                                else None
                            ),
                            reserved_cost_usd=(
                                float(self._ledger.reserved_cost_usd)
                                if self._ledger is not None
                                else 0.0
                            ),
                        )
                    )
                    try:
                        await asyncio.shield(write_task)
                    except asyncio.CancelledError:
                        await write_task
                        raise

            async def settle_smoke_charge(settled_outcome: TrialOutcome) -> None:
                charge_task = asyncio.create_task(ledger.charge(settled_outcome, guard=request_guard))
                try:
                    await asyncio.shield(charge_task)
                except asyncio.CancelledError:
                    try:
                        await charge_task
                    except BudgetExceeded as exc:
                        settled_outcome.error = f"benchmark incomplete: {exc}"
                        settled_outcome.failure_kind = "budget"
                    await checkpoint_smoke(settled_outcome, "failed")
                    raise
                except BudgetExceeded as exc:
                    settled_outcome.error = f"benchmark incomplete: {exc}"
                    settled_outcome.failure_kind = "budget"
                await checkpoint_smoke(settled_outcome, "failed")

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
                request_timeout_seconds = ledger.request_timeout_seconds()
                if self._timing is None:
                    outcome = await execute_smoke(
                        task,
                        manifest=self.manifest,
                        arm=self.arm,
                        model_spec=model_spec,
                        registered_model=self.provider_registry["models"][model_spec.model_id],
                        model=model,
                        transport=transport,
                        fixture=cell_fixture,
                        token=token,
                        secrets=self.secrets,
                        secret_values=resolver.secret_values,
                        request_timeout_seconds=request_timeout_seconds,
                        request_guard=request_guard,
                        input_schemas=input_schemas,
                        expected_server_instructions=server_instructions,
                        timing_sink=self._timing.record if self._timing is not None else None,
                    )
                else:
                    with self._timing.measure("model_execution"):
                        outcome = await execute_smoke(
                            task,
                            manifest=self.manifest,
                            arm=self.arm,
                            model_spec=model_spec,
                            registered_model=self.provider_registry["models"][model_spec.model_id],
                            model=model,
                            transport=transport,
                            fixture=cell_fixture,
                            token=token,
                            secrets=self.secrets,
                            secret_values=resolver.secret_values,
                            request_timeout_seconds=request_timeout_seconds,
                            request_guard=request_guard,
                            input_schemas=input_schemas,
                            expected_server_instructions=server_instructions,
                            timing_sink=self._timing.record if self._timing is not None else None,
                        )
                await settle_smoke_charge(outcome)
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
            registered_model = self.provider_registry["models"][model_spec.model_id]
            route_observed, route_valid = validate_routing_evidence(
                outcome.provider_evidence,
                model_spec,
                registered_model,
            )
            model_identity_matches = (
                outcome.model_class == model_spec.class_name
                and outcome.model_id == model_spec.model_id
                and outcome.transport == transport
            )
            route_matches = (
                (outcome.routing_observed, outcome.routing_valid) == (route_observed, route_valid)
                and route_valid
            )
            provider_models_match = all(
                item.get("model") == model_spec.model_id
                for item in outcome.provider_evidence
            )
            smoke_failure = smoke_outcome_failure_reason(outcome, task, server_instructions)
            valid = model_identity_matches and route_matches and provider_models_match and smoke_failure is None
            if not valid and outcome.error is None:
                if not model_identity_matches:
                    outcome.error = "smoke gate outcome did not match the requested model and transport"
                    outcome.failure_kind = "provider"
                elif not route_matches:
                    outcome.error = "smoke gate provider route did not match the registered route"
                    outcome.failure_kind = "provider"
                elif not provider_models_match:
                    outcome.error = "smoke gate provider receipt model did not match the requested model"
                    outcome.failure_kind = "provider"
                elif smoke_failure is not None:
                    outcome.error = f"smoke gate {smoke_failure}"
                    outcome.failure_kind = (
                        "tool"
                        if smoke_failure == "does not contain a successful MCP tool call"
                        else "terminal_response"
                        if smoke_failure == "does not contain a follow-up terminal response"
                        else "provider"
                    )
                else:
                    outcome.error = "smoke gate outcome failed an unclassified model or evidence check"
                    outcome.failure_kind = "provider"
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
            raw_outcome = item.get("outcome")
            failure_detail = raw_outcome.get("error") if isinstance(raw_outcome, dict) else None
            detail = failure_detail if isinstance(failure_detail, str) and failure_detail else (
                "returned incomplete model, provider, instruction, or terminal-response evidence"
            )
            raise RuntimeContractError(
                f"smoke gate cell {cell_key} failed: {detail}",
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
                        try:
                            _validate_openrouter_model_identity(spec, row)
                        except ValueError as exc:
                            raise RuntimeContractError(
                                str(exc),
                                stage="provider_registry",
                            ) from exc
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
                            and str(endpoint.get("provider_name", "")).casefold() == spec.routing.order[0].casefold()
                            and endpoint.get("quantization") == "fp8"
                        ]
                        if len(selected) != 1:
                            raise RuntimeContractError(
                                f"pinned {spec.routing.order[0]} fp8 endpoint is unavailable or ambiguous for {spec.class_name}",
                                stage="provider_registry",
                            )
                        try:
                            _validate_openrouter_endpoint_price_ceiling(spec, selected[0])
                        except ValueError as exc:
                            raise RuntimeContractError(str(exc), stage="provider_registry") from exc
                        registered_model = {
                            "manifest_version": spec.version,
                            "model_record": row,
                            "endpoint_snapshot": endpoints_response,
                            "selected_endpoint": selected[0],
                        }
                        if registered_route_target(spec, registered_model) is None:
                            raise RuntimeContractError(
                                f"provider registry endpoint identity drifted for {spec.class_name}",
                                stage="provider_registry",
                            )
                        registry_models[spec.model_id] = registered_model
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
        seal = {
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
            "paired_order_mode": self.manifest.paired_order,
            "paired_order_seed": self.manifest.paired_order_seed,
            "provider_registry_hash": self.provider_registry.get("snapshot_hash"),
            "provider_registry_status": self.provider_registry.get("status"),
        }
        if self.selection is not None:
            seal["selection"] = self.selection.artifact_metadata()
        return seal

    async def _bind_pre_smoke_seal(
        self,
        runtime: dict[str, Any],
        catalogs: dict[str, CatalogSnapshot],
    ) -> None:
        local_inputs = self._local_pre_smoke_seal(runtime, catalogs)
        local_hash = hash_json(local_inputs)
        continued_seal_hash: str | None = None
        if self.continuation is not None:
            arm = cast(ArmName, self.arm)
            self.continuation.validate_pre_smoke_inputs(
                arm,
                local_inputs,
                current_descriptor_raw=self.descriptor.raw,
                runtime=runtime,
                secrets=self.secrets,
            )
            continued_seal_hash = str(self.continuation.lineage["source_pre_smoke_seal_hash"])
            self.pre_smoke_seal_inputs = self.continuation.parent_pre_smoke_inputs(arm)
        else:
            self.pre_smoke_seal_inputs = local_inputs
        self.pre_smoke_seal_hash = (
            await self.paired_coordinator.register_pre_smoke_seal(
                cast(ArmName, self.arm),
                local_hash,
                continued_seal_hash=continued_seal_hash,
            )
            if self.paired_coordinator is not None
            else hash_json({self.arm: local_hash})
        )
        if self._checkpoint_store is not None:
            async with self._checkpoint_lock:
                await asyncio.to_thread(
                    self._checkpoint_store.bind_pre_smoke_seal,
                    self.pre_smoke_seal_hash,
                    self.provider_registry,
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
        catalog_server_instructions: Mapping[str, str] | None = None,
    ) -> None:
        model = build_model(model_spec)

        async def checkpoint_settled_outcome(outcome: TrialOutcome) -> None:
            identity = self._trial_identity(outcome)
            checkpoint_key = planned_by_identity.get(identity)
            if checkpoint_key is None:
                raise RuntimeContractError("settled trial is not an exact planned checkpoint input")
            await self._checkpoint_trial(
                run_key,
                checkpoint_key,
                outcome,
                status="failed",
            )

        executor = TrialExecutor(
            self.manifest,
            arm=self.arm,
            model_spec=model_spec,
            registered_model=self.provider_registry["models"][model_spec.model_id],
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
            server_instructions_by_profile=(
                {
                    profile: catalog_server_instructions[f"{transport}:{profile}"]
                    for profile in profiles
                }
                if catalog_server_instructions is not None
                else None
            ),
            outcome_sink=(checkpoint_settled_outcome if self._checkpoint_store is not None else None),
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
                executor.set_repeat_index(repeat_index)
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
                        if outcome.failure_kind in {"interrupted", "budget"}
                        or outcome.unsafe_mutation
                        or (outcome.provider_evidence and not outcome.routing_valid)
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
            elif not self.paired_budget_restored:
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
                outcome = self._checkpoint_store.reusable_outcome_for(planned_key)
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
                and (outcome := self._checkpoint_store.reusable_outcome_for(key)) is not None
            }
            await self.paired_coordinator.register_arm(cast(ArmName, self.arm), reused)
            await self.paired_coordinator.wait_until_registered()
        planned_by_run = self._planned_trials_by_run(planned_keys)
        pending_by_run = {
            run_key: [
                (task, repeat_index, key)
                for task, repeat_index, key in trials
                if self._checkpoint_store is None or self._checkpoint_store.reusable_outcome_for(key) is None
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
            if self.continuation is not None:
                self.provider_registry = self.continuation.parent_provider_registry()
            elif self._checkpoint_store is not None and self.resume_path is not None:
                frozen_registry = self._checkpoint_store.document.provider_registry_snapshot
                if frozen_registry is None:
                    raise RuntimeContractError(
                        "resume checkpoint is missing its frozen provider registry snapshot",
                        stage="provider_registry",
                    )
                if self._checkpoint_store.resumed_from_finalized:
                    self.provider_registry = frozen_registry
                else:
                    current_registry = await self._capture_provider_registry(ledger)
                    try:
                        current_hash = _verify_provider_registry_revalidation(
                            frozen_registry,
                            current_registry,
                            self.manifest,
                        )
                    except Exception:
                        async with self._checkpoint_lock:
                            await asyncio.to_thread(
                                self._checkpoint_store.record_provider_registry_revalidation,
                                current_registry,
                                invariant_sha256=None,
                            )
                        raise
                    async with self._checkpoint_lock:
                        await asyncio.to_thread(
                            self._checkpoint_store.record_provider_registry_revalidation,
                            current_registry,
                            invariant_sha256=current_hash,
                        )
                    self.provider_registry = frozen_registry
            else:
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
                catalog_server_instructions = {
                    key: str(snapshot.server_discover["instructions"])
                    for key, snapshot in catalogs.items()
                }
                await self._run_smoke_gate(
                    fixtures=fixtures,
                    resolver=resolver,
                    ledger=ledger,
                    catalog_input_schemas=catalog_input_schemas,
                    catalog_server_instructions=catalog_server_instructions,
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
                                catalog_server_instructions=catalog_server_instructions,
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
                        catalog_server_instructions=catalog_server_instructions,
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
                            reused_trials=(
                                self.continuation.parent_trial_count(cast(ArmName, self.arm))
                                if self.continuation is not None
                                else self._checkpoint_reused_trials
                            ),
                            rerun_trials=(0 if self.continuation is not None else self._checkpoint_rerun_trials),
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


_DERIVED_TRIAL_FIELDS = (
    "cluster_id",
    "state_before",
    "state_after",
    "state_observations_before",
    "state_observations_after",
    "state_available_before",
    "state_available_after",
    "state_contract_passed",
    "state_checks",
    "response_rubric_passed",
    "first_action_accuracy",
    "first_material_operation",
    "first_material_action_accuracy",
    "preparatory_call_count",
    "discouraged_preflight_calls",
    "material_call_count",
    "argument_validity",
    "required_operations_completed",
    "required_attempts_completed",
    "tool_outcome_match",
    "expected_error_match",
    "user_outcome_completed",
    "protocol_handshake_valid",
    "clarification_accuracy",
    "stopping_accuracy",
    "target_payload_accuracy",
    "multi_step_ordering",
    "result_binding_accuracy",
    "accepted_behavior_matched",
    "accepted_behavior_ids",
    "nonexistent_tool_attempts",
    "wrong_capability_calls",
    "wrong_resource_calls",
    "wrong_target_calls",
    "fabricated_resource_claims",
    "unsupported_success_claims",
    "post_completion_overshoot",
    "semantic_reasons",
    "trial_error",
    "trial_error_kinds",
    "forbidden_mutation_attempts",
    "unsafe_mutation",
    "success",
    "safety",
)


def _state_observations(
    outcome: TrialOutcome,
    task: TaskManifest,
    *,
    before: bool,
) -> list[StateObservation]:
    label = "before" if before else "after"
    raw_items = outcome.state_observations_before if before else outcome.state_observations_after
    expectations = task.expected_final_state.observation_sets
    if not isinstance(raw_items, list) or len(raw_items) != len(expectations):
        raise ValueError(f"{label}-state observations are missing or incomplete")
    observations: list[StateObservation] = []
    for expectation, raw in zip(expectations, raw_items, strict=True):
        if not isinstance(raw, dict) or set(raw) != {"available", "status_code", "payload", "error"}:
            raise ValueError(f"{label}-state observation record is invalid")
        available = raw["available"]
        status_code = raw["status_code"]
        error = raw["error"]
        if not isinstance(available, bool):
            raise ValueError(f"{label}-state availability is invalid")
        if status_code is not None and (
            not isinstance(status_code, int)
            or isinstance(status_code, bool)
            or not 100 <= status_code <= 599
        ):
            raise ValueError(f"{label}-state HTTP status is invalid")
        if error is not None and not isinstance(error, str):
            raise ValueError(f"{label}-state error is invalid")
        if before and not expectation.check_before and raw == {
            "available": True,
            "status_code": None,
            "payload": None,
            "error": None,
        }:
            observations.append(StateObservation(True, None, None, None))
            continue
        if available and (
            not isinstance(status_code, int)
            or isinstance(status_code, bool)
            or not 100 <= status_code <= 599
        ):
            raise ValueError(f"{label}-state HTTP status is missing or invalid")
        if available and error is not None:
            raise ValueError(f"{label}-state observation reports an error despite being available")
        if not available and not error:
            raise ValueError(f"{label}-state observation is unavailable without an error")
        if before and not expectation.check_before and not available:
            raise ValueError("unchecked before-state observations must be omitted explicitly")
        observations.append(
            StateObservation(
                available=available,
                status_code=status_code,
                payload=raw["payload"],
                error=error,
            )
        )
    return observations


def _validate_trial_evidence(
    outcome: TrialOutcome,
    task: TaskManifest,
    *,
    arm: str,
    model_spec: Any,
    registered_model: dict[str, Any],
    transport: str,
    public_operations: list[PublicOperation],
    input_schemas: dict[str, dict[str, Any]],
    consumer_root: str | Path | None,
) -> None:
    if (
        outcome.task_id != task.id
        or outcome.cluster_id != task.pair_id
        or outcome.category != task.category
        or outcome.locale != task.locale
        or outcome.arm != arm
        or outcome.model_class != model_spec.class_name
        or outcome.model_id != model_spec.model_id
        or outcome.transport != transport
    ):
        raise ValueError("trial identity differs from its registered task, model, transport, or arm")
    if not has_measured_evidence(outcome, task):
        raise ValueError("trial is missing provider, state, or raw call evidence")
    if outcome.error is not None:
        raise ValueError("trial outcome contains an execution error")
    if not all(math.isfinite(value) for value in (outcome.cost_usd, outcome.latency_seconds)):
        raise ValueError("trial usage contains a non-finite cost or latency")
    observed_routing, valid_routing = validate_routing_evidence(
        outcome.provider_evidence,
        model_spec,
        registered_model,
    )
    if (outcome.routing_observed, outcome.routing_valid) != (observed_routing, valid_routing) or not valid_routing:
        raise ValueError("trial routing flags do not match its provider responses")
    input_tokens, output_tokens = provider_token_totals(outcome.provider_evidence)
    provider_cost = provider_response_cost(outcome.provider_evidence)
    if (
        outcome.model_requests != len(outcome.provider_evidence)
        or (outcome.input_tokens, outcome.output_tokens) != (input_tokens, output_tokens)
        or outcome.total_tokens != input_tokens + output_tokens
        or provider_cost is None
        or outcome.provider_cost_usd is None
        or not math.isclose(outcome.provider_cost_usd, provider_cost, rel_tol=0.0, abs_tol=1e-12)
        or not math.isclose(outcome.cost_usd, provider_cost, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise ValueError("trial usage totals do not match its provider responses")

    before = _state_observations(outcome, task, before=True)
    after = _state_observations(outcome, task, before=False)
    capture_positions, captured_material_count = _captured_material_positions_by_stage(task, outcome.tool_calls)
    expected_checkpoints = [
        capture_positions.get(task.expected_material_attempts[checkpoint.after_attempt - 1].stage_id)
        for checkpoint in task.expected_final_state.checkpoints
    ]
    raw_checkpoints = outcome.state_checkpoint_observations
    recorded_checkpoints = [item.get("after_attempt") for item in raw_checkpoints if isinstance(item, dict)]
    if (
        any(position is None for position in expected_checkpoints)
        or recorded_checkpoints != expected_checkpoints
        or len(raw_checkpoints) != len(expected_checkpoints)
    ):
        raise ValueError("state checkpoint observations do not cover the registered checkpoints")
    for item in raw_checkpoints:
        if (
            not isinstance(item, dict)
            or set(item) != {"after_attempt", "available", "status_code", "payload", "error"}
            or not isinstance(item.get("after_attempt"), int)
            or isinstance(item.get("after_attempt"), bool)
            or item["after_attempt"] < 1
            or item["after_attempt"] > captured_material_count
            or not isinstance(item.get("available"), bool)
            or (item.get("error") is not None and not isinstance(item.get("error"), str))
        ):
            raise ValueError("state checkpoint observation is invalid")

    for call in outcome.tool_calls:
        decoded, raw_valid = decode_raw_args(call.raw_model_args)
        if call.raw_args_valid != raw_valid:
            raise ValueError("raw tool argument validity flag does not match the recorded call")
        if call.server_args is None:
            if call.server_args_equal_raw or call.effective_server_args is not None:
                raise ValueError("unobserved tool call claims server-side arguments")
        else:
            equal = raw_valid and call.server_args == decoded
            if call.server_args_equal_raw != equal:
                raise ValueError("raw and server tool arguments disagree with the recorded equality flag")
            schema = input_schemas.get(call.tool_name)
            if schema is not None and call.effective_server_args != canonicalize_arguments(call.server_args, schema):
                raise ValueError("effective tool arguments do not match the captured input schema")
        action = decoded.get("action") if isinstance(decoded, dict) else None
        routes = [
            operation
            for operation in public_operations
            if transport in operation.transports
            and getattr(operation, arm).tool == call.tool_name
            and getattr(operation, arm).action == (action if isinstance(action, str) else None)
        ]
        if len(routes) > 1:
            raise ValueError("tool call matches more than one registered public operation")
        expected_exists = call.tool_name in input_schemas
        if call.tool_exists != expected_exists:
            raise ValueError("tool existence flag does not match the captured catalog")
        if routes:
            route = routes[0]
            if (
                not call.tool_exists
                or call.canonical_operation != route.operation
                or call.surface_action != getattr(route, arm).action
                or call.logical_operation != route.logical_operation
                or call.resource_type != route.resource_type
            ):
                raise ValueError("tool call route metadata does not match the registered public operation")
        elif (
            call.canonical_operation is not None
            or call.surface_action is not None
            or call.logical_operation != "unknown"
            or call.resource_type != "unknown"
        ):
            raise ValueError("unregistered tool call carries fabricated operation metadata")
        if call.server_succeeded and (not call.transport_succeeded or call.server_error_code is not None):
            raise ValueError("successful tool call lacks a successful server response")
    if outcome.first_logical_operation != (outcome.tool_calls[0].logical_operation if outcome.tool_calls else "none"):
        raise ValueError("first action does not match the ordered raw call trace")
    if outcome.successful_mcp_tool_calls != sum(call.server_succeeded for call in outcome.tool_calls):
        raise ValueError("successful MCP call count does not match the raw call trace")

    verified = TrialOutcome.model_validate(outcome.model_dump(mode="python"))
    verified.finalize(task, before, after, consumer_root=consumer_root)
    actual_dump = outcome.model_dump(mode="json")
    verified_dump = verified.model_dump(mode="json")
    for field_name in _DERIVED_TRIAL_FIELDS:
        if actual_dump.get(field_name) != verified_dump.get(field_name):
            raise ValueError(f"trial {field_name} does not match the recorded state and call evidence")


def _validate_provider_registry_identity(
    registry: dict[str, Any],
    manifest: BenchmarkRunManifest,
    *,
    arm: str,
) -> None:
    model_list_snapshot = registry.get("model_list_snapshot")
    model_rows = model_list_snapshot.get("data") if isinstance(model_list_snapshot, dict) else None
    if (
        not isinstance(model_rows, list)
        or registry.get("model_list_hash") != hash_json(model_list_snapshot)
    ):
        raise ValueError(f"{arm} provider model-list snapshot is missing or invalid")

    registered_models = registry.get("models")
    endpoint_snapshots = registry.get("endpoint_snapshots")
    expected_aliases = {spec.model_id for spec in manifest.models}
    if (
        not isinstance(registered_models, dict)
        or set(registered_models) != expected_aliases
        or not isinstance(endpoint_snapshots, dict)
        or set(endpoint_snapshots) != expected_aliases
    ):
        raise ValueError(f"{arm} provider registry does not cover the pinned model aliases")

    for spec in manifest.models:
        matches = [
            row
            for row in model_rows
            if isinstance(row, dict) and row.get("id") == spec.model_id
        ]
        if len(matches) != 1:
            raise ValueError(f"{arm} provider registry does not contain exactly one pinned alias for {spec.class_name}")
        model_record = matches[0]
        try:
            _validate_openrouter_model_identity(spec, model_record)
        except ValueError as exc:
            raise ValueError(f"{arm} provider registry identity is invalid: {exc}") from exc

        captured_model = registered_models[spec.model_id]
        if (
            not isinstance(captured_model, dict)
            or captured_model.get("manifest_version") != spec.version
            or captured_model.get("model_record") != model_record
            or captured_model.get("endpoint_snapshot") != endpoint_snapshots.get(spec.model_id)
        ):
            raise ValueError(f"{arm} provider registry model evidence differs from the pinned alias and canonical identity")
        selected_endpoint = captured_model.get("selected_endpoint")
        if not isinstance(selected_endpoint, dict):
            raise ValueError(f"{arm} provider registry selected endpoint is invalid")
        try:
            _validate_openrouter_endpoint_price_ceiling(spec, selected_endpoint)
        except ValueError as exc:
            raise ValueError(f"{arm} provider registry pricing is invalid: {exc}") from exc
        if registered_route_target(spec, captured_model) is None:
            raise ValueError(f"{arm} provider registry endpoint evidence differs from the pinned {spec.routing.order[0]} fp8 route")


def _catalog_input_schemas(artifact: dict[str, Any], transport: str, profile: str) -> dict[str, dict[str, Any]]:
    key = f"{transport}:{profile}"
    raw = artifact.get("catalogs", {}).get(key)
    if not isinstance(raw, dict):
        raise ValueError(f"captured catalog is missing for {key}")
    snapshot = CatalogSnapshot.model_validate(raw)
    schemas: dict[str, dict[str, Any]] = {}
    for tool in snapshot.tools:
        name = tool.get("name")
        schema = tool.get("inputSchema")
        if not isinstance(name, str) or not isinstance(schema, dict):
            raise ValueError(f"captured catalog contains an invalid tool schema for {key}")
        schemas[name] = schema
    return schemas


def _validate_preregistered_inputs(
    artifact: dict[str, Any],
    *,
    manifest: BenchmarkRunManifest,
    tasks: list[TaskManifest],
    arm: str,
) -> RuntimeDescriptor:
    raw_tasks = artifact.get("task_contracts")
    if not isinstance(raw_tasks, list) or raw_tasks != [task.model_dump(mode="json") for task in tasks]:
        raise ValueError(f"{arm} artifact does not carry its complete task corpus")
    manifest.validate_tasks(tasks)
    expected_task_ids = [task.id for task in tasks]
    if artifact.get("task_ids") != expected_task_ids:
        raise ValueError(f"{arm} task ids do not match the sealed task corpus")
    expected_task_locales = [
        {"id": task.id, "locale": task.locale, "pair_id": task.pair_id}
        for task in tasks
    ]
    if artifact.get("task_locales") != expected_task_locales:
        raise ValueError(f"{arm} task locale and cluster declarations do not match the sealed task corpus")
    task_corpus_hash = hash_json([task.model_dump(mode="json") for task in tasks])
    oracle_hash = hash_json(
        {task.id: task.expected_final_state.model_dump(mode="json") for task in tasks}
    )
    if artifact.get("task_corpus_hash") != task_corpus_hash:
        raise ValueError(f"{arm} task corpus hash does not match its full task evidence")

    inputs = artifact.get("pre_smoke_seal_inputs")
    if not isinstance(inputs, dict) or inputs.get("schema_version") != 1:
        raise ValueError(f"{arm} pre-smoke seal inputs are missing or invalid")
    continuation_lineage = artifact.get("continuation_lineage")
    sealed_manifest_hash = (
        validate_artifact_lineage(artifact)
        if continuation_lineage is not None
        else artifact.get("run_manifest_hash")
    )
    selection = artifact.get("selection")
    if selection is not None and inputs.get("selection") != selection:
        raise ValueError(f"{arm} selection differs from its pre-smoke seal")
    if selection is None and "selection" in inputs:
        raise ValueError(f"{arm} pre-smoke seal contains an unbound selection")
    if (
        inputs.get("arm") != arm
        or inputs.get("source_revision") != artifact.get("source_revision")
        or inputs.get("run_manifest_hash") != sealed_manifest_hash
        or inputs.get("task_corpus_hash") != task_corpus_hash
        or inputs.get("oracle_hash") != oracle_hash
        or inputs.get("catalogs") != artifact.get("catalogs")
        or inputs.get("paired_order_plan") != artifact.get("paired_order_plan")
    ):
        raise ValueError(f"{arm} pre-smoke seal is not connected to its manifest, corpus, oracle, catalogs, or order plan")

    registry = artifact.get("provider_registry")
    if not isinstance(registry, dict) or registry.get("status") != "verified":
        raise ValueError(f"{arm} provider registry snapshot is unavailable")
    registry_hash = registry.get("snapshot_hash")
    registry_payload = {key: value for key, value in registry.items() if key != "snapshot_hash"}
    registry_models = registry.get("models")
    expected_models = {model.model_id for model in manifest.models}
    if (
        not isinstance(registry_hash, str)
        or hash_json(registry_payload) != registry_hash
        or not isinstance(registry_models, dict)
        or set(registry_models) != expected_models
        or inputs.get("provider_registry_hash") != registry_hash
        or inputs.get("provider_registry_status") != registry.get("status")
    ):
        raise ValueError(f"{arm} provider registry seal does not match the captured registry")
    _validate_provider_registry_identity(registry, manifest, arm=arm)

    fixture_inputs = inputs.get("fixture")
    fixture = artifact.get("fixture")
    if not isinstance(fixture_inputs, dict) or not isinstance(fixture, dict):
        raise ValueError(f"{arm} fixture seal is missing")
    reset = fixture.get("reset")
    if not isinstance(reset, dict):
        raise ValueError(f"{arm} fixture reset evidence is invalid")
    try:
        descriptor = RuntimeDescriptor.from_dict(fixture_inputs.get("runtime_descriptor"))
    except Exception as exc:
        raise ValueError(f"{arm} sealed runtime descriptor is invalid: {exc}") from exc
    descriptor_evidence = descriptor.raw.get("evidence")
    if not isinstance(descriptor_evidence, dict):
        raise ValueError(f"{arm} runtime descriptor omits artifact version evidence")
    descriptor_versions = {
        key: descriptor_evidence.get(key)
        for key in ("backend_artifact_version", "proxy_artifact_version")
        if key in artifact.get("artifact_versions", {})
    }
    if (
        descriptor_evidence.get("source_revision") != artifact.get("source_revision")
        or fixture_inputs.get("scenario") != fixture.get("scenario")
        or fixture_inputs.get("scenario") != descriptor.scenario
        or fixture_inputs.get("reset_url") != reset.get("url")
        or fixture_inputs.get("reset_body") != reset.get("body")
        or fixture_inputs.get("reset_url") != descriptor.reset_url
        or fixture_inputs.get("reset_body") != descriptor.reset_body
        or fixture_inputs.get("runtime_identity") != artifact.get("artifact_versions")
        or descriptor_versions != artifact.get("artifact_versions")
    ):
        raise ValueError(f"{arm} fixture seal does not match its runtime descriptor and artifact identity")

    environment = inputs.get("environment")
    if not isinstance(environment, dict) or artifact.get("execution_environment") != environment:
        raise ValueError(f"{arm} execution environment is not connected to its pre-smoke seal")
    packages = environment.get("packages")
    if (
        not isinstance(environment.get("python"), str)
        or not environment["python"]
        or not isinstance(environment.get("platform"), str)
        or not environment["platform"]
        or not isinstance(packages, dict)
        or set(packages) != {"pydantic-ai", "pydantic-evals", "fastmcp", "mcp", "scipy"}
        or any(value is not None and not isinstance(value, str) for value in packages.values())
        or not isinstance(environment.get("uv_lock_sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", environment["uv_lock_sha256"]) is None
    ):
        raise ValueError(f"{arm} execution environment evidence is incomplete")
    return descriptor


def _validate_shared_budget(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    manifest: BenchmarkRunManifest,
    tasks: list[TaskManifest],
    descriptors: dict[str, RuntimeDescriptor],
) -> None:
    budget = baseline.get("paired_budget_used")
    if not isinstance(budget, dict):
        raise ValueError("shared paired budget evidence is missing")
    if candidate.get("paired_budget_used") != budget:
        raise ValueError("paired artifacts do not share identical budget evidence")
    expected_budget_fields = {
        "model_requests",
        "provider_setup_requests",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
        "wall_seconds",
        "model_work_seconds",
        "max_total_cost_usd",
        "final_reserved_cost_usd",
    }
    selection = baseline.get("selection")
    if isinstance(selection, dict):
        expected_budget_fields.add("new_execution_limit_usd")
    if set(budget) != expected_budget_fields:
        raise ValueError("shared paired budget evidence has unexpected fields")
    if isinstance(selection, dict):
        selection_budget = selection.get("budget")
        if (
            not isinstance(selection_budget, dict)
            or budget.get("new_execution_limit_usd") != selection_budget.get("new_execution_limit_usd")
        ):
            raise ValueError("shared budget does not match the approved selection limit")
    try:
        final_reserved_cost = Decimal(str(budget["final_reserved_cost_usd"]))
    except (InvalidOperation, TypeError):
        raise ValueError("shared budget final reservation is invalid") from None
    if not final_reserved_cost.is_finite() or final_reserved_cost != 0:
        raise ValueError("shared budget retained an unsettled terminal reservation")
    exact_limits = {
        "max_total_cost_usd": manifest.budget.max_total_cost_usd,
    }
    for key, expected in exact_limits.items():
        observed = budget.get(key)
        if not isinstance(observed, (int, float)) or isinstance(observed, bool) or observed != expected:
            raise ValueError(f"shared budget {key} does not match preregistration")

    expected_cells = [
        f"{model.class_name}:{transport}"
        for model in manifest.models
        for transport in manifest.transports
    ]
    models: dict[str, ModelSpec] = {model.class_name: model for model in manifest.models}
    task_map = {task.id: task for task in tasks}
    aggregate = {"model_requests": 0, "input_tokens": 0, "output_tokens": 0}
    aggregate_cost = 0.0
    aggregate_work = 0.0
    for artifact, arm in ((baseline, "baseline"), (candidate, "candidate")):
        raw_smoke = artifact.get("smoke_gate")
        if not isinstance(raw_smoke, dict) or raw_smoke.get("status") != "passed":
            raise ValueError(f"{arm} smoke gate evidence is invalid")
        if raw_smoke.get("required_cells") != expected_cells:
            raise ValueError(f"{arm} smoke gate cells do not match the registered model and transport matrix")
        smoke_cells = raw_smoke.get("cells")
        if not isinstance(smoke_cells, list) or [item.get("cell") for item in smoke_cells if isinstance(item, dict)] != expected_cells:
            raise ValueError(f"{arm} smoke gate does not contain all four completed cells")
        descriptor = descriptors[arm]
        for item in smoke_cells:
            cell = item["cell"]
            if item.get("status") != "completed" or not isinstance(item.get("outcome"), dict):
                raise ValueError(f"{arm} smoke gate cell {cell} is incomplete")
            class_name, transport = cell.split(":", 1)
            outcome = TrialOutcome.model_validate(item["outcome"])
            task = task_map.get(outcome.task_id)
            model_spec = models.get(class_name)
            if task is None or model_spec is None or transport not in task.fixture.transports:
                raise ValueError(f"{arm} smoke gate cell {cell} has an invalid task or model")
            cell_descriptor = descriptor.benchmark_cells.get(cell, descriptor)
            consumer_root = (
                cell_descriptor.stdio_service.get("consumer_root")
                if transport == "stdio" and task.fixture.local_files and cell_descriptor.stdio_service is not None
                else None
            )
            schemas = _catalog_input_schemas(artifact, transport, task.fixture.credential_profile)
            _validate_trial_evidence(
                outcome,
                task,
                arm=arm,
                model_spec=model_spec,
                registered_model=artifact["provider_registry"]["models"][model_spec.model_id],
                transport=transport,
                public_operations=manifest.public_operations,
                input_schemas=schemas,
                consumer_root=consumer_root,
            )
            if (
                outcome.successful_mcp_tool_calls <= 0
                or outcome.model_requests < 2
                or not outcome.follow_up_terminal_response
            ):
                raise ValueError(f"{arm} smoke gate cell {cell} lacks a successful tool and terminal response")
            _add_budget_outcome(aggregate, outcome)
            aggregate_cost += max(outcome.cost_usd, outcome.provider_cost_usd or 0.0)
            aggregate_work += outcome.latency_seconds

        outcome_map = _outcome_map(artifact)
        for outcome in outcome_map.values():
            task = task_map.get(outcome.task_id)
            model_spec = models.get(outcome.model_class)
            if task is None or model_spec is None:
                raise ValueError(f"{arm} trial references a task or model outside the registered plan")
            cell = f"{outcome.model_class}:{outcome.transport}"
            cell_descriptor = descriptor.benchmark_cells.get(cell, descriptor)
            consumer_root = (
                cell_descriptor.stdio_service.get("consumer_root")
                if outcome.transport == "stdio" and task.fixture.local_files and cell_descriptor.stdio_service is not None
                else None
            )
            schemas = _catalog_input_schemas(artifact, outcome.transport, task.fixture.credential_profile)
            _validate_trial_evidence(
                outcome,
                task,
                arm=arm,
                model_spec=model_spec,
                registered_model=artifact["provider_registry"]["models"][model_spec.model_id],
                transport=outcome.transport,
                public_operations=manifest.public_operations,
                input_schemas=schemas,
                consumer_root=consumer_root,
            )
            _add_budget_outcome(aggregate, outcome)
            aggregate_cost += max(outcome.cost_usd, outcome.provider_cost_usd or 0.0)
            aggregate_work += outcome.latency_seconds

    for key, expected in aggregate.items():
        if budget.get(key) != expected:
            raise ValueError(f"shared budget {key} does not match the paired trial and smoke outcomes")
    if budget.get("total_tokens") != aggregate["input_tokens"] + aggregate["output_tokens"]:
        raise ValueError("shared budget total_tokens does not match provider evidence")
    cost = budget.get("cost_usd")
    wall = budget.get("wall_seconds")
    work = budget.get("model_work_seconds")
    setup_requests = budget.get("provider_setup_requests")
    if (
        not isinstance(cost, (int, float))
        or isinstance(cost, bool)
        or not math.isfinite(cost)
        or not math.isclose(cost, aggregate_cost, rel_tol=0.0, abs_tol=1e-9)
        or not isinstance(wall, (int, float))
        or isinstance(wall, bool)
        or not math.isfinite(wall)
        or wall <= 0
        or not isinstance(work, (int, float))
        or isinstance(work, bool)
        or not math.isfinite(work)
        or not math.isclose(work, aggregate_work, rel_tol=0.0, abs_tol=1e-9)
        or not isinstance(setup_requests, int)
        or isinstance(setup_requests, bool)
        or setup_requests < 0
    ):
        raise ValueError("shared paired budget costs, time, or setup-request evidence is invalid")
    if cost > manifest.budget.max_total_cost_usd:
        raise ValueError("shared paired usage exceeds a registered budget limit")


def _add_budget_outcome(aggregate: dict[str, int], outcome: TrialOutcome) -> None:
    aggregate["model_requests"] += outcome.model_requests
    aggregate["input_tokens"] += outcome.input_tokens
    aggregate["output_tokens"] += outcome.output_tokens


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
    result = {
        "schema_version": 4,
        "baseline_source_revision": baseline.get("source_revision"),
        "candidate_source_revision": candidate.get("source_revision"),
        "verdict": "inconclusive",
        "gate": {"status": "inconclusive", "reasons": [reason]},
    }
    if isinstance(baseline.get("selection"), dict) and isinstance(candidate.get("selection"), dict):
        result["observed_selected_diagnostics"] = _selected_diagnostic_summary(baseline, candidate)
    return result


def _selected_diagnostic_summary(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Summarize selected observations without changing the inconclusive gate."""

    plans: dict[str, set[tuple[str, str, str, int]]] = {}
    for arm, artifact in (("baseline", baseline), ("candidate", candidate)):
        selection = artifact.get("selection")
        raw_plan = selection.get("selected_trials") if isinstance(selection, dict) else None
        identities: set[tuple[str, str, str, int]] = set()
        for item in raw_plan if isinstance(raw_plan, list) else []:
            if not isinstance(item, dict):
                continue
            model_class, transport, task_id, repeat_index = (
                item.get("model_class"),
                item.get("transport"),
                item.get("task_id"),
                item.get("repeat_index"),
            )
            if (
                isinstance(model_class, str)
                and isinstance(transport, str)
                and isinstance(task_id, str)
                and type(repeat_index) is int
            ):
                identities.add((model_class, transport, task_id, repeat_index))
        plans[arm] = identities

    totals: dict[str, dict[str, Any]] = {}
    parsed_by_arm: dict[str, list[TrialOutcome]] = {}
    count_fields = (
        "observed_trials",
        "successes",
        "task_errors",
        "safe_trials",
        "unsafe_mutation_trials",
        "tool_attempts",
        "model_requests",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    )
    for arm, artifact in (("baseline", baseline), ("candidate", candidate)):
        runs = artifact.get("runs")
        plan = plans[arm]
        raw_rows: list[dict[str, Any]] = []
        excluded = 0
        if isinstance(runs, dict):
            for run in runs.values():
                if not isinstance(run, dict):
                    continue
                trials = run.get("trials")
                if not isinstance(trials, list):
                    continue
                for row in trials:
                    if not isinstance(row, dict):
                        excluded += 1
                        continue
                    identity = (
                        row.get("model_class"),
                        row.get("transport"),
                        row.get("task_id"),
                        row.get("repeat_index"),
                    )
                    if (
                        isinstance(identity[0], str)
                        and isinstance(identity[1], str)
                        and isinstance(identity[2], str)
                        and type(identity[3]) is int
                        and identity in plan
                    ):
                        raw_rows.append(row)
                    else:
                        excluded += 1

        totals[arm] = {key: 0 for key in count_fields}
        totals[arm]["observed_trials"] = len(raw_rows)
        totals[arm]["malformed_outcomes"] = 0
        totals[arm]["invalid_cost_rows"] = 0
        totals[arm]["unselected_or_unidentified_rows"] = excluded
        cost = Decimal("0")
        parsed: list[TrialOutcome] = []
        for row in raw_rows:
            totals[arm]["successes"] += row.get("success") is True
            totals[arm]["task_errors"] += row.get("trial_error") is True
            totals[arm]["safe_trials"] += row.get("safety") is True
            totals[arm]["unsafe_mutation_trials"] += row.get("unsafe_mutation") is True
            tool_calls = row.get("tool_calls")
            totals[arm]["tool_attempts"] += len(tool_calls) if isinstance(tool_calls, list) else 0
            for metric_name in ("model_requests", "input_tokens", "output_tokens", "total_tokens"):
                value = row.get(metric_name, 0)
                if type(value) is int and value >= 0:
                    totals[arm][metric_name] += value
            raw_cost = row.get("cost_usd", 0)
            try:
                row_cost = Decimal(str(raw_cost))
                if not row_cost.is_finite() or row_cost < 0:
                    raise InvalidOperation
                cost += row_cost
            except (InvalidOperation, TypeError, ValueError):
                totals[arm]["invalid_cost_rows"] += 1
            try:
                parsed.append(TrialOutcome.model_validate(row))
            except ValueError:
                totals[arm]["malformed_outcomes"] += 1
        totals[arm]["cost_usd"] = str(cost)
        totals[arm]["parsed_outcomes"] = len(parsed)
        totals[arm]["efficiency_metrics"] = summarize_outcomes(parsed)
        parsed_by_arm[arm] = parsed

    combined: dict[str, Any] = {
        key: sum(int(totals[arm][key]) for arm in ("baseline", "candidate")) for key in count_fields
    }
    combined.update(
        {
            "malformed_outcomes": sum(int(totals[arm]["malformed_outcomes"]) for arm in ("baseline", "candidate")),
            "invalid_cost_rows": sum(int(totals[arm]["invalid_cost_rows"]) for arm in ("baseline", "candidate")),
            "unselected_or_unidentified_rows": sum(
                int(totals[arm]["unselected_or_unidentified_rows"]) for arm in ("baseline", "candidate")
            ),
            "cost_usd": str(sum((Decimal(totals[arm]["cost_usd"]) for arm in ("baseline", "candidate")), start=Decimal("0"))),
            "parsed_outcomes": sum(int(totals[arm]["parsed_outcomes"]) for arm in ("baseline", "candidate")),
            "efficiency_metrics": summarize_outcomes(parsed_by_arm["baseline"] + parsed_by_arm["candidate"]),
        }
    )
    return {
        "classification": "diagnostic_only",
        "adoption_eligible": False,
        "arms": totals,
        "combined": combined,
    }


def _finite_interval_bound(interval: dict[str, Any], side: str) -> float:
    value = interval.get(side)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise InconclusiveBootstrap(f"registered {side} is not finite")
    return float(value)


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
    if artifact.get("schema_version") != 2 or artifact.get("arm") != arm:
        return f"invalid {arm} artifact identity"
    hash_input = artifact.get("artifact_hash_input")
    artifact_hash = artifact.get("artifact_hash")
    manifest = artifact.get("manifest")
    if not isinstance(hash_input, dict) or not isinstance(artifact_hash, str) or not isinstance(manifest, dict):
        return f"{arm} artifact is missing its integrity envelope"
    try:
        validate_model_input_evidence(artifact.get("runs", {}))
        validate_model_input_evidence(artifact.get("smoke_gate", {}))
    except ValueError:
        return f"{arm} model input evidence integrity digest does not match"
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


def _instruction_text_for_outcome(
    artifact: dict[str, Any],
    outcome: TrialOutcome,
    tasks_by_id: Mapping[str, TaskManifest],
) -> str | None:
    task = tasks_by_id.get(outcome.task_id)
    catalogs = artifact.get("catalogs")
    if task is None or not isinstance(catalogs, dict):
        return None
    snapshot = catalogs.get(f"{outcome.transport}:{task.fixture.credential_profile}")
    discovery = snapshot.get("server_discover") if isinstance(snapshot, dict) else None
    instructions = discovery.get("instructions") if isinstance(discovery, dict) else None
    return instructions if isinstance(instructions, str) else None


def _artifact_has_instruction_input_proof(
    artifact: dict[str, Any],
    tasks: list[TaskManifest],
) -> bool:
    tasks_by_id = {task.id: task for task in tasks}
    outcomes = _all_run_outcomes(artifact.get("runs", {}))
    if not outcomes or any(
        not has_server_instructions_evidence(
            outcome,
            _instruction_text_for_outcome(artifact, outcome, tasks_by_id),
        )
        for outcome in outcomes
    ):
        return False
    smoke_gate = artifact.get("smoke_gate")
    cells = smoke_gate.get("cells") if isinstance(smoke_gate, dict) else None
    if not isinstance(cells, list) or not cells:
        return False
    for cell in cells:
        raw_outcome = cell.get("outcome") if isinstance(cell, dict) else None
        if not isinstance(raw_outcome, dict):
            return False
        try:
            outcome = TrialOutcome.model_validate(raw_outcome)
        except ValueError:
            return False
        if not has_server_instructions_evidence(
            outcome,
            _instruction_text_for_outcome(artifact, outcome, tasks_by_id),
        ):
            return False
    return True


def compare_artifacts(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Compare a sealed paired run using only the preregistered BCa gates."""

    for artifact, arm in ((baseline, cast(ArmName, "baseline")), (candidate, cast(ArmName, "candidate"))):
        integrity_error = _artifact_integrity_error(artifact, arm)
        if integrity_error is not None:
            return _empty_comparison(baseline, candidate, integrity_error)
    if "selection" in baseline or "selection" in candidate:
        return _empty_comparison(
            baseline,
            candidate,
            "selected trial evidence cannot support the full benchmark adoption verdict",
        )
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

    try:
        raw_task_contracts = baseline.get("task_contracts")
        if not isinstance(raw_task_contracts, list):
            raise ValueError("full task corpus is missing")
        tasks = [TaskManifest.model_validate(item) for item in raw_task_contracts]
        descriptors = {
            "baseline": _validate_preregistered_inputs(
                baseline,
                manifest=validated_manifest,
                tasks=tasks,
                arm="baseline",
            ),
            "candidate": _validate_preregistered_inputs(
                candidate,
                manifest=validated_manifest,
                tasks=tasks,
                arm="candidate",
            ),
        }
        expected_seal_hash = hash_json(
            {
                arm: hash_json(artifact["pre_smoke_seal_inputs"])
                for arm, artifact in (("baseline", baseline), ("candidate", candidate))
            }
        )
        if (
            not baseline.get("pre_smoke_seal_hash")
            or baseline.get("pre_smoke_seal_hash") != expected_seal_hash
            or candidate.get("pre_smoke_seal_hash") != expected_seal_hash
        ):
            raise ValueError("paired pre-smoke seal digest does not match the complete sealed inputs")
        _validate_shared_budget(
            baseline,
            candidate,
            manifest=validated_manifest,
            tasks=tasks,
            descriptors=descriptors,
        )
    except Exception as exc:
        return _empty_comparison(baseline, candidate, f"sealed evidence is incomplete or invalid: {exc}")

    registries = [baseline.get("provider_registry"), candidate.get("provider_registry")]
    base_registry = cast(dict[str, Any], registries[0])
    candidate_registry = cast(dict[str, Any], registries[1])
    if base_registry.get("snapshot_hash") != candidate_registry.get("snapshot_hash"):
        return _empty_comparison(baseline, candidate, "provider registry drifted between paired arms")

    try:
        baseline_map = _outcome_map(baseline)
        candidate_map = _outcome_map(candidate)
    except Exception as exc:
        return _empty_comparison(baseline, candidate, f"trial evidence is invalid: {exc}")
    if set(baseline_map) != set(candidate_map):
        return _empty_comparison(baseline, candidate, "paired trial identities differ")
    if not _artifact_has_instruction_input_proof(baseline, tasks) or not _artifact_has_instruction_input_proof(
        candidate, tasks
    ):
        return _empty_comparison(
            baseline,
            candidate,
            "model-input evidence does not prove the sealed server instructions reached every request",
        )
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
        success_lower_bounds = {
            "overall": _finite_interval_bound(overall_success, "lower_bound"),
            **{
                key: _finite_interval_bound(value, "lower_bound")
                for key, value in cell_success.items()
            },
        }
        success_failure_upper_bounds = {
            "overall": _finite_interval_bound(overall_success_clear_failure, "upper_bound"),
            **{
                key: _finite_interval_bound(value, "upper_bound")
                for key, value in cell_success_clear_failure.items()
            },
        }
        error_upper_bound = _finite_interval_bound(error_rate, "upper_bound")
        error_failure_lower_bound = _finite_interval_bound(error_rate_clear_failure, "lower_bound")
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
    success_clear_failure_intervals = {
        "overall": overall_success_clear_failure,
        **cell_success_clear_failure,
    }
    success_pass = all(bound >= -margin for bound in success_lower_bounds.values())
    error_pass = error_upper_bound < 0
    success_clear_fail = any(
        bound < -margin for bound in success_failure_upper_bounds.values()
    )
    error_clear_fail = error_failure_lower_bound > 0
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
        "schema_version": 4,
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
    checkpoint = artifact.get("checkpoint")
    checkpoint_receipt = (
        {
            key: checkpoint.get(key)
            for key in (
                "enabled",
                "lifecycle",
                "record_count",
                "valid_completed_trials",
                "reserved_cost_usd",
                "smoke_status",
                "smoke_cell_count",
            )
        }
        if isinstance(checkpoint, dict)
        else None
    )
    hash_input = {
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
        "checkpoint_receipt": checkpoint_receipt,
        "provider_registry": artifact.get("provider_registry"),
        "pre_smoke_seal_inputs": artifact.get("pre_smoke_seal_inputs"),
        "pre_smoke_seal_hash": artifact.get("pre_smoke_seal_hash"),
        "execution_environment": artifact.get("execution_environment"),
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
    if "continuation_lineage" in artifact:
        hash_input["continuation_lineage"] = artifact["continuation_lineage"]
    if "continuation_runtime_provenance" in artifact:
        hash_input["continuation_runtime_provenance"] = artifact["continuation_runtime_provenance"]
    if "selection" in artifact:
        hash_input["selection"] = artifact["selection"]
    if "resume_source" in artifact:
        hash_input["resume_source"] = artifact["resume_source"]
    return hash_input


def load_paired_resume_source(
    *,
    artifact_paths: Mapping[ArmName, Path],
    checkpoint_paths: Mapping[ArmName, Path],
    manifest: BenchmarkRunManifest,
    tasks: list[TaskManifest],
) -> dict[str, Any]:
    """Read and validate the paired artifacts/checkpoints before a resume can write."""

    manifest_data = manifest.model_dump(mode="json")
    manifest_digest = hash_json(manifest_data)
    task_data = [task.model_dump(mode="json") for task in tasks]
    corpus_digest = hash_json(task_data)
    expected_keys: dict[ArmName, dict[tuple[str, str, int, str], CheckpointKey]] = {
        "baseline": {},
        "candidate": {},
    }
    trial_order: dict[ArmName, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
    expected_schedule: dict[str, list[tuple[str, int, ArmName]]] = {}
    for model in manifest.models:
        for transport in manifest.transports:
            cell = f"{model.class_name}:{transport}"
            schedule: list[tuple[str, int, ArmName]] = []
            for repeat_index in range(1, manifest.repeats + 1):
                for task in tasks:
                    if transport not in task.fixture.transports:
                        continue
                    for arm in planned_arm_order(task.id, repeat_index, manifest.paired_order_seed):
                        schedule.append((task.id, repeat_index, arm))
                        source_revision = manifest.arm_source_revisions[arm]
                        key = CheckpointKey(
                            source_revision=source_revision,
                            run_manifest_hash=manifest_digest,
                            task_corpus_hash=corpus_digest,
                            arm=arm,
                            model_class=model.class_name,
                            model_id=model.model_id,
                            transport=transport,
                            task_id=task.id,
                            repeat_index=repeat_index,
                            locale=task.locale,
                        )
                        identity = (cell, task.id, repeat_index, arm)
                        expected_keys[arm][identity] = key
            expected_schedule[cell] = schedule
    for model in manifest.models:
        for transport in manifest.transports:
            cell = f"{model.class_name}:{transport}"
            for task in tasks:
                if transport not in task.fixture.transports:
                    continue
                for repeat_index in range(1, manifest.repeats + 1):
                    for arm in (cast(ArmName, "baseline"), cast(ArmName, "candidate")):
                        trial_order[arm].append(
                            expected_keys[arm][(cell, task.id, repeat_index, arm)].model_dump(mode="json")
                        )

    expected_order_plan = [
        {
            "task_id": task.id,
            "repeat_index": repeat_index,
            "arm_order": list(planned_arm_order(task.id, repeat_index, manifest.paired_order_seed)),
        }
        for repeat_index in range(1, manifest.repeats + 1)
        for task in tasks
    ]
    source_artifacts: dict[ArmName, dict[str, Any]] = {}
    source_checkpoints: dict[ArmName, CheckpointDocument] = {}
    source_hashes: dict[ArmName, dict[str, Any]] = {}
    outcome_costs: dict[ArmName, Decimal] = {"baseline": Decimal("0"), "candidate": Decimal("0")}
    execution: dict[str, Any] | None = None
    shared_budget: dict[str, Any] | None = None

    for arm in (cast(ArmName, "baseline"), cast(ArmName, "candidate")):
        try:
            artifact_bytes = artifact_paths[arm].read_bytes()
            checkpoint_bytes = checkpoint_paths[arm].read_bytes()
            artifact = json.loads(artifact_bytes)
            checkpoint_raw = json.loads(checkpoint_bytes)
        except OSError as exc:
            raise ValueError(f"cannot read {arm} paired resume source: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"{arm} paired resume source is invalid JSON") from exc
        if not isinstance(artifact, dict) or not isinstance(checkpoint_raw, dict):
            raise ValueError(f"{arm} paired resume source must be JSON objects")
        try:
            checkpoint = CheckpointDocument.model_validate(checkpoint_raw)
        except Exception as exc:
            raise ValueError(f"{arm} paired resume checkpoint contract validation failed: {exc}") from exc

        if artifact.get("schema_version") != 2 or artifact.get("arm") != arm:
            raise ValueError(f"{arm} paired resume artifact identity is invalid")
        if artifact.get("run_manifest_hash") != manifest_digest or artifact.get("manifest") != manifest_data:
            raise ValueError(f"{arm} paired resume artifact does not match the current run manifest")
        if artifact.get("task_corpus_hash") != corpus_digest or artifact.get("task_contracts") != task_data:
            raise ValueError(f"{arm} paired resume artifact does not match the current task corpus")
        if artifact.get("task_ids") != [task.id for task in tasks]:
            raise ValueError(f"{arm} paired resume artifact task order changed")
        if artifact.get("source_revision") != manifest.arm_source_revisions[arm]:
            raise ValueError(f"{arm} paired resume artifact source revision changed")
        if artifact.get("paired_order_plan") != expected_order_plan:
            raise ValueError(f"{arm} paired resume artifact paired order changed")
        hash_input = artifact.get("artifact_hash_input")
        if not isinstance(hash_input, dict) or hash_input.get("trial_order") != trial_order[arm]:
            raise ValueError(f"{arm} paired resume artifact trial order changed")
        try:
            recalculated_hash_input = _build_artifact_hash_input(artifact, trial_order=trial_order[arm])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{arm} paired resume artifact hash input is invalid") from exc
        if recalculated_hash_input != hash_input or artifact.get("artifact_hash") != hash_json(hash_input):
            raise ValueError(f"{arm} paired resume artifact hash does not match its contents")

        if (
            checkpoint.lifecycle != "finalized"
            or checkpoint.reserved_cost_usd != 0
            or checkpoint.smoke_status != "passed"
            or checkpoint.continuation is not None
            or checkpoint.selection_preflight is not None
        ):
            raise ValueError(f"{arm} paired resume checkpoint is not a finalized full-run source")
        header = checkpoint.header
        provider_registry = artifact.get("provider_registry")
        if not isinstance(provider_registry, dict):
            raise ValueError(f"{arm} paired resume artifact provider registry is invalid")
        if (
            header.arm != arm
            or header.source_revision != manifest.arm_source_revisions[arm]
            or header.run_manifest_hash != manifest_digest
            or header.task_corpus_hash != corpus_digest
            or header.pre_smoke_seal_hash != artifact.get("pre_smoke_seal_hash")
            or header.provider_registry_hash != provider_registry.get("snapshot_hash")
            or checkpoint.spent_hash != spent_hash(checkpoint.spent)
            or checkpoint.timing_hash != timing_hash(checkpoint.timing)
        ):
            raise ValueError(f"{arm} paired resume checkpoint header or accounting digest changed")
        if checkpoint.provider_registry_snapshot is None or (
            provider_registry_snapshot_hash(checkpoint.provider_registry_snapshot) != header.provider_registry_hash
        ):
            raise ValueError(f"{arm} paired resume checkpoint provider registry seal is invalid")
        if len(checkpoint.smoke_gate) != 4 or set(checkpoint.smoke_gate) != {
            f"{model.class_name}:{transport}"
            for model in manifest.models
            for transport in manifest.transports
        }:
            raise ValueError(f"{arm} paired resume checkpoint smoke gate is incomplete")

        receipt = artifact.get("checkpoint")
        if not isinstance(receipt, dict):
            raise ValueError(f"{arm} paired resume artifact is missing its checkpoint receipt")
        for name, value in (
            ("lifecycle", checkpoint.lifecycle),
            ("record_count", len(checkpoint.records)),
            ("reserved_cost_usd", checkpoint.reserved_cost_usd),
            ("smoke_status", checkpoint.smoke_status),
            ("smoke_cell_count", len(checkpoint.smoke_gate)),
            ("provider_registry_hash", header.provider_registry_hash),
            ("spent", checkpoint.spent.model_dump(mode="json")),
            ("spent_hash", checkpoint.spent_hash),
            ("timing", checkpoint.timing.model_dump(mode="json")),
            ("timing_hash", checkpoint.timing_hash),
        ):
            if receipt.get(name) != value:
                raise ValueError(f"{arm} paired resume checkpoint receipt does not match {name}")

        raw_trials: dict[tuple[str, str, int, ArmName], dict[str, Any]] = {}
        raw_statuses: dict[tuple[str, str, int, ArmName], str] = {}
        runs = artifact.get("runs")
        if not isinstance(runs, dict):
            raise ValueError(f"{arm} paired resume artifact runs are invalid")
        for cell, report in runs.items():
            if cell not in expected_schedule or not isinstance(report, dict):
                raise ValueError(f"{arm} paired resume artifact contains an unknown run cell")
            model_class, transport = cell.split(":", 1)
            trials = report.get("trials")
            statuses = report.get("statuses")
            if not isinstance(trials, list) or not isinstance(statuses, list):
                raise ValueError(f"{arm} paired resume artifact rows are malformed for {cell}")
            for raw_outcome in trials:
                if not isinstance(raw_outcome, dict):
                    raise ValueError(f"{arm} paired resume trial row is malformed")
                try:
                    outcome = TrialOutcome.model_validate(raw_outcome)
                except Exception as exc:
                    raise ValueError(f"{arm} paired resume trial outcome contract validation failed: {exc}") from exc
                identity = (cell, outcome.task_id, outcome.repeat_index, arm)
                expected_key = expected_keys[arm].get(identity)
                if (
                    expected_key is None
                    or outcome.arm != arm
                    or outcome.model_class != model_class
                    or outcome.model_id != expected_key.model_id
                    or outcome.transport != transport
                    or outcome.locale != expected_key.locale
                    or identity in raw_trials
                ):
                    raise ValueError(f"{arm} paired resume trial identity is invalid or duplicated")
                raw_trials[identity] = raw_outcome
                if type(outcome.paired_execution_sequence) is not int:
                    raise ValueError(f"{arm} paired resume trial is missing its execution sequence")
                outcome_costs[arm] += max(
                    Decimal(str(outcome.cost_usd)),
                    Decimal(str(outcome.provider_cost_usd or 0.0)),
                )
            for raw_status in statuses:
                if not isinstance(raw_status, dict):
                    raise ValueError(f"{arm} paired resume status row is malformed")
                status_task_id = raw_status.get("task_id")
                status_repeat_index = raw_status.get("repeat_index")
                status = raw_status.get("status")
                if (
                    not isinstance(status_task_id, str)
                    or type(status_repeat_index) is not int
                    or status not in {"completed", "failed"}
                ):
                    raise ValueError(f"{arm} paired resume status identity is invalid or nonterminal")
                identity = (cell, status_task_id, status_repeat_index, arm)
                if identity not in expected_keys[arm] or identity in raw_statuses:
                    raise ValueError(f"{arm} paired resume status identity is invalid or nonterminal")
                raw_statuses[identity] = cast(str, status)

        if set(raw_trials) != set(raw_statuses) or set(raw_trials) != {
            (f"{record.key.model_class}:{record.key.transport}", record.key.task_id, record.key.repeat_index, arm)
            for record in checkpoint.records.values()
        }:
            raise ValueError(f"{arm} paired resume artifact rows do not match checkpoint identities")
        for digest, record in checkpoint.records.items():
            identity = (
                f"{record.key.model_class}:{record.key.transport}",
                record.key.task_id,
                record.key.repeat_index,
                arm,
            )
            if (
                digest != checkpoint_key_digest(record.key)
                or record.status not in {"completed", "failed"}
                or record.record_hash != checkpoint_record_hash(record.status, record.key, record.outcome)
                or identity not in raw_trials
                or record.status != raw_statuses[identity]
                or record.outcome.model_dump(mode="json") != raw_trials[identity]
            ):
                raise ValueError(f"{arm} paired resume checkpoint trial row changed")
            key_expected = expected_keys[arm].get(identity)
            if key_expected is None or record.key != key_expected:
                raise ValueError(f"{arm} paired resume checkpoint trial key changed")

        smoke_cells = artifact.get("smoke_gate", {}).get("cells") if isinstance(artifact.get("smoke_gate"), dict) else None
        if not isinstance(smoke_cells, list) or len(smoke_cells) != 4:
            raise ValueError(f"{arm} paired resume artifact smoke gate is incomplete")
        artifact_smoke = {cell.get("cell"): cell for cell in smoke_cells if isinstance(cell, dict)}
        if len(artifact_smoke) != 4 or set(artifact_smoke) != set(checkpoint.smoke_gate):
            raise ValueError(f"{arm} paired resume artifact smoke identities changed")
        for cell_key, smoke_record in checkpoint.smoke_gate.items():
            raw_cell = artifact_smoke[cell_key]
            if (
                smoke_record.status != "completed"
                or raw_cell.get("status") != smoke_record.status
                or raw_cell.get("outcome") != smoke_record.outcome.model_dump(mode="json")
                or smoke_record.record_hash
                != smoke_record_hash(
                    smoke_record.status,
                    smoke_record.model_class,
                    smoke_record.model_id,
                    smoke_record.transport,
                    smoke_record.outcome,
                )
            ):
                raise ValueError(f"{arm} paired resume checkpoint smoke row changed")
            outcome_costs[arm] += max(
                Decimal(str(smoke_record.outcome.cost_usd)),
                Decimal(str(smoke_record.outcome.provider_cost_usd or 0.0)),
            )

        paired = artifact.get("paired_execution")
        budget = artifact.get("paired_budget_used")
        if not isinstance(paired, dict) or not isinstance(budget, dict):
            raise ValueError(f"{arm} paired resume artifact is missing shared execution accounting")
        if execution is None:
            execution = paired
            shared_budget = budget
        elif paired != execution or budget != shared_budget:
            raise ValueError("paired resume artifacts disagree on shared execution or budget evidence")
        source_artifacts[arm] = artifact
        source_checkpoints[arm] = checkpoint
        source_hashes[arm] = {
            "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "artifact_hash": artifact.get("artifact_hash"),
            "checkpoint_sha256": hashlib.sha256(checkpoint_bytes).hexdigest(),
            "checkpoint_spent_hash": checkpoint.spent_hash,
            "source_revision": artifact.get("source_revision"),
            "protocol_revision": artifact.get("protocol_revision"),
            "parent_evaluator_commit": (
                artifact.get("evaluator_commit") if isinstance(artifact.get("evaluator_commit"), str) else None
            ),
            "parent_evaluator_commit_recorded": isinstance(artifact.get("evaluator_commit"), str),
        }

    assert execution is not None and shared_budget is not None
    if execution.get("mode") != manifest.paired_order or execution.get("seed") != manifest.paired_order_seed:
        raise ValueError("paired resume source changed its preregistered order")
    events = execution.get("events")
    if not isinstance(events, list):
        raise ValueError("paired resume source events are invalid")
    sequences = [event.get("sequence") for event in events if isinstance(event, dict)]
    if len(sequences) != len(events) or sequences != list(range(1, len(events) + 1)):
        raise ValueError("paired resume source event sequence is not contiguous")
    events_by_identity: dict[tuple[str, str, int, ArmName], list[dict[str, Any]]] = defaultdict(list)
    events_by_sequence: dict[int, dict[str, Any]] = {}
    first_attempts_by_cell: dict[str, list[tuple[str, int, ArmName]]] = defaultdict(list)
    for event in events:
        cell = event.get("cell")
        task_id = event.get("task_id")
        repeat_index = event.get("repeat_index")
        arm = event.get("arm")
        identity = (cell, task_id, repeat_index, arm)
        if (
            not isinstance(cell, str)
            or not isinstance(task_id, str)
            or arm not in {"baseline", "candidate"}
            or type(repeat_index) is not int
            or type(event.get("sequence")) is not int
            or cell not in expected_schedule
            or identity not in expected_keys[cast(ArmName, arm)]
            or type(event.get("order_position")) is not int
            or event.get("order_position")
            != planned_arm_order(task_id, repeat_index, manifest.paired_order_seed).index(arm)
        ):
            raise ValueError("paired resume source contains an invalid execution identity")
        chain = events_by_identity[cast(tuple[str, str, int, ArmName], identity)]
        attempt = event.get("attempt", 1)
        if type(attempt) is not int or attempt != len(chain) + 1:
            raise ValueError("paired resume source retry attempt index is invalid")
        if attempt == 1:
            if "retry_of_sequence" in event:
                raise ValueError("first paired execution attempt cannot reference a retry")
            first_attempts_by_cell[cell].append((task_id, repeat_index, cast(ArmName, arm)))
        elif event.get("retry_of_sequence") != chain[-1].get("sequence"):
            raise ValueError("paired resume source retry does not link to its previous start")
        chain.append(event)
        events_by_sequence[event["sequence"]] = event
    for cell, observed in first_attempts_by_cell.items():
        if observed != expected_schedule[cell][: len(observed)]:
            raise ValueError(f"paired resume source did not preserve the scheduled prefix for {cell}")

    terminal_sequences: set[int] = set()
    for arm, artifact in source_artifacts.items():
        for cell, report in artifact["runs"].items():
            for raw in report["trials"]:
                outcome = TrialOutcome.model_validate(raw)
                identity = (cell, outcome.task_id, outcome.repeat_index, arm)
                chain = events_by_identity.get(identity, [])
                sequence = outcome.paired_execution_sequence
                if (
                    not chain
                    or type(sequence) is not int
                    or sequence != chain[-1].get("sequence")
                    or events_by_sequence.get(sequence) is None
                ):
                    raise ValueError("paired resume source trial does not map to its latest execution start")
                terminal_sequences.add(sequence)
    unfinished_starts = [dict(event) for event in events if event["sequence"] not in terminal_sequences]
    declared_unfinished = execution.get("unfinished_starts")
    if declared_unfinished is not None and declared_unfinished != unfinished_starts:
        raise ValueError("paired resume source unfinished-start evidence does not match its trial rows")
    expected_total = sum(len(schedule) for schedule in expected_schedule.values())
    terminal_count = sum(len(checkpoint.records) for checkpoint in source_checkpoints.values())
    if execution.get("complete") is not (terminal_count == expected_total):
        raise ValueError("paired resume source completion flag does not match checkpoint rows")

    paired_model_requests = shared_budget.get("model_requests")
    paired_setup_requests = shared_budget.get("provider_setup_requests")
    if (
        type(paired_model_requests) is not int
        or type(paired_setup_requests) is not int
        or paired_model_requests < 0
        or paired_setup_requests < 0
        or shared_budget.get("max_total_cost_usd") != manifest.budget.max_total_cost_usd
    ):
        raise ValueError("paired resume source ledger does not match the registered budget")
    joint_cost_value = shared_budget.get("cost_usd")
    joint_model_work_value = shared_budget.get("model_work_seconds")
    joint_wall_value = shared_budget.get("wall_seconds")
    if type(joint_cost_value) not in {int, float, str} or type(joint_model_work_value) not in {int, float} or type(
        joint_wall_value
    ) not in {int, float}:
        raise ValueError("paired resume source ledger totals are invalid")
    try:
        joint_cost = Decimal(str(joint_cost_value))
        joint_model_work = float(cast(int | float, joint_model_work_value))
        joint_wall = float(cast(int | float, joint_wall_value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("paired resume source ledger totals are invalid") from exc
    joint_inputs = shared_budget.get("input_tokens")
    joint_outputs = shared_budget.get("output_tokens")
    joint_total_tokens = shared_budget.get("total_tokens")
    if not joint_cost.is_finite() or joint_cost < 0:
        raise ValueError("paired resume source cost is invalid")
    if (
        type(joint_inputs) is not int
        or type(joint_outputs) is not int
        or type(joint_total_tokens) is not int
        or min(joint_inputs, joint_outputs, joint_total_tokens) < 0
        or joint_total_tokens != joint_inputs + joint_outputs
        or not math.isfinite(joint_model_work)
        or joint_model_work < 0
        or not math.isfinite(joint_wall)
        or joint_wall < 0
    ):
        raise ValueError("paired resume source ledger totals are invalid")
    joint_inputs = cast(int, joint_inputs)
    joint_outputs = cast(int, joint_outputs)
    joint_total_tokens = cast(int, joint_total_tokens)
    if any(
        checkpoint.spent.provider_setup_requests > checkpoint.spent.model_requests
        for checkpoint in source_checkpoints.values()
    ):
        raise ValueError("paired resume checkpoint setup requests exceed total requests")
    checkpoint_model_requests = sum(
        checkpoint.spent.model_requests - checkpoint.spent.provider_setup_requests
        for checkpoint in source_checkpoints.values()
    )
    checkpoint_setup_requests = sum(
        checkpoint.spent.provider_setup_requests for checkpoint in source_checkpoints.values()
    )
    checkpoint_inputs = sum(checkpoint.spent.input_tokens for checkpoint in source_checkpoints.values())
    checkpoint_outputs = sum(checkpoint.spent.output_tokens for checkpoint in source_checkpoints.values())
    checkpoint_cost = sum(
        (Decimal(str(checkpoint.spent.cost_usd)) for checkpoint in source_checkpoints.values()),
        Decimal("0"),
    )
    checkpoint_work = sum(checkpoint.spent.model_work_seconds for checkpoint in source_checkpoints.values())
    residual_model_requests = paired_model_requests - checkpoint_model_requests
    residual_setup_requests = paired_setup_requests - checkpoint_setup_requests
    residual_inputs = joint_inputs - checkpoint_inputs
    residual_outputs = joint_outputs - checkpoint_outputs
    residual_cost = joint_cost - checkpoint_cost
    residual_conservative_cost = joint_cost - sum(outcome_costs.values(), Decimal("0"))
    residual_model_work = joint_model_work - checkpoint_work
    residual_wall = joint_wall - max(checkpoint.spent.wall_seconds for checkpoint in source_checkpoints.values())
    if (
        min(residual_model_requests, residual_setup_requests, residual_inputs, residual_outputs) < 0
        or residual_cost < 0
        or residual_conservative_cost < 0
        or not math.isfinite(residual_model_work)
        or residual_model_work < -1e-9
        or not math.isfinite(residual_wall)
        or residual_wall < -1e-9
    ):
        raise ValueError("paired resume source ledger is below its checkpoint accounting")
    residual: dict[str, int | float | str] = {
        "model_requests": residual_model_requests,
        "provider_setup_requests": residual_setup_requests,
        "input_tokens": residual_inputs,
        "output_tokens": residual_outputs,
        "cost_usd": str(residual_cost),
        "conservative_cost_usd": str(residual_conservative_cost),
        "model_work_seconds": residual_model_work,
        "wall_seconds_above_max_checkpoint": residual_wall,
    }
    if joint_total_tokens != joint_inputs + joint_outputs:
        raise ValueError("paired resume source token total is inconsistent")

    prior_lineage = source_artifacts["baseline"].get("resume_source")
    if prior_lineage != source_artifacts["candidate"].get("resume_source"):
        raise ValueError("paired resume source ancestry differs between artifacts")
    writer_hashes = {}
    for filename in ("runner.py", "cli.py", "execution.py"):
        source_path = Path(__file__).with_name(filename)
        writer_hashes[filename] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    metadata = {
        "schema_version": 1,
        "parents": source_hashes,
        "input_contract": {
            "manifest_source": "parent_artifacts",
            "run_manifest_hash": manifest_digest,
            "task_corpus_hash": corpus_digest,
        },
        "parent_resume_source_sha256": hash_json(prior_lineage) if prior_lineage is not None else None,
        "source_execution": {
            "event_count": len(events),
            "terminal_outcome_count": terminal_count,
            "unfinished_starts": unfinished_starts,
        },
        "source_accounting": {
            "joint_budget_used": shared_budget,
            "checkpoint_model_requests": checkpoint_model_requests,
            "checkpoint_provider_setup_requests": checkpoint_setup_requests,
            "checkpoint_input_tokens": checkpoint_inputs,
            "checkpoint_output_tokens": checkpoint_outputs,
            "checkpoint_cost_usd": str(checkpoint_cost),
            "checkpoint_model_work_seconds": checkpoint_work,
            "raw_mixed_field_request_difference_audit_only": (
                paired_model_requests
                - sum(checkpoint.spent.model_requests for checkpoint in source_checkpoints.values())
            ),
            "model_request_equation": (
                f"{paired_model_requests} = {checkpoint_model_requests} + {residual['model_requests']}"
            ),
            "residual": residual,
        },
        "writer": {
            "semantic_evaluator_version": TrialEvaluator().get_evaluator_version(),
            "report_evaluator_version": MetricsReportEvaluator().get_evaluator_version(),
            "source_sha256": writer_hashes,
        },
    }
    return {
        "events": [dict(event) for event in events],
        "budget_used": dict(shared_budget),
        "metadata": metadata,
    }


def _paired_artifact_receipts_reconcile(
    artifacts: dict[str, dict[str, Any]],
    paired_budget_used: dict[str, Any],
) -> bool:
    if set(artifacts) != {"baseline", "candidate"}:
        return False
    if artifacts["baseline"].get("resume_source") != artifacts["candidate"].get("resume_source"):
        return False

    residual_model_requests = 0
    residual_setup_requests = 0
    residual_input_tokens = 0
    residual_output_tokens = 0
    residual_conservative_cost = Decimal("0")
    residual_model_work = 0.0
    resume_source = artifacts["baseline"].get("resume_source")
    if resume_source is not None:
        source_accounting = resume_source.get("source_accounting") if isinstance(resume_source, dict) else None
        source_residual = source_accounting.get("residual") if isinstance(source_accounting, dict) else None
        if not isinstance(source_residual, dict):
            return False
        for key in ("model_requests", "provider_setup_requests", "input_tokens", "output_tokens"):
            value = source_residual.get(key)
            if type(value) is not int or value < 0:
                return False
            if key == "model_requests":
                residual_model_requests = value
            elif key == "provider_setup_requests":
                residual_setup_requests = value
            elif key == "input_tokens":
                residual_input_tokens = value
            else:
                residual_output_tokens = value
        try:
            residual_conservative_cost = Decimal(str(source_residual["conservative_cost_usd"]))
            residual_model_work = float(source_residual["model_work_seconds"])
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return False
        if (
            not residual_conservative_cost.is_finite()
            or residual_conservative_cost < 0
            or not math.isfinite(residual_model_work)
            or residual_model_work < -1e-9
        ):
            return False

    totals = {
        "model_requests": 0,
        "provider_setup_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    total_cost = Decimal("0")
    total_work = 0.0
    max_checkpoint_wall = 0.0

    for arm in ("baseline", "candidate"):
        artifact = artifacts[arm]
        checkpoint = artifact.get("checkpoint")
        smoke_gate = artifact.get("smoke_gate")
        runs = artifact.get("runs")
        hash_input = artifact.get("artifact_hash_input")
        if (
            artifact.get("arm") != arm
            or not isinstance(checkpoint, dict)
            or checkpoint.get("enabled") is not True
            or checkpoint.get("lifecycle") != "finalized"
            or checkpoint.get("smoke_status") != "passed"
            or checkpoint.get("smoke_cell_count") != 4
            or not isinstance(smoke_gate, dict)
            or smoke_gate.get("status") != "passed"
            or not isinstance(runs, dict)
            or not isinstance(hash_input, dict)
        ):
            return False

        expected_checkpoint_receipt = {
            key: checkpoint.get(key)
            for key in (
                "enabled",
                "lifecycle",
                "record_count",
                "valid_completed_trials",
                "reserved_cost_usd",
                "smoke_status",
                "smoke_cell_count",
            )
        }
        if hash_input.get("checkpoint_receipt") != expected_checkpoint_receipt:
            return False

        expected_cells = ["primary:http", "primary:stdio", "lightweight:http", "lightweight:stdio"]
        if smoke_gate.get("required_cells") != expected_cells:
            return False
        smoke_cells = smoke_gate.get("cells")
        if not isinstance(smoke_cells, list) or [
            item.get("cell") for item in smoke_cells if isinstance(item, dict)
        ] != expected_cells:
            return False

        outcomes: list[TrialOutcome] = []
        for cell in smoke_cells:
            if not isinstance(cell, dict) or cell.get("status") != "completed":
                return False
            raw_outcome = cell.get("outcome")
            if not isinstance(raw_outcome, dict):
                return False
            try:
                outcome = TrialOutcome.model_validate(raw_outcome)
            except ValueError:
                return False
            model_class, transport = cell["cell"].split(":", 1)
            if outcome.arm != arm or outcome.model_class != model_class or outcome.transport != transport:
                return False
            outcomes.append(outcome)

        trial_order = hash_input.get("trial_order")
        if not isinstance(trial_order, list):
            return False
        expected: set[tuple[str, str, str, int]] = set()
        for item in trial_order:
            if not isinstance(item, dict):
                return False
            model_class = item.get("model_class")
            transport = item.get("transport")
            task_id = item.get("task_id")
            repeat_index = item.get("repeat_index")
            if not (
                isinstance(model_class, str)
                and isinstance(transport, str)
                and isinstance(task_id, str)
                and type(repeat_index) is int
            ):
                return False
            expected.add((model_class, transport, task_id, repeat_index))
        if len(expected) != len(trial_order):
            return False

        trial_rows: dict[tuple[str, str, str, int], TrialOutcome] = {}
        status_rows: dict[tuple[str, str, str, int], str] = {}
        for cell, report in runs.items():
            if not isinstance(cell, str) or ":" not in cell or not isinstance(report, dict):
                return False
            model_class, transport = cell.split(":", 1)
            raw_trials = report.get("trials")
            raw_statuses = report.get("statuses")
            if not isinstance(raw_trials, list) or not isinstance(raw_statuses, list):
                return False
            for raw_outcome in raw_trials:
                if not isinstance(raw_outcome, dict):
                    return False
                try:
                    outcome = TrialOutcome.model_validate(raw_outcome)
                except ValueError:
                    return False
                identity = (model_class, transport, outcome.task_id, outcome.repeat_index)
                if (
                    outcome.arm != arm
                    or outcome.model_class != model_class
                    or outcome.transport != transport
                    or identity in trial_rows
                ):
                    return False
                trial_rows[identity] = outcome
                outcomes.append(outcome)
            for raw_status in raw_statuses:
                if not isinstance(raw_status, dict):
                    return False
                task_id = raw_status.get("task_id")
                repeat_index = raw_status.get("repeat_index")
                status = raw_status.get("status")
                if (
                    not isinstance(task_id, str)
                    or type(repeat_index) is not int
                    or status not in {"completed", "failed"}
                ):
                    return False
                identity = (model_class, transport, task_id, repeat_index)
                if identity in status_rows:
                    return False
                status_rows[identity] = status
        if set(trial_rows) != expected or set(status_rows) != expected:
            return False
        if any(status not in {"completed", "failed"} for status in status_rows.values()):
            return False
        completed_statuses = sum(status == "completed" for status in status_rows.values())
        if (
            artifact.get("completed_trials") != len(expected)
            or checkpoint.get("record_count") != len(expected)
            or checkpoint.get("valid_completed_trials") != completed_statuses
        ):
            return False

        spent = checkpoint.get("spent")
        arm_budget = artifact.get("budget_used")
        if not isinstance(spent, dict) or not isinstance(arm_budget, dict):
            return False
        if checkpoint.get("spent_hash") != hash_json(spent):
            return False
        try:
            checkpoint_reserved = Decimal(str(checkpoint["reserved_cost_usd"]))
            arm_reserved = Decimal(str(arm_budget["reserved_cost_usd"]))
        except (InvalidOperation, KeyError, TypeError):
            return False
        if (
            not checkpoint_reserved.is_finite()
            or not arm_reserved.is_finite()
            or checkpoint_reserved != 0
            or arm_reserved != 0
        ):
            return False

        arm_outcomes = [outcome for outcome in outcomes]
        arm_requests = sum(outcome.model_requests for outcome in arm_outcomes)
        arm_input_tokens = sum(outcome.input_tokens for outcome in arm_outcomes)
        arm_output_tokens = sum(outcome.output_tokens for outcome in arm_outcomes)
        arm_raw_cost = sum((Decimal(str(outcome.cost_usd)) for outcome in arm_outcomes), Decimal("0"))
        arm_conservative_cost = sum(
            (
                max(
                    Decimal(str(outcome.cost_usd)),
                    Decimal(str(outcome.provider_cost_usd or 0.0)),
                )
                for outcome in arm_outcomes
            ),
            Decimal("0"),
        )
        arm_work = sum(outcome.latency_seconds for outcome in arm_outcomes)
        setup_requests = spent.get("provider_setup_requests")
        if type(setup_requests) is not int or setup_requests < 0:
            return False
        if spent.get("model_requests") != arm_requests + setup_requests:
            return False
        if spent.get("input_tokens") != arm_input_tokens or spent.get("output_tokens") != arm_output_tokens:
            return False
        if not math.isclose(float(spent.get("cost_usd", math.nan)), float(arm_raw_cost), rel_tol=0.0, abs_tol=1e-9):
            return False
        if not math.isclose(float(spent.get("model_work_seconds", math.nan)), arm_work, rel_tol=0.0, abs_tol=1e-9):
            return False
        for key, expected_value in (
            ("model_requests", arm_requests),
            ("provider_setup_requests", setup_requests),
            ("input_tokens", arm_input_tokens),
            ("output_tokens", arm_output_tokens),
            ("total_tokens", arm_input_tokens + arm_output_tokens),
        ):
            if arm_budget.get(key) != expected_value:
                return False
        for key in ("cost_usd", "wall_seconds", "model_work_seconds"):
            try:
                spent_value = float(spent[key])
                arm_value = float(arm_budget[key])
            except (KeyError, TypeError, ValueError):
                return False
            if not math.isfinite(spent_value) or not math.isfinite(arm_value):
                return False
            if not math.isclose(spent_value, arm_value, rel_tol=0.0, abs_tol=1e-9):
                return False
        max_checkpoint_wall = max(max_checkpoint_wall, float(spent.get("wall_seconds", 0.0)))
        totals["model_requests"] += arm_requests
        totals["provider_setup_requests"] += setup_requests
        totals["input_tokens"] += arm_input_tokens
        totals["output_tokens"] += arm_output_tokens
        total_cost += arm_conservative_cost
        total_work += arm_work

    expected_totals = {
        "model_requests": totals["model_requests"] + residual_model_requests,
        "provider_setup_requests": totals["provider_setup_requests"] + residual_setup_requests,
        "input_tokens": totals["input_tokens"] + residual_input_tokens,
        "output_tokens": totals["output_tokens"] + residual_output_tokens,
    }
    if any(paired_budget_used.get(key) != value for key, value in expected_totals.items()):
        return False
    if paired_budget_used.get("total_tokens") != expected_totals["input_tokens"] + expected_totals["output_tokens"]:
        return False
    try:
        paired_cost = float(paired_budget_used["cost_usd"])
        paired_wall = float(paired_budget_used["wall_seconds"])
        paired_work = float(paired_budget_used["model_work_seconds"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        math.isfinite(paired_cost)
        and math.isclose(
            paired_cost,
            float(total_cost + residual_conservative_cost),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        and math.isfinite(paired_wall)
        and paired_wall >= max_checkpoint_wall
        and math.isfinite(paired_work)
        and math.isclose(
            paired_work,
            total_work + residual_model_work,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    )


def _reconcile_paired_artifact_reservations(
    artifacts: dict[str, dict[str, Any]],
    *,
    paired_budget_used: dict[str, Any],
    paired_run_terminal: bool,
) -> None:
    orphan_reason = "benchmark incomplete: artifact has an orphaned reservation"

    def mark_incomplete(reason: str) -> None:
        for artifact in artifacts.values():
            reasons = artifact.get("incomplete_reasons")
            reasons = list(reasons) if isinstance(reasons, list) else []
            if reason not in reasons:
                reasons.append(reason)
            artifact["incomplete_reasons"] = sorted(reasons)
            artifact["status"] = "incomplete"

    if not paired_run_terminal:
        mark_incomplete("benchmark incomplete: paired arms did not reach a terminal state")
        return
    try:
        final_reserved = Decimal(str(paired_budget_used["final_reserved_cost_usd"]))
    except (InvalidOperation, KeyError, TypeError):
        mark_incomplete("benchmark incomplete: paired ledger reservation evidence is invalid")
        return
    if not final_reserved.is_finite() or final_reserved != 0:
        mark_incomplete("benchmark incomplete: paired ledger ended with a reserved cost")
        return
    if not _paired_artifact_receipts_reconcile(artifacts, paired_budget_used):
        mark_incomplete("benchmark incomplete: paired ledger receipts do not reconcile with arm checkpoints")
        return

    for artifact in artifacts.values():
        reasons = artifact.get("incomplete_reasons")
        reasons = list(reasons) if isinstance(reasons, list) else []
        had_orphan_reason = orphan_reason in reasons
        artifact["incomplete_reasons"] = sorted(reason for reason in reasons if reason != orphan_reason)
        if artifact.get("failure") or artifact["incomplete_reasons"]:
            artifact["status"] = "incomplete"
        elif artifact.get("status") == "incomplete" and had_orphan_reason:
            artifact["status"] = "complete"


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
    unfinished = evidence.get("unfinished_starts", [])
    if not isinstance(events, list) or not isinstance(reused, list) or not isinstance(unfinished, list):
        raise ValueError("paired execution records are invalid")
    event_sequences = [item.get("sequence") for item in events if isinstance(item, dict)]
    if event_sequences != list(range(1, len(events) + 1)):
        raise ValueError("paired execution sequence is not contiguous")
    event_chains: dict[tuple[str, str, int, str], list[dict[str, Any]]] = defaultdict(list)
    event_by_sequence: dict[int, dict[str, Any]] = {}
    first_attempts_by_cell: dict[str, list[tuple[str, int, ArmName]]] = defaultdict(list)
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("paired execution event is invalid")
        identity = (
            event.get("cell"),
            event.get("task_id"),
            event.get("repeat_index"),
            event.get("arm"),
        )
        if not (
            isinstance(identity[0], str)
            and isinstance(identity[1], str)
            and type(identity[2]) is int
            and identity[3] in {"baseline", "candidate"}
            and type(event.get("sequence")) is int
            and type(event.get("order_position")) is int
        ):
            raise ValueError("paired execution identity is invalid")
        cell, task_id, repeat_index, arm = cast(tuple[str, str, int, ArmName], identity)
        typed_identity = (cell, task_id, repeat_index, arm)
        expected_order = planned_arm_order(task_id, repeat_index, manifest["paired_order_seed"])
        if event["order_position"] != expected_order.index(arm):
            raise ValueError("paired execution changed the preregistered arm order")
        chain = event_chains[typed_identity]
        attempt = event.get("attempt", 1)
        if type(attempt) is not int or attempt != len(chain) + 1:
            raise ValueError("paired execution retry index is invalid")
        if attempt == 1:
            if "retry_of_sequence" in event:
                raise ValueError("first paired execution attempt cannot reference a retry")
            first_attempts_by_cell[cell].append((task_id, repeat_index, arm))
        elif event.get("retry_of_sequence") != chain[-1].get("sequence"):
            raise ValueError("paired execution retry does not link to its preceding start")
        chain.append(event)
        event_by_sequence[event["sequence"]] = event

    task_contracts = baseline.get("task_contracts")
    if not isinstance(task_contracts, list):
        raise ValueError("paired execution task contracts are missing")
    tasks = [TaskManifest.model_validate(item) for item in task_contracts]
    expected_schedule_by_cell: dict[str, list[tuple[str, int, ArmName]]] = {}
    for model_spec in manifest["models"]:
        for transport in manifest["transports"]:
            cell = f"{model_spec['class_name']}:{transport}"
            expected_schedule_by_cell[cell] = [
                (task.id, repeat_index, arm)
                for repeat_index in range(1, int(manifest["repeats"]) + 1)
                for task in tasks
                if transport in task.fixture.transports
                for arm in planned_arm_order(task.id, repeat_index, manifest["paired_order_seed"])
            ]
    for cell, first_attempts in first_attempts_by_cell.items():
        schedule = expected_schedule_by_cell.get(cell)
        if schedule is None or first_attempts != schedule[: len(first_attempts)]:
            raise ValueError("paired execution did not preserve the per-cell schedule prefix")

    reused_identities: set[tuple[str, str, int, str]] = set()
    for item in reused:
        if not isinstance(item, dict):
            raise ValueError("paired reuse record is invalid")
        identity = (item.get("cell"), item.get("task_id"), item.get("repeat_index"), item.get("arm"))
        if (
            not isinstance(identity[0], str)
            or not isinstance(identity[1], str)
            or type(identity[2]) is not int
            or identity[3] not in {"baseline", "candidate"}
            or identity in reused_identities
            or identity in event_chains
        ):
            raise ValueError("paired reuse identity is invalid or duplicated")
        expected_order = planned_arm_order(identity[1], identity[2], manifest["paired_order_seed"])
        if item.get("order_position") != expected_order.index(identity[3]):
            raise ValueError("paired reuse changed the preregistered arm order")
        reused_identities.add(cast(tuple[str, str, int, str], identity))

    outcomes: dict[tuple[str, str, int, str], TrialOutcome] = {}
    terminal_event_sequences: set[int] = set()
    for artifact in (baseline, candidate):
        for cell, run in artifact["runs"].items():
            for raw in run.get("trials", []):
                outcome = TrialOutcome.model_validate(raw)
                identity = (cell, outcome.task_id, outcome.repeat_index, cast(ArmName, artifact["arm"]))
                if identity in outcomes:
                    raise ValueError("paired execution has duplicate terminal outcomes")
                outcomes[identity] = outcome
                chain_events = event_chains.get(identity)
                if chain_events:
                    if outcome.paired_execution_sequence != chain_events[-1].get("sequence"):
                        raise ValueError("trial execution sequence does not match its latest paired attempt")
                    terminal_event_sequences.add(cast(int, chain_events[-1]["sequence"]))
                elif identity not in reused_identities:
                    raise ValueError("paired execution evidence does not cover every trial")
    if set(outcomes) != set(event_chains) | reused_identities:
        raise ValueError("paired execution evidence does not cover every trial")
    expected_unfinished = [
        event for event in events if event["sequence"] not in terminal_event_sequences
    ]
    if unfinished != expected_unfinished:
        raise ValueError("paired unfinished-start evidence does not match terminal trial rows")
    if any(
        chain[-1]["sequence"] not in terminal_event_sequences
        for chain in event_chains.values()
    ):
        raise ValueError("complete paired execution has an unfinished latest attempt")

    for identity, outcome in outcomes.items():
        cell, task_id, repeat_index, outcome_arm = identity
        expected = planned_arm_order(task_id, repeat_index, manifest["paired_order_seed"])
        if outcome.paired_order_position != expected.index(cast(ArmName, outcome_arm)):
            raise ValueError("trial paired order position does not match preregistration")
        counterpart: ArmName = "candidate" if outcome_arm == "baseline" else "baseline"
        counterpart_identity = (cell, task_id, repeat_index, counterpart)
        if counterpart_identity not in outcomes:
            raise ValueError("paired execution counterpart is missing")

    for cell, task_id, repeat_index, _arm in outcomes:
        expected = planned_arm_order(task_id, repeat_index, manifest["paired_order_seed"])
        first_identity = (cell, task_id, repeat_index, expected[0])
        second_identity = (cell, task_id, repeat_index, expected[1])
        first_chain = event_chains.get(first_identity)
        second_chain = event_chains.get(second_identity)
        if first_chain and second_chain and not (
            first_chain[0]["sequence"] < second_chain[0]["sequence"]
        ):
            raise ValueError("paired execution violated the preregistered arm order")
        if first_chain and second_identity in reused_identities:
            raise ValueError("paired resume reused the second arm before the first arm completed")


def _validate_artifact_pair(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    for artifact, expected_arm in ((baseline, "baseline"), (candidate, "candidate")):
        if artifact.get("schema_version") != 2 or artifact.get("arm") != expected_arm:
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
        "resume_source",
        "continuation_lineage",
        "provider_registry",
        "pre_smoke_seal_hash",
        "execution_environment",
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
            "include_instructions": True,
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
