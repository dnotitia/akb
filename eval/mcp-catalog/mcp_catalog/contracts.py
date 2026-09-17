"""Strict, source-neutral benchmark contracts and deterministic serialization."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PROTOCOL_REVISION = "2026-07-28"
CONTRACT_SCHEMA_VERSION = 2
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_BASE_URL_ENV = "MCP_BENCH_OPENROUTER_BASE_URL"
OPENROUTER_PROVIDER_KEY_ENV = "MCP_BENCH_OPENROUTER_API_KEY"
OPENROUTER_PRICE_CEILINGS = {
    "deepseek/deepseek-v4-flash-0731": (0.14, 0.28),
    "qwen/qwen3.8-27b": (0.24, 2.20),
}
OPENROUTER_CLASSES = {
    "primary": "deepseek/deepseek-v4-flash-0731",
    "lightweight": "qwen/qwen3.8-27b",
}
TASK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOOL_NAME_RE = re.compile(r"\bakb_[a-z0-9_]+\b", re.IGNORECASE)
JSON_POINTER_RE = re.compile(r"^(?:|/(?:[^/~]|~0|~1)*(?:/(?:[^/~]|~0|~1)*)*)$")

Category = Literal[
    "single_operation",
    "ambiguous_action",
    "multi_step",
    "destructive_confirmation",
    "authorization",
    "invalid_input_recovery",
    "stdio_local",
]
Suite = Literal["capability", "tool_surface_risk"]
CapabilityFamily = Literal[
    "vault_discovery_metadata_lifecycle",
    "document_discovery_read_history",
    "document_mutation",
    "collection_lifecycle",
    "relation_graph",
    "table_lifecycle",
    "publication_lifecycle",
    "identity_access_management",
    "import_export",
    "stdio_local_files",
]
RiskHypothesis = Literal[
    "overlapping_read_semantics",
    "overlapping_mutation_semantics",
    "scope_ambiguity",
    "unnecessary_identity_access_preflight",
    "post_completion_overshoot",
    "unsupported_or_fabricated_claim",
    "argument_target_confusion",
    "destructive_authorization_boundary",
    "multi_step_planning_binding",
    "transport_divergence",
]
CAPABILITY_FAMILIES: tuple[CapabilityFamily, ...] = (
    "vault_discovery_metadata_lifecycle",
    "document_discovery_read_history",
    "document_mutation",
    "collection_lifecycle",
    "relation_graph",
    "table_lifecycle",
    "publication_lifecycle",
    "identity_access_management",
    "import_export",
    "stdio_local_files",
)
RISK_HYPOTHESES: tuple[RiskHypothesis, ...] = (
    "overlapping_read_semantics",
    "overlapping_mutation_semantics",
    "scope_ambiguity",
    "unnecessary_identity_access_preflight",
    "post_completion_overshoot",
    "unsupported_or_fabricated_claim",
    "argument_target_confusion",
    "destructive_authorization_boundary",
    "multi_step_planning_binding",
    "transport_divergence",
)
ResourceType = Literal[
    "vault",
    "collection",
    "document",
    "relation",
    "table",
    "publication",
    "identity",
    "access",
    "archive",
    "file",
    "image",
    "help",
    "unknown",
]
TaskLocale = Literal["ko-KR", "en-US"]
Transport = Literal["http", "stdio"]
ArmName = Literal["baseline", "candidate"]
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

EXPECTED_LOCALE_COUNTS: dict[TaskLocale, int] = {"ko-KR": 8, "en-US": 8}
EXPECTED_PAIR_CATEGORIES: dict[str, Category] = {
    "read-vaults": "single_operation",
    "create-vault": "single_operation",
    "ambiguous-find": "ambiguous_action",
    "multi-step": "multi_step",
    "destructive-confirm": "destructive_confirmation",
    "authorization-readonly": "authorization",
    "invalid-recovery": "invalid_input_recovery",
    "stdio-local": "stdio_local",
}
PAIR_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")


def default_transports() -> list[Transport]:
    return ["http", "stdio"]


def default_arms() -> list[ArmName]:
    return ["baseline", "candidate"]


def default_locales() -> list[TaskLocale]:
    return ["ko-KR", "en-US"]


def default_locale_counts() -> dict[TaskLocale, int]:
    return dict(EXPECTED_LOCALE_COUNTS)


def default_pair_categories() -> dict[str, Category]:
    return dict(EXPECTED_PAIR_CATEGORIES)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class StateProbe(ContractModel):
    """A read-only state endpoint owned by the runtime or application."""

    service: Literal["app", "fixture"]
    method: Literal["GET", "POST"] = "GET"
    path: str = Field(pattern=r"^/[A-Za-z0-9_./{}?=&-]*$")
    body: dict[str, JsonValue] | None = None
    expected_status: int = Field(default=200, ge=100, le=599)


class StateExpectation(ContractModel):
    pointer: str = Field(pattern=JSON_POINTER_RE.pattern)
    operator: Literal["equals", "contains", "not_contains", "exists"]
    value: JsonValue = None

    @model_validator(mode="after")
    def validate_value(self) -> StateExpectation:
        if self.operator == "exists" and not isinstance(self.value, bool):
            raise ValueError("exists expectations require a boolean value")
        if self.operator in {"equals", "contains", "not_contains"} and self.value is None:
            raise ValueError(f"{self.operator} expectations require value")
        return self


class StateContract(ContractModel):
    probe: StateProbe
    must: list[StateExpectation] = Field(default_factory=list)
    must_not: list[StateExpectation] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)

    @field_validator("unchanged")
    @classmethod
    def validate_unchanged(cls, values: list[str]) -> list[str]:
        for value in values:
            if JSON_POINTER_RE.fullmatch(value) is None:
                raise ValueError(f"invalid JSON pointer: {value}")
        return values


class FixtureContract(ContractModel):
    scenario: str = Field(min_length=1, max_length=100)
    credential_profile: str = Field(default="default", pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    transports: list[Transport] = Field(default_factory=default_transports)
    local_files: list[str] = Field(default_factory=list)

    @field_validator("transports")
    @classmethod
    def unique_transports(cls, values: list[Transport]) -> list[Transport]:
        if not values or len(set(values)) != len(values):
            raise ValueError("fixture transports must contain at least one unique transport")
        return values

    @field_validator("local_files")
    @classmethod
    def validate_local_files(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(
            re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", name) is None or name in {".", ".."}
            for name in values
        ):
            raise ValueError("fixture local_files must contain unique file names")
        return values

    @model_validator(mode="after")
    def validate_local_file_transport(self) -> FixtureContract:
        if self.local_files and "stdio" not in self.transports:
            raise ValueError("fixture local_files require the stdio transport")
        return self


class ResponseRubric(ContractModel):
    required_any_of: list[list[str]] = Field(default_factory=list)
    forbidden_terms: list[str] = Field(default_factory=list)
    require_non_empty: bool = True
    confirmation_terms: list[str] = Field(default_factory=list)
    clarification_terms: list[str] = Field(default_factory=list)
    success_claim_terms: list[str] = Field(default_factory=list)

    @field_validator("required_any_of")
    @classmethod
    def validate_required_groups(cls, values: list[list[str]]) -> list[list[str]]:
        for group in values:
            if not group or any(not value.strip() for value in group):
                raise ValueError("required rubric term groups must be non-empty")
            normalized = [value.casefold() for value in group]
            if len(set(normalized)) != len(normalized):
                raise ValueError("required rubric term groups must be unique")
        return values

    @field_validator("forbidden_terms", "confirmation_terms", "clarification_terms", "success_claim_terms")
    @classmethod
    def validate_terms(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("rubric terms must be non-empty")
        normalized = [value.casefold() for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("rubric terms must be unique")
        return values


class AcceptedBehavior(ContractModel):
    """One user-visible path that satisfies a task without naming a legacy tool."""

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    description: str = Field(min_length=1, max_length=500)
    mode: Literal["complete", "clarify", "refuse", "expected_error"]
    required_operations: list[str] = Field(default_factory=list)
    required_resources: list[ResourceType] = Field(default_factory=list)

    @field_validator("required_operations")
    @classmethod
    def validate_operations(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"^[a-z][a-z0-9_]*$", value) for value in values):
            raise ValueError("accepted behavior operations must be lowercase snake_case")
        if len(set(values)) != len(values):
            raise ValueError("accepted behavior operations must be unique")
        return values


class ClarificationContract(ContractModel):
    expectation: Literal["not_applicable", "required", "forbidden", "allowed"] = "not_applicable"
    match_cardinality: Literal["not_applicable", "zero", "one", "multiple"] = "not_applicable"

    @model_validator(mode="after")
    def validate_cardinality(self) -> ClarificationContract:
        if self.expectation == "not_applicable" and self.match_cardinality != "not_applicable":
            raise ValueError("non-applicable clarification cannot declare match cardinality")
        if self.expectation != "not_applicable" and self.match_cardinality == "not_applicable":
            raise ValueError("clarification contracts must declare match cardinality")
        return self


class StoppingContract(ContractModel):
    completion: Literal[
        "final_user_outcome",
        "clarification",
        "expected_error",
        "immediate_response",
    ] = "final_user_outcome"
    enforce_for_task_success: bool = False
    allowed_follow_up_operations: list[str] = Field(default_factory=list)

    @field_validator("allowed_follow_up_operations")
    @classmethod
    def validate_operations(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"^[a-z][a-z0-9_]*$", value) for value in values):
            raise ValueError("stopping operations must be lowercase snake_case")
        if len(set(values)) != len(values):
            raise ValueError("stopping operations must be unique")
        return values


class ForbiddenMutation(ContractModel):
    operation: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    target_contains: str | None = Field(default=None, min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)


class ExpectedMaterialOutcome(ContractModel):
    logical_operation: str
    outcome: Literal["success", "permission_denied"]
    status_code: int | None = Field(default=None, ge=100, le=599)
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_expected_outcome(self) -> ExpectedMaterialOutcome:
        if self.outcome == "permission_denied":
            if (self.status_code is not None and self.status_code != 403) or not self.error_code:
                raise ValueError("permission_denied outcomes must declare the stable error code and optional HTTP 403")
        elif self.status_code is not None or self.error_code is not None:
            raise ValueError("successful outcomes cannot declare an error response")
        return self


class ExpectedMaterialAttempt(ContractModel):
    logical_operation: str
    tool_name: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    resource_type: ResourceType | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    local_file_arguments: dict[str, str] = Field(default_factory=dict)
    outcome: Literal["success", "permission_denied", "rejected"]
    status_code: int | None = Field(default=None, ge=100, le=599)
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_expected_attempt(self) -> ExpectedMaterialAttempt:
        if self.tool_name is None and self.resource_type is None:
            raise ValueError("material attempts must declare a resource type or an exact tool")
        if self.outcome == "success":
            if self.status_code is not None or self.error_code is not None:
                raise ValueError("successful attempts cannot declare an error response")
        elif self.outcome == "permission_denied":
            if (self.status_code is not None and self.status_code != 403) or not self.error_code:
                raise ValueError("permission_denied attempts must declare the stable error code and optional HTTP 403")
        elif (self.status_code is not None and self.status_code < 400) or not self.error_code:
            raise ValueError("rejected attempts must declare the stable error code and optional HTTP error")
        return self

    @field_validator("local_file_arguments")
    @classmethod
    def validate_local_file_arguments(cls, values: dict[str, str]) -> dict[str, str]:
        if any(
            re.fullmatch(r"^[A-Za-z][A-Za-z0-9_]*$", argument) is None
            or re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", filename) is None
            or filename in {".", ".."}
            for argument, filename in values.items()
        ):
            raise ValueError("local file arguments must map fields to file names")
        return values


class ExpectedResultBinding(ContractModel):
    source_attempt: int = Field(ge=1)
    source_field: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    target_attempt: int = Field(ge=1)
    target_argument: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    relation: Literal["equals", "contains"] = "contains"

    @model_validator(mode="after")
    def validate_binding_order(self) -> ExpectedResultBinding:
        if self.source_attempt >= self.target_attempt:
            raise ValueError("material result bindings must reference an earlier attempt")
        return self


class ProviderRouting(ContractModel):
    order: list[Literal["parasail"]] = Field(min_length=1, max_length=1)
    allow_fallbacks: Literal[True] = True
    require_parameters: Literal[True] = True

    def request_body(self, *, input_price: float, output_price: float) -> dict[str, JsonValue]:
        return {
            "provider": {
                "order": ["parasail"],
                "allow_fallbacks": True,
                "require_parameters": True,
                "max_price": {"prompt": input_price, "completion": output_price},
            }
        }


class TaskManifest(ContractModel):
    schema_version: Literal[2] = 2
    id: str
    suite: Suite
    category: Category
    locale: TaskLocale
    pair_id: str
    capability_families: list[CapabilityFamily] = Field(min_length=1)
    risk_hypotheses: list[RiskHypothesis] = Field(default_factory=list)
    user_outcome: str = Field(min_length=1, max_length=1000)
    accepted_behaviors: list[AcceptedBehavior] = Field(min_length=1)
    clarification: ClarificationContract = Field(default_factory=ClarificationContract)
    stopping: StoppingContract = Field(default_factory=StoppingContract)
    forbidden_mutations: list[ForbiddenMutation] = Field(default_factory=list)
    allowed_resources: list[ResourceType] = Field(min_length=1)
    coverage_tools: list[str] = Field(default_factory=list)
    prompt: str = Field(min_length=1, max_length=4000)
    fixture: FixtureContract
    allowed_preparatory_operations: list[str] = Field(default_factory=list)
    allowed_material_operations: list[str] = Field(min_length=1)
    allowed_first_operations: list[str] = Field(min_length=1)
    forbidden_operations: list[str] = Field(default_factory=list)
    required_attempted_operations: list[str] = Field(default_factory=list)
    material_attempt_limits: dict[str, int] = Field(default_factory=dict)
    material_attempt_minimums: dict[str, int] = Field(default_factory=dict)
    required_operations: list[str] = Field(default_factory=list)
    expected_material_arguments: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    expected_material_outcomes: list[ExpectedMaterialOutcome] = Field(default_factory=list)
    expected_material_attempts: list[ExpectedMaterialAttempt] = Field(default_factory=list)
    expected_result_bindings: list[ExpectedResultBinding] = Field(default_factory=list, max_length=8)
    expected_final_state: StateContract
    response_rubric: ResponseRubric = Field(default_factory=ResponseRubric)

    @field_validator("capability_families", "risk_hypotheses", "allowed_resources", "coverage_tools")
    @classmethod
    def validate_unique_contract_lists(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("semantic contract lists must contain unique values")
        return values

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if TASK_ID_RE.fullmatch(value) is None:
            raise ValueError("task id must be lowercase kebab-case and 3-64 characters")
        return value

    @field_validator("pair_id")
    @classmethod
    def validate_pair_id(cls, value: str) -> str:
        if PAIR_ID_RE.fullmatch(value) is None:
            raise ValueError("pair_id must be lowercase kebab-case and 3-64 characters")
        return value

    @field_validator(
        "allowed_preparatory_operations",
        "allowed_material_operations",
        "allowed_first_operations",
        "forbidden_operations",
        "required_attempted_operations",
        "required_operations",
    )
    @classmethod
    def validate_operations(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"^[a-z][a-z0-9_]*$", value) for value in values):
            raise ValueError("logical operation names must be lowercase snake_case")
        if len(set(values)) != len(values):
            raise ValueError("logical operation names must be unique")
        return values

    @model_validator(mode="after")
    def validate_task_boundaries(self) -> TaskManifest:
        if self.suite == "tool_surface_risk" and not self.risk_hypotheses:
            raise ValueError("tool-surface risk tasks must declare a risk hypothesis")
        if self.suite == "capability" and self.risk_hypotheses:
            raise ValueError("capability tasks cannot declare risk hypotheses")
        if self.clarification.expectation == "required" and not self.response_rubric.clarification_terms:
            raise ValueError("required clarification needs locale-specific clarification terms")
        if self.clarification.expectation != "required" and any(
            behavior.mode == "clarify" for behavior in self.accepted_behaviors
        ):
            raise ValueError("clarification behavior requires a required clarification contract")
        if self.stopping.completion == "clarification" and self.clarification.expectation != "required":
            raise ValueError("clarification stopping requires a required clarification contract")
        if self.stopping.completion == "expected_error" and not any(
            outcome.outcome == "permission_denied" for outcome in self.expected_material_outcomes
        ):
            raise ValueError("expected-error stopping requires an expected permission denial")
        if self.category == "stdio_local" and "stdio" not in self.fixture.transports:
            raise ValueError("stdio_local tasks must run on stdio")
        if set(self.allowed_first_operations) & set(self.forbidden_operations):
            raise ValueError("an operation cannot be both allowed first and forbidden")
        if set(self.allowed_preparatory_operations) & set(self.allowed_material_operations):
            raise ValueError("an operation cannot be both preparatory and material")
        if self.category != "destructive_confirmation" and set(self.allowed_material_operations) & set(self.forbidden_operations):
            raise ValueError("a material operation cannot be forbidden")
        if set(self.required_operations) & set(self.forbidden_operations):
            raise ValueError("a required operation cannot be forbidden")
        if not set(self.required_attempted_operations) <= set(self.allowed_material_operations):
            raise ValueError("required attempted operations must be material operations")
        if any(operation not in self.allowed_material_operations for operation in self.material_attempt_limits):
            raise ValueError("material attempt limits must be material operations")
        if any(operation not in self.allowed_material_operations for operation in self.material_attempt_minimums):
            raise ValueError("material attempt minimums must be material operations")
        if any(limit <= 0 for limit in self.material_attempt_limits.values()):
            raise ValueError("material attempt limits must be positive")
        if any(limit <= 0 for limit in self.material_attempt_minimums.values()):
            raise ValueError("material attempt minimums must be positive")
        if any(
            minimum > self.material_attempt_limits.get(operation, minimum)
            for operation, minimum in self.material_attempt_minimums.items()
        ):
            raise ValueError("material attempt minimum cannot exceed its limit")
        if not set(self.required_operations) <= set(self.allowed_material_operations):
            raise ValueError("required operations must be material operations")
        expected_operations = [item.logical_operation for item in self.expected_material_outcomes]
        if len(set(expected_operations)) != len(expected_operations):
            raise ValueError("expected material outcomes must use unique logical operations")
        if not set(expected_operations) <= set(self.allowed_material_operations):
            raise ValueError("expected material outcomes must be material operations")
        expected_attempt_operations = [item.logical_operation for item in self.expected_material_attempts]
        if not set(expected_attempt_operations) <= set(self.allowed_material_operations):
            raise ValueError("expected material attempts must be material operations")
        if self.fixture.local_files and not self.expected_material_attempts:
            raise ValueError("fixture local_files require expected material attempts")
        for attempt in self.expected_material_attempts:
            if set(attempt.local_file_arguments) & set(attempt.arguments):
                raise ValueError("local file arguments cannot also have static expected values")
            if not set(attempt.local_file_arguments.values()) <= set(self.fixture.local_files):
                raise ValueError("expected local file arguments must reference declared fixture files")
        attempted_local_files = [
            filename
            for attempt in self.expected_material_attempts
            for filename in attempt.local_file_arguments.values()
        ]
        if Counter(attempted_local_files) != Counter(self.fixture.local_files):
            raise ValueError("every fixture local file must be used by exactly one expected material attempt")
        binding_targets: set[tuple[int, str]] = set()
        for binding in self.expected_result_bindings:
            if binding.target_attempt > len(self.expected_material_attempts) or binding.source_attempt > len(self.expected_material_attempts):
                raise ValueError("material result binding attempt is outside the expected attempt sequence")
            target = self.expected_material_attempts[binding.target_attempt - 1]
            if binding.target_argument in target.arguments or binding.target_argument in target.local_file_arguments:
                raise ValueError("bound target arguments cannot also have static expected values")
            target_key = (binding.target_attempt, binding.target_argument)
            if target_key in binding_targets:
                raise ValueError("material result binding targets must be unique")
            binding_targets.add(target_key)
        if not set(self.expected_material_arguments) <= set(self.allowed_material_operations):
            raise ValueError("expected material arguments must be material operations")
        if not set(self.required_attempted_operations) <= set(expected_operations):
            raise ValueError("every required attempted operation needs an expected material outcome")
        attempt_counts = Counter(expected_attempt_operations)
        if any(
            attempt_counts[operation] > limit
            for operation, limit in self.material_attempt_limits.items()
        ):
            raise ValueError("expected material attempts exceed their declared limits")
        if self.category == "stdio_local" and not {"file_upload", "image_upload"} <= set(self.required_operations):
            raise ValueError("stdio_local tasks must require both file and image operations")
        if self.category == "destructive_confirmation" and not self.response_rubric.confirmation_terms:
            raise ValueError("destructive confirmation tasks must declare confirmation terms")
        return self


class ToolCoverageEntry(ContractModel):
    tool: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    transport: Literal["http", "stdio", "both"]
    capability_family: CapabilityFamily
    task_ids: list[str] = Field(default_factory=list)
    coverage_kind: Literal["required_path", "accepted_path", "risk_distractor", "not_applicable"]
    rationale: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_coverage(self) -> ToolCoverageEntry:
        if self.coverage_kind == "not_applicable" and self.task_ids:
            raise ValueError("not-applicable tools cannot reference tasks")
        if self.coverage_kind != "not_applicable" and not self.task_ids:
            raise ValueError("covered tools must reference at least one task")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("coverage task ids must be unique")
        return self


class ToolCoverageMatrix(ContractModel):
    schema_version: Literal[1] = 1
    entries: list[ToolCoverageEntry] = Field(min_length=1)

    def validate_tasks(self, tasks: list[TaskManifest]) -> None:
        task_ids = {task.id for task in tasks}
        tools = [entry.tool for entry in self.entries]
        if len(set(tools)) != len(tools):
            raise ValueError("coverage matrix tool names must be unique")
        unknown = sorted({task_id for entry in self.entries for task_id in entry.task_ids} - task_ids)
        if unknown:
            raise ValueError(f"coverage matrix references unknown tasks: {unknown}")
        covered_families = {
            entry.capability_family for entry in self.entries if entry.coverage_kind != "not_applicable"
        }
        expected_families = set(CAPABILITY_FAMILIES)
        if covered_families != expected_families:
            raise ValueError("coverage matrix must cover every capability family")

    def validate_manifest(self, manifest: BenchmarkRunManifest) -> None:
        matrix_tools = {entry.tool for entry in self.entries}
        registered_tools = set(manifest.tool_resources)
        if matrix_tools != registered_tools:
            missing = sorted(registered_tools - matrix_tools)
            extra = sorted(matrix_tools - registered_tools)
            raise ValueError(f"coverage matrix/catalog mismatch: missing={missing}, extra={extra}")


class LocalizedTaskDefinition(ContractModel):
    id: str = Field(pattern=TASK_ID_RE.pattern)
    prompt: str = Field(min_length=1, max_length=4000)
    response_rubric: ResponseRubric = Field(default_factory=ResponseRubric)


class TaskPairDefinition(ContractModel):
    pair_id: str = Field(pattern=PAIR_ID_RE.pattern)
    common: dict[str, Any]
    locales: dict[TaskLocale, LocalizedTaskDefinition]

    @model_validator(mode="after")
    def validate_pair(self) -> TaskPairDefinition:
        if set(self.locales) != {"ko-KR", "en-US"}:
            raise ValueError("each task pair must define ko-KR and en-US")
        forbidden = {"id", "locale", "pair_id", "prompt", "response_rubric"} & set(self.common)
        if forbidden:
            raise ValueError(f"pair common fields cannot override localized coordinates: {sorted(forbidden)}")
        return self


class TaskCorpusDocument(ContractModel):
    schema_version: Literal[2] = 2
    pairs: list[TaskPairDefinition] = Field(min_length=1)


class ModelSpec(ContractModel):
    class_name: Literal["primary", "lightweight"]
    provider: Literal["openrouter"]
    model_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=200)
    base_url_env: str = Field(min_length=1, max_length=100)
    provider_key_env: str = Field(min_length=1, max_length=100)
    routing: ProviderRouting
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    input_cost_per_million_usd: float = Field(gt=0)
    output_cost_per_million_usd: float = Field(gt=0)

    @field_validator("base_url_env", "provider_key_env")
    @classmethod
    def validate_env_name(cls, value: str) -> str:
        if ENV_NAME_RE.fullmatch(value) is None:
            raise ValueError(f"invalid environment name: {value}")
        return value

    @model_validator(mode="after")
    def validate_openrouter_model(self) -> ModelSpec:
        expected_model = OPENROUTER_CLASSES[self.class_name]
        if self.model_id != expected_model or self.version != expected_model:
            raise ValueError(f"{self.class_name} model/version must be pinned to {expected_model}")
        if self.base_url_env != OPENROUTER_BASE_URL_ENV or self.provider_key_env != OPENROUTER_PROVIDER_KEY_ENV:
            raise ValueError("OpenRouter model credentials must use the declared environment names")
        expected_prices = OPENROUTER_PRICE_CEILINGS[self.model_id]
        if (self.input_cost_per_million_usd, self.output_cost_per_million_usd) != expected_prices:
            raise ValueError(f"price ceiling for {self.model_id} does not match the registered model contract")
        if set(self.settings) != {"temperature", "max_tokens"}:
            raise ValueError("model settings must contain only temperature and max_tokens")
        max_tokens = self.settings.get("max_tokens")
        if self.settings.get("temperature") != 0.0 or not isinstance(max_tokens, int):
            raise ValueError("model settings must pin temperature=0 and an integer max_tokens")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        return self


class Budget(ContractModel):
    max_model_requests: int = Field(gt=0)
    max_total_cost_usd: float = Field(gt=0)
    max_wall_seconds: int = Field(gt=0)
    request_timeout_seconds: int = Field(gt=0)
    max_requests_per_trial: int = Field(gt=0)
    max_cost_per_trial_usd: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_limits(self) -> Budget:
        if self.max_requests_per_trial > self.max_model_requests:
            raise ValueError("per-trial request cap cannot exceed global request cap")
        if self.request_timeout_seconds > self.max_wall_seconds:
            raise ValueError("request timeout cannot exceed the global wall limit")
        return self


class StatisticalProcedure(ContractModel):
    method: Literal["paired_task_mean_normal_approximation"]
    confidence: float = Field(default=0.95, gt=0, lt=1)
    noninferiority_margin: float = Field(default=0.03, ge=0, lt=1)
    repeated_trials_are_averaged_per_task: Literal[True] = True
    z_value: float = Field(default=1.644854, gt=0)


class ProviderSensitivity(ContractModel):
    maximum_arm_share_difference: float = Field(default=0.20, ge=0, le=1)
    downgrade_on_imbalance: Literal[True] = True


class BenchmarkRunManifest(ContractModel):
    schema_version: Literal[2] = 2
    name: str = Field(min_length=1, max_length=100)
    protocol_revision: Literal["2026-07-28"] = "2026-07-28"
    source_revision: str = Field(min_length=1, max_length=200)
    arms: list[ArmName] = Field(default_factory=default_arms)
    transports: list[Transport] = Field(default_factory=default_transports)
    models: list[ModelSpec] = Field(min_length=2)
    repeats: int = Field(ge=2, le=100)
    # Task cases stay serial within one mutable cell; the runner owns the
    # independent model/transport cell parallelism.
    max_concurrency: Literal[1] = 1
    category_minimums: dict[Category, int]
    suite_minimums: dict[Suite, int]
    capability_minimums: dict[CapabilityFamily, int]
    risk_minimums: dict[RiskHypothesis, int]
    statistical_procedure: StatisticalProcedure
    paired_order: Literal["counterbalanced_task_repeat"]
    paired_order_seed: str = Field(min_length=1, max_length=100)
    provider_sensitivity: ProviderSensitivity = Field(default_factory=ProviderSensitivity)
    budget: Budget
    operation_map: dict[str, list[str]]
    tool_resources: dict[str, ResourceType]
    credential_profiles: dict[str, str | None] = Field(default_factory=dict)
    fixture_scenario: str = Field(min_length=1, max_length=100)
    locales: list[TaskLocale] = Field(default_factory=default_locales)
    locale_counts: dict[TaskLocale, int] = Field(default_factory=default_locale_counts)
    pair_categories: dict[str, Category] = Field(default_factory=default_pair_categories)

    @field_validator("arms", "transports")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("list values must be unique")
        return values

    @field_validator("locales")
    @classmethod
    def validate_locales(cls, values: list[TaskLocale]) -> list[TaskLocale]:
        if len(set(values)) != len(values):
            raise ValueError("locales must be unique")
        return values

    @field_validator("locale_counts")
    @classmethod
    def validate_locale_counts(cls, values: dict[TaskLocale, int]) -> dict[TaskLocale, int]:
        if any(count <= 0 for count in values.values()):
            raise ValueError("locale counts must be positive")
        return values

    @field_validator("pair_categories")
    @classmethod
    def validate_pair_categories(cls, values: dict[str, Category]) -> dict[str, Category]:
        if any(PAIR_ID_RE.fullmatch(pair_id) is None for pair_id in values):
            raise ValueError("pair ids must be lowercase kebab-case and 3-64 characters")
        return values

    @field_validator("operation_map")
    @classmethod
    def validate_operation_map(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if not value:
            raise ValueError("operation_map cannot be empty")
        for logical, tools in value.items():
            if re.fullmatch(r"^[a-z][a-z0-9_]*$", logical) is None:
                raise ValueError(f"invalid logical operation: {logical}")
            if not tools or len(set(tools)) != len(tools):
                raise ValueError(f"operation map for {logical} must contain unique tool names")
            if any(re.fullmatch(r"^[A-Za-z][A-Za-z0-9_.-]*$", tool) is None for tool in tools):
                raise ValueError(f"invalid tool name in operation map for {logical}")
        return value

    @field_validator("credential_profiles")
    @classmethod
    def validate_credential_profiles(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        for profile, env_name in value.items():
            if re.fullmatch(r"^[a-z][a-z0-9_-]{0,31}$", profile) is None:
                raise ValueError(f"invalid credential profile: {profile}")
            if env_name is not None and ENV_NAME_RE.fullmatch(env_name) is None:
                raise ValueError(f"invalid credential environment name: {env_name}")
        return value

    @model_validator(mode="after")
    def validate_execution_contract(self) -> BenchmarkRunManifest:
        if set(self.arms) != {"baseline", "candidate"}:
            raise ValueError("paired benchmark requires exactly baseline and candidate arms")
        if set(self.transports) != {"http", "stdio"}:
            raise ValueError("benchmark must register both HTTP and stdio transports")
        if self.budget.max_total_cost_usd != 50.0:
            raise ValueError("the benchmark hard total cost cap must be $50")
        classes = {model.class_name for model in self.models}
        if classes != {"primary", "lightweight"}:
            raise ValueError("models must include one primary and one lightweight class")
        if len({model.class_name for model in self.models}) != len(self.models):
            raise ValueError("each model class must be registered exactly once")
        if len({model.settings["max_tokens"] for model in self.models}) != 1:
            raise ValueError("model output limits must be consistent across the registered models")
        if set(self.category_minimums) != {
            "single_operation",
            "ambiguous_action",
            "multi_step",
            "destructive_confirmation",
            "authorization",
            "invalid_input_recovery",
            "stdio_local",
        }:
            raise ValueError("category_minimums must register every benchmark category")
        if any(count < 2 for count in self.category_minimums.values()):
            raise ValueError("each category needs at least two independent tasks")
        profiles = set(self.credential_profiles)
        if "default" not in profiles:
            raise ValueError("credential_profiles must define default")
        if self.locales != ["ko-KR", "en-US"]:
            raise ValueError("the benchmark must register ko-KR and en-US locales in that order")
        if set(self.locale_counts) != set(self.locales):
            raise ValueError("locale counts must cover the registered locales")
        if set(self.suite_minimums) != {"capability", "tool_surface_risk"}:
            raise ValueError("suite minimums must register both benchmark suites")
        if set(self.capability_minimums) != set(CAPABILITY_FAMILIES):
            raise ValueError("capability minimums must register every capability family")
        if set(self.risk_minimums) != set(RISK_HYPOTHESES):
            raise ValueError("risk minimums must register every tool-surface risk")
        if any(value < 1 for value in (*self.suite_minimums.values(), *self.capability_minimums.values(), *self.risk_minimums.values())):
            raise ValueError("suite, capability, and risk minimums must be positive")
        registered_tools = {tool for tools in self.operation_map.values() for tool in tools}
        if set(self.tool_resources) != registered_tools:
            raise ValueError("tool_resources must classify every registered tool exactly once")
        duplicates = [tool for tool, count in Counter(tool for tools in self.operation_map.values() for tool in tools).items() if count > 1]
        if duplicates:
            raise ValueError(f"tools cannot map to multiple logical operations: {sorted(duplicates)}")
        return self

    def validate_tasks(self, tasks: list[TaskManifest]) -> None:
        if not tasks:
            raise ValueError("task corpus cannot be empty")
        ids = [task.id for task in tasks]
        duplicates = [task_id for task_id, count in Counter(ids).items() if count > 1]
        if duplicates:
            raise ValueError(f"duplicate task ids: {', '.join(sorted(duplicates))}")
        counts = Counter(task.category for task in tasks)
        missing = {
            category: minimum
            for category, minimum in self.category_minimums.items()
            if counts[category] < minimum
        }
        if missing:
            raise ValueError(f"task corpus is below category minimums: {missing}")
        suite_counts = Counter(task.suite for task in tasks)
        missing_suites = {
            suite: minimum for suite, minimum in self.suite_minimums.items() if suite_counts[suite] < minimum
        }
        if missing_suites:
            raise ValueError(f"task corpus is below suite minimums: {missing_suites}")
        capability_counts = Counter(family for task in tasks for family in task.capability_families)
        missing_capabilities = {
            family: minimum
            for family, minimum in self.capability_minimums.items()
            if capability_counts[family] < minimum
        }
        if missing_capabilities:
            raise ValueError(f"task corpus is below capability minimums: {missing_capabilities}")
        risk_counts = Counter(risk for task in tasks for risk in task.risk_hypotheses)
        missing_risks = {
            risk: minimum for risk, minimum in self.risk_minimums.items() if risk_counts[risk] < minimum
        }
        if missing_risks:
            raise ValueError(f"task corpus is below risk minimums: {missing_risks}")
        unknown_operations = {
            operation
            for task in tasks
            for operation in [
                *task.allowed_preparatory_operations,
                *task.allowed_material_operations,
                *task.allowed_first_operations,
                *task.forbidden_operations,
                *task.material_attempt_limits,
                *task.material_attempt_minimums,
                *task.required_operations,
                *(behavior_operation for behavior in task.accepted_behaviors for behavior_operation in behavior.required_operations),
                *task.stopping.allowed_follow_up_operations,
                *(mutation.operation for mutation in task.forbidden_mutations),
                *(item.logical_operation for item in task.expected_material_attempts),
            ]
            if operation not in self.operation_map and operation != "none"
        }
        if unknown_operations:
            raise ValueError(f"logical operations are missing from operation_map: {sorted(unknown_operations)}")
        unregistered_attempt_tools = sorted(
            f"{task.id}:{attempt.tool_name}"
            for task in tasks
            for attempt in task.expected_material_attempts
            if attempt.tool_name is not None and attempt.tool_name not in self.operation_map.get(attempt.logical_operation, [])
        )
        if unregistered_attempt_tools:
            raise ValueError(
                "expected material attempt tools must be registered for their logical operation: "
                f"{unregistered_attempt_tools}"
            )
        if any(task.fixture.scenario != self.fixture_scenario for task in tasks):
            raise ValueError("every task must use the registered fixture scenario")
        unregistered_coverage_tools = sorted(
            f"{task.id}:{tool}"
            for task in tasks
            for tool in task.coverage_tools
            if tool not in self.tool_resources
        )
        if unregistered_coverage_tools:
            raise ValueError(f"task coverage tools are not registered: {unregistered_coverage_tools}")
        self._validate_locale_pairs(tasks)
        source_blind_violations = source_blind_violations_for(tasks, self.operation_map)
        if source_blind_violations:
            details = "; ".join(f"{task_id}: {term}" for task_id, term in source_blind_violations)
            raise ValueError(f"task corpus contains source-aware tool hints: {details}")
        trials_per_arm = sum(len(task.fixture.transports) for task in tasks) * len(self.models) * self.repeats
        required_trials = trials_per_arm * len(self.arms)
        smoke_cells = len(self.models) * len(self.transports) * len(self.arms)
        if self.budget.max_model_requests < required_trials + smoke_cells:
            raise ValueError("max_model_requests is below the registered trial and smoke-gate count")
        reserved_cost = self.budget.max_cost_per_trial_usd * (required_trials + smoke_cells)
        if reserved_cost > self.budget.max_total_cost_usd:
            raise ValueError("the preregistered trial and smoke-gate cost reservations exceed max_total_cost_usd")

    def _validate_locale_pairs(self, tasks: list[TaskManifest]) -> None:
        if len(tasks) != sum(self.locale_counts.values()):
            raise ValueError("task corpus size must match the locale counts")
        locale_counts = Counter(task.locale for task in tasks)
        if dict(locale_counts) != self.locale_counts:
            raise ValueError("task corpus locale counts do not match the manifest")

        pairs: dict[str, list[TaskManifest]] = defaultdict(list)
        for task in tasks:
            pairs[task.pair_id].append(task)
        if set(pairs) != set(self.pair_categories):
            raise ValueError("task corpus semantic pair coverage does not match the manifest")

        category_counts = Counter(task.category for task in tasks)
        expected_category_counts = Counter(
            {category: count * 2 for category, count in Counter(self.pair_categories.values()).items()}
        )
        if dict(category_counts) != dict(expected_category_counts):
            raise ValueError("task corpus category counts do not match the registered pair coverage")

        for pair_id, expected_category in self.pair_categories.items():
            members = pairs[pair_id]
            if len(members) != 2 or {task.locale for task in members} != {"ko-KR", "en-US"}:
                raise ValueError(f"semantic pair {pair_id} must contain one task per locale")
            if any(task.category != expected_category for task in members):
                raise ValueError(f"semantic pair {pair_id} has a mismatched category")
            if self._pair_contract(members[0]) != self._pair_contract(members[1]):
                raise ValueError(f"semantic pair {pair_id} has mismatched task or response requirements")

    @staticmethod
    def _pair_contract(task: TaskManifest) -> dict[str, Any]:
        rubric = task.response_rubric
        return {
            "category": task.category,
            "suite": task.suite,
            "capability_families": sorted(task.capability_families),
            "risk_hypotheses": sorted(task.risk_hypotheses),
            "user_outcome": task.user_outcome,
            "accepted_behaviors": [item.model_dump(mode="json") for item in task.accepted_behaviors],
            "clarification": task.clarification.model_dump(mode="json"),
            "stopping": task.stopping.model_dump(mode="json"),
            "forbidden_mutations": [item.model_dump(mode="json") for item in task.forbidden_mutations],
            "allowed_resources": sorted(task.allowed_resources),
            "coverage_tools": sorted(task.coverage_tools),
            "fixture": task.fixture.model_dump(mode="json"),
            "allowed_preparatory_operations": sorted(task.allowed_preparatory_operations),
            "allowed_material_operations": sorted(task.allowed_material_operations),
            "allowed_first_operations": sorted(task.allowed_first_operations),
            "forbidden_operations": sorted(task.forbidden_operations),
            "required_attempted_operations": sorted(task.required_attempted_operations),
            "material_attempt_limits": dict(sorted(task.material_attempt_limits.items())),
            "material_attempt_minimums": dict(sorted(task.material_attempt_minimums.items())),
            "required_operations": sorted(task.required_operations),
            "expected_material_arguments": task.expected_material_arguments,
            "expected_material_outcomes": [item.model_dump(mode="json") for item in task.expected_material_outcomes],
            "expected_material_attempts": [item.model_dump(mode="json") for item in task.expected_material_attempts],
            "expected_result_bindings": [item.model_dump(mode="json") for item in task.expected_result_bindings],
            "expected_final_state": task.expected_final_state.model_dump(mode="json"),
            "response_requirements": {
                "require_non_empty": rubric.require_non_empty,
                "required_any_of_shape": sorted(len(group) for group in rubric.required_any_of),
                "forbidden_terms_count": len(rubric.forbidden_terms),
                "confirmation_terms_count": len(rubric.confirmation_terms),
                "clarification_terms_count": len(rubric.clarification_terms),
                "success_claim_terms_count": len(rubric.success_claim_terms),
            },
        }


class CatalogSnapshot(ContractModel):
    schema_version: Literal[1] = 1
    source: Literal["server_tools_list"] = "server_tools_list"
    protocol_revision: Literal["2026-07-28"] = "2026-07-28"
    transport: Transport
    source_revision: str = Field(min_length=1)
    artifact_version: str = Field(min_length=1)
    capability_profile: dict[str, JsonValue] = Field(default_factory=dict)
    tool_count: int = Field(ge=0)
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_token_estimate: int = Field(ge=0)
    token_estimator: Literal["utf8_bytes_div_4_ceiling"] = "utf8_bytes_div_4_ceiling"
    tools: list[dict[str, JsonValue]]
    harness_filtering: Literal[False] = False
    lazy_discovery: Literal[False] = False
    builtin_tools: Literal[0] = 0

    @model_validator(mode="after")
    def validate_catalog(self) -> CatalogSnapshot:
        if self.tool_count != len(self.tools):
            raise ValueError("tool_count must match tools length")
        expected_hash = hash_json(self.tools)
        if self.catalog_hash != expected_hash:
            raise ValueError("catalog_hash does not match tools")
        expected_tokens = token_estimate(self.tools)
        if self.catalog_token_estimate != expected_tokens:
            raise ValueError("catalog_token_estimate does not match tools")
        return self


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def token_estimate(value: Any) -> int:
    encoded_size = len(canonical_json(value).encode("utf-8"))
    return (encoded_size + 3) // 4


def source_blind_violations_for(
    tasks: list[TaskManifest], operation_map: dict[str, list[str]] | None = None
) -> list[tuple[str, str]]:
    forbidden = {tool.lower() for tools in (operation_map or {}).values() for tool in tools}
    violations: list[tuple[str, str]] = []
    for task in tasks:
        prompt = task.prompt.lower()
        match = TOOL_NAME_RE.search(task.prompt)
        if match:
            violations.append((task.id, match.group(0)))
            continue
        if "tools/list" in prompt or "tools/call" in prompt:
            violations.append((task.id, "MCP method name"))
            continue
        for tool_name in sorted(forbidden, key=len, reverse=True):
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(tool_name)}(?![A-Za-z0-9_])", prompt):
                violations.append((task.id, tool_name))
                break
    return violations


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def load_task_corpus(path: Path) -> list[TaskManifest]:
    raw = load_json(path)
    try:
        corpus = TaskCorpusDocument.model_validate(raw)
        tasks = [
            TaskManifest.model_validate(
                {
                    "schema_version": 2,
                    **pair.common,
                    "pair_id": pair.pair_id,
                    "locale": locale,
                    **localized.model_dump(mode="json"),
                }
            )
            for pair in corpus.pairs
            for locale, localized in pair.locales.items()
        ]
        if len({pair.pair_id for pair in corpus.pairs}) != len(corpus.pairs):
            raise ValueError("task corpus pair ids must be unique")
        return tasks
    except ValidationError as exc:
        raise ValueError(f"task corpus validation failed: {exc}") from exc


def load_tool_coverage(path: Path) -> ToolCoverageMatrix:
    raw = load_json(path)
    try:
        return ToolCoverageMatrix.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"tool coverage validation failed: {exc}") from exc


def load_run_manifest(path: Path) -> BenchmarkRunManifest:
    raw = load_json(path)
    try:
        return BenchmarkRunManifest.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"run manifest validation failed: {exc}") from exc
