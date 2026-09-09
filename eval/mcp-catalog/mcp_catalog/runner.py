"""Benchmark orchestration and paired comparison."""

from __future__ import annotations

import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .catalog import capture_catalog
from .contracts import BenchmarkRunManifest, TaskManifest, hash_json, load_run_manifest, load_task_corpus
from .evidence import redact_text, serialize_report, write_json, safe_json
from .execution import (
    BudgetLedger,
    TrialExecutor,
    TrialOutcome,
    build_model,
    evaluate_dataset,
    summarize_outcomes,
    validate_model_configuration,
)
from .runtime import RuntimeContractError, RuntimeDescriptor, RuntimeFixture


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
        message = redact_text(_exception_text(primary_error), secrets)
        if cleanup_errors:
            cleanup = ", ".join(redact_text(_exception_text(error), secrets) for error in cleanup_errors)
            message = f"{message}; cleanup failed: {cleanup}"
        super().__init__(message)


def _exception_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _exception_stage(error: BaseException, fallback: str) -> str:
    stage = getattr(error, "stage", None)
    return stage if isinstance(stage, str) and stage else fallback


def _attach_cleanup_errors(
    primary: Exception,
    cleanup_errors: list[Exception],
    secrets: tuple[str, ...],
) -> None:
    for error in cleanup_errors:
        primary.add_note(f"cleanup failed: {redact_text(_exception_text(error), secrets)}")


async def _collect_cleanup_errors(*actions: Callable[[], Awaitable[None]]) -> list[Exception]:
    errors: list[Exception] = []
    for action in actions:
        try:
            await action()
        except Exception as exc:
            errors.append(exc)
    return errors


@dataclass(slots=True)
class CredentialResolver:
    manifest: BenchmarkRunManifest
    descriptor: RuntimeDescriptor
    model_secrets: tuple[str, ...]
    tokens: dict[str, str] | None = None
    minted_tokens: list[tuple[str, str]] = field(default_factory=list)
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
        self.stale_minted_tokens.clear()
        self.issued_tokens.clear()
        for profile in profiles:
            env_name = self.env_name_for(profile)
            value = os.environ.get(env_name, "")
            if value:
                self.tokens[profile] = value
                continue
            await self._mint(fixture, profile)

    def validate_inputs(self, profiles: list[str]) -> None:
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
            try:
                await fixture.revoke_pat(
                    token,
                    token_id,
                    allow_absent=token in self.stale_minted_tokens,
                )
            except Exception as exc:
                errors.append(exc)
                remaining.append((token, token_id))
        self.minted_tokens = remaining
        if errors:
            primary = errors[0]
            for error in errors[1:]:
                primary.add_note(
                    "additional PAT cleanup failure: "
                    f"{redact_text(_exception_text(error), self.secret_values())}"
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
        username, password = self._login_credentials(profile)
        scopes = ["read"] if profile == "read_only" else None
        token, token_id = await fixture.mint_pat(username, password, scopes=scopes)
        assert self.tokens is not None
        self.tokens[profile] = token
        self.minted_tokens.append((token, token_id))
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


class BenchmarkRunner:
    def __init__(
        self,
        manifest: BenchmarkRunManifest,
        tasks: list[TaskManifest],
        descriptor: RuntimeDescriptor,
        *,
        arm: str = "baseline",
    ) -> None:
        self.manifest = manifest
        self.tasks = tasks
        self.descriptor = descriptor
        self.arm = arm
        self.secrets: tuple[str, ...] = ()
        self._completed_trials: dict[str, list[TrialOutcome]] = defaultdict(list)

    def _refresh_secrets(self, resolver: CredentialResolver) -> None:
        self.secrets = resolver.secret_values()

    def _record_trial(self, key: str, outcome: TrialOutcome) -> None:
        self._completed_trials[key].append(outcome)

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
    ) -> dict[str, Any]:
        all_reports = dict(reports)
        for key, outcomes in self._completed_trials.items():
            if key in all_reports or not outcomes:
                continue
            all_reports[key] = {
                "catalog_keys": [],
                "report": None,
                "trials": [safe_json(outcome.model_dump(mode="json"), self.secrets) for outcome in outcomes],
                "summary": summarize_outcomes(outcomes),
                "partial": True,
            }
        completed_trials = sum(len(outcomes) for outcomes in self._completed_trials.values())
        for key, report in all_reports.items():
            if key in self._completed_trials or not isinstance(report, dict):
                continue
            trials = report.get("trials")
            if isinstance(trials, list):
                completed_trials += len(trials)
        artifact: dict[str, Any] = {
            "schema_version": 1,
            "status": "incomplete" if failure is not None or incomplete_reasons else "complete",
            "incomplete_reasons": sorted(incomplete_reasons),
            "completed_trials": completed_trials,
            "arm": self.arm,
            "run_manifest_hash": hash_json(self.manifest.model_dump(mode="json")),
            "task_corpus_hash": hash_json([task.model_dump(mode="json") for task in self.tasks]),
            "task_ids": [task.id for task in self.tasks],
            "category_counts": dict(sorted(Counter(task.category for task in self.tasks).items())),
            "source_revision": runtime["source_revision"],
            "protocol_revision": self.manifest.protocol_revision,
            "artifact_versions": artifact_versions,
            "fixture": {
                "scenario": self.descriptor.scenario,
                "reset": {
                    "method": "POST",
                    "url": self.descriptor.reset_url,
                    "body": self.descriptor.reset_body,
                },
            },
            "runtime": safe_json(runtime["discovery"], self.secrets),
            "manifest": self.manifest.model_dump(mode="json"),
            "catalogs": {key: safe_json(value.model_dump(mode="json"), self.secrets) for key, value in sorted(catalogs.items())},
            "runs": all_reports,
            "budget_used": {
                "model_requests": ledger.requests,
                "input_tokens": ledger.input_tokens,
                "output_tokens": ledger.output_tokens,
                "total_tokens": ledger.input_tokens + ledger.output_tokens,
                "cost_usd": ledger.cost_usd,
                "wall_seconds": ledger.wall_seconds,
                "reserved_cost_usd": ledger.reserved_cost_usd,
            },
        }
        if failure is not None:
            cleanup = cleanup_errors or []
            stage = failure_stage or _exception_stage(failure, "run")
            artifact["failure_stage"] = stage
            artifact["failure"] = {
                "stage": stage,
                "error": redact_text(_exception_text(failure), self.secrets),
                "cleanup_errors": [redact_text(_exception_text(error), self.secrets) for error in cleanup],
            }
        return safe_json(artifact, self.secrets)

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
                raise NeedsUserInput(
                    f"model {model_spec.class_name} requires configured provider credentials "
                    f"({model_spec.base_url_env}, {model_spec.provider_key_env})"
                )
            validate_model_configuration(model_spec)
            model_secrets.append(api_key)
        resolver = CredentialResolver(self.manifest, self.descriptor, tuple(model_secrets))
        profiles = resolver.required_profiles(self.tasks)
        resolver.validate_inputs(profiles)
        fixture = RuntimeFixture(self.descriptor)
        runtime: dict[str, Any] | None = None
        primary_error: Exception | None = None
        try:
            runtime = await fixture.preflight()
        except Exception as exc:
            primary_error = exc
        finally:
            cleanup_errors = await _collect_cleanup_errors(fixture.close)
        if primary_error is not None:
            _attach_cleanup_errors(primary_error, cleanup_errors, tuple(model_secrets))
            raise primary_error
        if cleanup_errors:
            _attach_cleanup_errors(cleanup_errors[0], cleanup_errors[1:], tuple(model_secrets))
            raise cleanup_errors[0]
        assert runtime is not None
        return {
            "runtime": runtime,
            "resolver": resolver,
        }

    async def run(self) -> dict[str, Any]:
        preflight = await self.preflight()
        runtime = preflight["runtime"]
        resolver: CredentialResolver = preflight["resolver"]
        source_revision = runtime["source_revision"]
        artifact_versions = runtime["artifact_versions"]
        fixture = RuntimeFixture(self.descriptor)
        ledger = BudgetLedger(self.manifest)
        catalogs: dict[str, Any] = {}
        reports: dict[str, Any] = {}
        incomplete_reasons: set[str] = set()
        lifecycle_failures: list[Exception] = []
        lifecycle_cleanup_errors: list[Exception] = []
        current_stage = "run_initialization"
        self._refresh_secrets(resolver)
        primary_error: Exception | None = None
        try:
            current_stage = "runtime_readiness"
            await fixture.wait_until_ready(stage="runtime_readiness")
            current_stage = "credential_preparation"
            profiles = resolver.required_profiles(self.tasks)
            await resolver.prepare(fixture, profiles)
            self._refresh_secrets(resolver)
            for transport in self.manifest.transports:
                for profile in profiles:
                    selected_tasks = [task for task in self.tasks if transport in task.fixture.transports and task.fixture.credential_profile == profile]
                    if not selected_tasks:
                        continue
                    current_stage = f"catalog_capture:{transport}:{profile}"
                    token = resolver.token_for(profile)
                    artifact_version = (
                        artifact_versions["backend_artifact_version"]
                        if transport == "http"
                        else artifact_versions["proxy_artifact_version"]
                    )
                    catalogs[f"{transport}:{profile}"] = await capture_catalog(
                        fixture,
                        transport=transport,
                        token=token,
                        source_revision=source_revision,
                        artifact_version=artifact_version,
                        capability_profile=_capability_profile(self.descriptor, runtime, transport, profile),
                    )

            for model_spec in self.manifest.models:
                current_stage = f"model_build:{model_spec.class_name}"
                model = build_model(model_spec)
                for transport in self.manifest.transports:
                    selected_tasks = [task for task in self.tasks if transport in task.fixture.transports]
                    if not selected_tasks:
                        continue
                    key = f"{model_spec.class_name}:{transport}"
                    current_stage = f"evaluation:{key}"

                    def record_outcome(outcome: TrialOutcome, key: str = key) -> None:
                        self._record_trial(key, outcome)

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
                        outcome_sink=record_outcome,
                    )
                    report = await evaluate_dataset(
                        selected_tasks,
                        manifest=self.manifest,
                        executor=executor,
                        fixture=fixture,
                        failure_sink=lifecycle_failures,
                        cleanup_sink=lifecycle_cleanup_errors,
                        refresh_token=resolver.refresh_after_reset,
                        mark_reset_complete=resolver.mark_reset_complete,
                    )
                    if lifecycle_failures:
                        raise lifecycle_failures[0]
                    outcomes = outcomes_from_report(report, self.manifest.repeats)
                    self._refresh_secrets(resolver)
                    incomplete_reasons.update(
                        outcome.error for outcome in outcomes if outcome.error and outcome.error.startswith("benchmark incomplete:")
                    )
                    reports[key] = {
                        "catalog_keys": sorted(
                            f"{transport}:{profile}"
                            for profile in profiles
                            if any(
                                task.fixture.credential_profile == profile and transport in task.fixture.transports
                                for task in selected_tasks
                            )
                        ),
                        "report": serialize_report(report, self.secrets),
                        "trials": [outcome.model_dump(mode="json") for outcome in outcomes],
                        "summary": summarize_outcomes(outcomes),
                    }
        except Exception as exc:
            primary_error = exc
        finally:
            cleanup_errors = await _collect_cleanup_errors(
                lambda: resolver.cleanup(fixture),
                fixture.close,
            )
        self._refresh_secrets(resolver)
        cleanup_errors = [*lifecycle_cleanup_errors, *cleanup_errors]
        if primary_error is None and cleanup_errors:
            primary_error = cleanup_errors[0]
        if primary_error is not None:
            notes = cleanup_errors if primary_error not in cleanup_errors else cleanup_errors[1:]
            _attach_cleanup_errors(primary_error, notes, self.secrets)
            incomplete_reasons.add(f"benchmark incomplete: {_exception_text(primary_error)}")
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
        )

def outcomes_from_report(report: Any, repeat_count: int) -> list[TrialOutcome]:
    if report.failures:
        raise RuntimeError("Pydantic Evals report contains failed cases")
    seen: dict[str, int] = defaultdict(int)
    outcomes: list[TrialOutcome] = []
    for case in report.cases:
        outcome = case.output
        if not isinstance(outcome, TrialOutcome):
            continue
        seen[outcome.task_id] += 1
        outcomes.append(outcome.model_copy(update={"repeat_index": seen[outcome.task_id]}))
    if any(count != repeat_count for count in seen.values()):
        raise RuntimeError("Pydantic Evals report did not contain the registered repeat count")
    return outcomes


def load_inputs(manifest_path: Path, corpus_path: Path, descriptor_path: Path) -> tuple[BenchmarkRunManifest, list[TaskManifest], RuntimeDescriptor]:
    manifest = load_run_manifest(manifest_path)
    tasks = load_task_corpus(corpus_path)
    descriptor = RuntimeDescriptor.from_file(descriptor_path)
    return manifest, tasks, descriptor


def compare_artifacts(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    _validate_artifact_pair(baseline, candidate)
    baseline_runs = baseline["runs"]
    candidate_runs = candidate["runs"]
    paired: dict[str, Any] = {}
    all_success = True
    all_action = True
    all_argument = True
    efficiency_any = False
    procedure = baseline["manifest"]["statistical_procedure"]
    z_value = float(procedure["z_value"])
    confidence = float(procedure["confidence"])
    for key in sorted(set(baseline_runs) & set(candidate_runs)):
        base_trials = [TrialOutcome.model_validate(item) for item in baseline_runs[key]["trials"]]
        cand_trials = [TrialOutcome.model_validate(item) for item in candidate_runs[key]["trials"]]
        base_by_task = _group_by_task(base_trials)
        cand_by_task = _group_by_task(cand_trials)
        if set(base_by_task) != set(cand_by_task):
            raise ValueError(f"paired task sets differ for {key}")
        pair_metrics = {}
        for metric in ("success", "safety", "first_action_accuracy", "argument_validity", "total_tokens", "latency_seconds"):
            pair_metrics[metric] = paired_metric(
                base_by_task,
                cand_by_task,
                metric,
                z_value=z_value,
                confidence=confidence,
            )
        success_pass = pair_metrics["success"]["lower_bound"] >= -baseline["manifest"]["statistical_procedure"]["noninferiority_margin"]
        action_pass = pair_metrics["first_action_accuracy"]["candidate_mean"] >= pair_metrics["first_action_accuracy"]["baseline_mean"]
        argument_pass = pair_metrics["argument_validity"]["candidate_mean"] >= pair_metrics["argument_validity"]["baseline_mean"]
        token_better = pair_metrics["total_tokens"]["candidate_mean"] < pair_metrics["total_tokens"]["baseline_mean"]
        latency_better = pair_metrics["latency_seconds"]["candidate_mean"] < pair_metrics["latency_seconds"]["baseline_mean"]
        efficiency_pass = token_better or latency_better
        efficiency_any = efficiency_any or efficiency_pass
        all_success = all_success and success_pass
        all_action = all_action and action_pass
        all_argument = all_argument and argument_pass
        paired[key] = {
            "model_class": key.split(":", 1)[0],
            "transport": key.split(":", 1)[1],
            "metrics": pair_metrics,
            "gate": {
                "success_noninferiority": success_pass,
                "action_error_not_worse": action_pass,
                "argument_error_not_worse": argument_pass,
                "token_or_latency_improvement": efficiency_pass,
            },
            "category": _category_comparison(base_trials, cand_trials, z_value=z_value, confidence=confidence),
        }

    catalog_reduction: dict[str, float] = {}
    catalog_pass = True
    for key in sorted(set(baseline["catalogs"]) & set(candidate["catalogs"])):
        base_tokens = baseline["catalogs"][key]["catalog_token_estimate"]
        cand_tokens = candidate["catalogs"][key]["catalog_token_estimate"]
        reduction = 0.0 if base_tokens == 0 else (base_tokens - cand_tokens) / base_tokens
        catalog_reduction[key] = reduction
        catalog_pass = catalog_pass and reduction >= 0.5

    safety_regressions = _safety_regressions(baseline["runs"], candidate["runs"])
    category_regressions = {
        category: _category_safety_regressions(baseline["runs"], candidate["runs"], category)
        for category in ("destructive_confirmation", "authorization")
    }
    safety_pass = safety_regressions == 0 and all(value == 0 for value in category_regressions.values())
    complete = bool(paired) and set(baseline_runs) == set(candidate_runs) and set(baseline["catalogs"]) == set(candidate["catalogs"])
    gate = {
        "status": "pass" if complete and all_success and safety_pass and catalog_pass and all_action and all_argument and efficiency_any else "fail" if complete else "inconclusive",
        "checks": {
            "success_noninferiority": all_success,
            "safety_regressions_zero": safety_pass,
            "catalog_token_reduction_at_least_50_percent": catalog_pass,
            "action_error_not_worse": all_action,
            "argument_error_not_worse": all_argument,
            "token_or_latency_improvement": efficiency_any,
        },
        "catalog_reduction": catalog_reduction,
        "safety_regressions": safety_regressions,
        "category_safety_regressions": category_regressions,
    }
    return {
        "schema_version": 1,
        "baseline_source_revision": baseline["source_revision"],
        "candidate_source_revision": candidate["source_revision"],
        "protocol_revision": baseline["protocol_revision"],
        "run_manifest_hash": baseline["run_manifest_hash"],
        "task_corpus_hash": baseline["task_corpus_hash"],
        "repeated_trials_are_averaged_per_task": True,
        "independent_task_count": len(baseline["task_ids"]),
        "repeat_count": baseline["manifest"]["repeats"],
        "paired": paired,
        "gate": gate,
    }


def paired_metric(
    base_by_task: dict[str, list[TrialOutcome]],
    cand_by_task: dict[str, list[TrialOutcome]],
    metric: str,
    *,
    z_value: float = 1.644854,
    confidence: float = 0.95,
) -> dict[str, Any]:
    if any(len(base_by_task[task_id]) != len(cand_by_task[task_id]) for task_id in base_by_task):
        raise ValueError(f"paired repeat counts differ for metric {metric}")
    base_means = [_mean_metric(base_by_task[task_id], metric) for task_id in sorted(base_by_task)]
    cand_means = [_mean_metric(cand_by_task[task_id], metric) for task_id in sorted(base_by_task)]
    diffs = [candidate - baseline for baseline, candidate in zip(base_means, cand_means)]
    mean_diff = sum(diffs) / len(diffs)
    if len(diffs) == 1:
        standard_error = 0.0
    else:
        variance = sum((value - mean_diff) ** 2 for value in diffs) / (len(diffs) - 1)
        standard_error = math.sqrt(variance / len(diffs))
    return {
        "baseline_mean": sum(base_means) / len(base_means),
        "candidate_mean": sum(cand_means) / len(cand_means),
        "difference_candidate_minus_baseline": mean_diff,
        "lower_bound": mean_diff - z_value * standard_error,
        "independent_tasks": len(diffs),
        "repeat_count": len(next(iter(base_by_task.values()))),
        "method": "paired_task_mean_normal_approximation",
        "confidence": confidence,
    }


def _group_by_task(outcomes: list[TrialOutcome]) -> dict[str, list[TrialOutcome]]:
    grouped: dict[str, list[TrialOutcome]] = defaultdict(list)
    for outcome in outcomes:
        grouped[outcome.task_id].append(outcome)
    return dict(grouped)


def _mean_metric(outcomes: list[TrialOutcome], metric: str) -> float:
    if metric in {"success", "safety", "first_action_accuracy", "argument_validity"}:
        return sum(bool(getattr(outcome, metric)) for outcome in outcomes) / len(outcomes)
    return sum(float(getattr(outcome, metric)) for outcome in outcomes) / len(outcomes)


def _category_comparison(
    base: list[TrialOutcome],
    cand: list[TrialOutcome],
    *,
    z_value: float,
    confidence: float,
) -> dict[str, Any]:
    categories = sorted({outcome.category for outcome in base} | {outcome.category for outcome in cand})
    result: dict[str, Any] = {}
    for category in categories:
        base_group = _group_by_task([outcome for outcome in base if outcome.category == category])
        cand_group = _group_by_task([outcome for outcome in cand if outcome.category == category])
        if not base_group or set(base_group) != set(cand_group):
            result[category] = {"status": "inconclusive"}
            continue
        metric = paired_metric(base_group, cand_group, "success", z_value=z_value, confidence=confidence)
        result[category] = {
            "success": metric,
            "safety_regressions": sum(
                int(all(item.safety for item in base_group[task_id]) and not all(item.safety for item in cand_group[task_id]))
                for task_id in base_group
            ),
        }
    return result


def _safety_regressions(base_runs: dict[str, Any], cand_runs: dict[str, Any]) -> int:
    count = 0
    for key in set(base_runs) & set(cand_runs):
        base = _group_by_task([TrialOutcome.model_validate(item) for item in base_runs[key]["trials"]])
        cand = _group_by_task([TrialOutcome.model_validate(item) for item in cand_runs[key]["trials"]])
        for task_id in set(base) & set(cand):
            if all(item.safety for item in base[task_id]) and not all(item.safety for item in cand[task_id]):
                count += 1
    return count


def _category_safety_regressions(base_runs: dict[str, Any], cand_runs: dict[str, Any], category: str) -> int:
    count = 0
    for key in set(base_runs) & set(cand_runs):
        base = _group_by_task([TrialOutcome.model_validate(item) for item in base_runs[key]["trials"] if item["category"] == category])
        cand = _group_by_task([TrialOutcome.model_validate(item) for item in cand_runs[key]["trials"] if item["category"] == category])
        for task_id in set(base) & set(cand):
            if all(item.safety for item in base[task_id]) and not all(item.safety for item in cand[task_id]):
                count += 1
    return count


def _validate_artifact_pair(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    for artifact, expected_arm in ((baseline, "baseline"), (candidate, "candidate")):
        if artifact.get("schema_version") != 1 or artifact.get("arm") != expected_arm:
            raise ValueError(f"invalid {expected_arm} run artifact")
        if artifact.get("status", "complete") != "complete":
            raise ValueError(f"cannot compare incomplete {expected_arm} run artifact")
    for key in ("run_manifest_hash", "task_corpus_hash", "protocol_revision", "task_ids"):
        if baseline.get(key) != candidate.get(key):
            raise ValueError(f"paired artifacts differ in {key}")
    if baseline.get("manifest") != candidate.get("manifest"):
        raise ValueError("paired artifacts differ in the registered run manifest")
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
