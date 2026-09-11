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
CONTRACT_SCHEMA_VERSION = 1
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_BASE_URL_ENV = "MCP_BENCH_OPENROUTER_BASE_URL"
OPENROUTER_PROVIDER_KEY_ENV = "MCP_BENCH_OPENROUTER_API_KEY"
OPENROUTER_PRICES = {
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

    @field_validator("transports")
    @classmethod
    def unique_transports(cls, values: list[Transport]) -> list[Transport]:
        if not values or len(set(values)) != len(values):
            raise ValueError("fixture transports must contain at least one unique transport")
        return values


class ResponseRubric(ContractModel):
    required_any_of: list[list[str]] = Field(default_factory=list)
    forbidden_terms: list[str] = Field(default_factory=list)
    require_non_empty: bool = True
    confirmation_terms: list[str] = Field(default_factory=list)

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

    @field_validator("forbidden_terms", "confirmation_terms")
    @classmethod
    def validate_terms(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("rubric terms must be non-empty")
        normalized = [value.casefold() for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("rubric terms must be unique")
        return values


class ProviderRouting(ContractModel):
    order: list[Literal["parasail"]] = Field(min_length=1, max_length=1)
    allow_fallbacks: Literal[False] = False
    require_parameters: Literal[True] = True

    @field_validator("order")
    @classmethod
    def pin_parasail(cls, values: list[Literal["parasail"]]) -> list[Literal["parasail"]]:
        if values != ["parasail"]:
            raise ValueError("OpenRouter routing must pin the parasail upstream")
        return values

    def request_body(self, *, input_price: float, output_price: float) -> dict[str, JsonValue]:
        return {
            "provider": {
                "order": ["parasail"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "max_price": {"prompt": input_price, "completion": output_price},
            }
        }


class TaskManifest(ContractModel):
    schema_version: Literal[1] = 1
    id: str
    category: Category
    locale: TaskLocale
    pair_id: str
    prompt: str = Field(min_length=1, max_length=4000)
    fixture: FixtureContract
    allowed_first_operations: list[str] = Field(min_length=1)
    forbidden_operations: list[str] = Field(default_factory=list)
    required_operations: list[str] = Field(default_factory=list)
    expected_final_state: StateContract
    response_rubric: ResponseRubric = Field(default_factory=ResponseRubric)

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

    @field_validator("allowed_first_operations", "forbidden_operations", "required_operations")
    @classmethod
    def validate_operations(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"^[a-z][a-z0-9_]*$", value) for value in values):
            raise ValueError("logical operation names must be lowercase snake_case")
        if len(set(values)) != len(values):
            raise ValueError("logical operation names must be unique")
        return values

    @model_validator(mode="after")
    def validate_task_boundaries(self) -> TaskManifest:
        if self.category == "stdio_local" and "stdio" not in self.fixture.transports:
            raise ValueError("stdio_local tasks must run on stdio")
        if set(self.allowed_first_operations) & set(self.forbidden_operations):
            raise ValueError("an operation cannot be both allowed first and forbidden")
        if set(self.required_operations) & set(self.forbidden_operations):
            raise ValueError("a required operation cannot be forbidden")
        if self.category == "stdio_local" and set(self.required_operations) != {"file_upload", "image_upload"}:
            raise ValueError("stdio_local tasks must require both file and image operations")
        if self.category == "destructive_confirmation" and not self.response_rubric.confirmation_terms:
            raise ValueError("destructive confirmation tasks must declare confirmation terms")
        return self


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
        expected_prices = OPENROUTER_PRICES[self.model_id]
        if (self.input_cost_per_million_usd, self.output_cost_per_million_usd) != expected_prices:
            raise ValueError(f"pricing snapshot for {self.model_id} does not match the pinned Parasail prices")
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
    max_requests_per_trial: int = Field(gt=0)
    max_cost_per_trial_usd: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_limits(self) -> Budget:
        if self.max_requests_per_trial > self.max_model_requests:
            raise ValueError("per-trial request cap cannot exceed global request cap")
        return self


class StatisticalProcedure(ContractModel):
    method: Literal["paired_task_mean_normal_approximation"]
    confidence: float = Field(default=0.95, gt=0, lt=1)
    noninferiority_margin: float = Field(default=0.03, ge=0, lt=1)
    repeated_trials_are_averaged_per_task: Literal[True] = True
    z_value: float = Field(default=1.644854, gt=0)


class BenchmarkRunManifest(ContractModel):
    schema_version: Literal[1] = 1
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
    statistical_procedure: StatisticalProcedure
    budget: Budget
    operation_map: dict[str, list[str]]
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
        if self.locale_counts != EXPECTED_LOCALE_COUNTS:
            raise ValueError("the benchmark must register eight tasks for each locale")
        if self.pair_categories != EXPECTED_PAIR_CATEGORIES:
            raise ValueError("the benchmark must register the fixed semantic task pairs")
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
        unknown_operations = {
            operation
            for task in tasks
            for operation in [
                *task.allowed_first_operations,
                *task.forbidden_operations,
                *task.required_operations,
            ]
            if operation not in self.operation_map and operation != "none"
        }
        if unknown_operations:
            raise ValueError(f"logical operations are missing from operation_map: {sorted(unknown_operations)}")
        if any(task.fixture.scenario != self.fixture_scenario for task in tasks):
            raise ValueError("every task must use the registered fixture scenario")
        self._validate_locale_pairs(tasks)
        source_blind_violations = source_blind_violations_for(tasks, self.operation_map)
        if source_blind_violations:
            details = "; ".join(f"{task_id}: {term}" for task_id, term in source_blind_violations)
            raise ValueError(f"task corpus contains source-aware tool hints: {details}")
        trials_per_arm = sum(len(task.fixture.transports) for task in tasks) * len(self.models) * self.repeats
        if trials_per_arm != 180:
            raise ValueError("the benchmark must register exactly 180 paid trials per arm")
        required_trials = trials_per_arm * len(self.arms)
        smoke_cells = len(self.models) * len(self.transports)
        if self.budget.max_model_requests < required_trials + smoke_cells:
            raise ValueError("max_model_requests is below the registered trial and smoke-gate count")
        reserved_cost = self.budget.max_cost_per_trial_usd * (required_trials + smoke_cells)
        if reserved_cost > self.budget.max_total_cost_usd:
            raise ValueError("the preregistered trial and smoke-gate cost reservations exceed max_total_cost_usd")

    def _validate_locale_pairs(self, tasks: list[TaskManifest]) -> None:
        if len(tasks) != sum(self.locale_counts.values()):
            raise ValueError("task corpus must contain exactly 16 tasks")
        locale_counts = Counter(task.locale for task in tasks)
        if dict(locale_counts) != self.locale_counts:
            raise ValueError("task corpus must contain exactly eight ko-KR and eight en-US tasks")

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
            "fixture": task.fixture.model_dump(mode="json"),
            "allowed_first_operations": sorted(task.allowed_first_operations),
            "forbidden_operations": sorted(task.forbidden_operations),
            "required_operations": sorted(task.required_operations),
            "expected_final_state": task.expected_final_state.model_dump(mode="json"),
            "response_requirements": {
                "require_non_empty": rubric.require_non_empty,
                "required_any_of_shape": sorted(len(group) for group in rubric.required_any_of),
                "forbidden_terms_count": len(rubric.forbidden_terms),
                "confirmation_terms_count": len(rubric.confirmation_terms),
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
    if not isinstance(raw, list):
        raise ValueError("task corpus root must be an array")
    try:
        return [TaskManifest.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise ValueError(f"task corpus validation failed: {exc}") from exc


def load_run_manifest(path: Path) -> BenchmarkRunManifest:
    raw = load_json(path)
    try:
        return BenchmarkRunManifest.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"run manifest validation failed: {exc}") from exc
