"""Read-only C10 shadow comparison for one completed C9 migration run."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

import asyncpg

from app.exceptions import ConflictError, NotFoundError
from app.repositories.native_revision_migration_repo import (
    LegacyRevisionMapping,
    MigrationItem,
    MigrationRun,
    NativeRevisionMigrationRepository,
)
from app.services.legacy_revision_bridge import (
    LegacyActivitySemantics,
    LegacyInventory,
    LegacyInventoryDocument,
)
from app.services.native_revision_shadow_reader import (
    LegacyFixedRefShadowReader,
    NativeActivityEvidence,
    NativeRevisionShadowReader,
    ShadowReaderScopeError,
)


_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_PATH_TOKEN_RE = re.compile(r"([^.[\]]+)|\[(\d+)\]")
_OPERATIONS = ("get", "history", "diff", "activity")
_PATH_TO_RULE = {
    "get:$.current_commit": "BR-01",
    "history:$.entries[0].selector": "BR-01",
    "diff:$.commit": "BR-01",
    "activity:$.events[0].hash": "BR-01",
    "history:$.history_source": "BR-02",
    "diff:$.basis": "BR-03",
    "get:$.projection.revision": "BR-04",
    "history:$.entries[0].projection_revision": "BR-04",
    "activity:$.events[0].projection_revision": "BR-04",
    "get:$.content": "BR-05",
    "activity:$.events[0].author.display": "BR-06",
    "history:$.lineage_boundary": "BR-07",
}
_RULE_CLASSIFICATION = {
    "BR-01": "revision_token",
    "BR-02": "history_source",
    "BR-03": "diff_basis",
    "BR-04": "projection_revision",
    "BR-05": "formatting_only",
    "BR-06": "activity_display",
    "BR-07": "lineage_boundary",
}
_ACTIVITY_AUDIT_PROFILE = "akb-native-revision-p2-activity-audit/v1"
_EVIDENCE_BINDING_CANONICALIZATION = "utf8-json-sort-keys-no-whitespace-v1"
_EVIDENCE_BINDING_DOMAIN = "akb-native-revision-p2-w1-c10/evidence-binding/v1"
__all__ = (
    "LegacyFixedRefShadowReader",
    "NativeRevisionShadowReader",
    "NativeRevisionShadowComparator",
    "NativeRevisionShadowService",
    "ShadowComparisonError",
    "ShadowReaderScopeError",
    "ShadowRunIncompleteError",
)


class ShadowComparisonError(ConflictError):
    """A C10 comparison cannot establish approved semantic parity."""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = "native_revision_shadow_comparison_failed"


class ShadowRunIncompleteError(ShadowComparisonError):
    """A shadow comparison is only valid for a completed C9 run."""

    def __init__(self, run_id: uuid.UUID):
        super().__init__(f"native revision shadow comparison requires completed run: {run_id}")
        self.code = "native_revision_shadow_run_incomplete"


class MigrationReadRepository(Protocol):
    async def get_run(self, run_id: uuid.UUID) -> MigrationRun | None: ...

    async def list_items(self, run_id: uuid.UUID) -> list[MigrationItem]: ...

    async def exact_mapping(
        self,
        *,
        resource_id: uuid.UUID,
        legacy_git_oid: str,
    ) -> LegacyRevisionMapping | None: ...


class _NativeBinding:
    __slots__ = ("fixed_ref", "native_revision_id", "evidence_fact")

    def __init__(
        self,
        *,
        fixed_ref: str,
        native_revision_id: str,
        evidence_fact: dict[str, Any],
    ):
        self.fixed_ref = fixed_ref
        self.native_revision_id = native_revision_id
        self.evidence_fact = evidence_fact


class InventoryReader(Protocol):
    async def inventory_for_run(self, run: MigrationRun) -> LegacyInventory: ...


class ShadowReader(Protocol):
    async def get(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> Mapping[str, Any]: ...

    async def history(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> Mapping[str, Any]: ...

    async def diff(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> Mapping[str, Any]: ...

    async def activity(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> Mapping[str, Any]: ...

    async def activity_evidence(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> NativeActivityEvidence: ...


class NativeRevisionShadowComparator:
    """Compare legacy and candidate reads without opening a write path."""

    protocol_version = "akb-native-revision-p2-w1-c10/v4"

    def __init__(
        self,
        pool: asyncpg.Pool | None = None,
        *,
        repository: MigrationReadRepository | None = None,
        bridge: InventoryReader,
        legacy_reader: ShadowReader,
        candidate_reader: ShadowReader,
    ):
        if repository is None:
            if pool is None:
                raise ValueError("pool or a read-only migration repository is required")
            repository = NativeRevisionMigrationRepository(pool)
        self.repository = repository
        self.bridge = bridge
        self.legacy_reader = legacy_reader
        self.candidate_reader = candidate_reader

    async def compare_run(self, run_id: uuid.UUID | str) -> dict[str, Any]:
        parsed_run_id = self._run_id(run_id)
        run = await self.repository.get_run(parsed_run_id)
        if run is None:
            raise NotFoundError("Native revision migration run", str(parsed_run_id))
        if self._value(run, "status") != "complete":
            raise ShadowRunIncompleteError(parsed_run_id)
        run_facts = self._run_facts(run, label="completed comparison run")

        # The bridge is deliberately called before any candidate read.  Its
        # inventory digest check is the immutable C9 gate and its existing
        # MigrationInventoryDriftError is allowed to fail closed unchanged.
        inventory = await self.bridge.inventory_for_run(run)
        self._validate_inventory_binding(run, inventory)
        documents = self._documents(inventory)
        items = await self.repository.list_items(parsed_run_id)
        bindings = await self._native_bindings(run, documents, items)

        resources = []
        for document in sorted(documents, key=lambda item: str(item.resource_id)):
            resources.append(
                await self._compare_resource(
                    run,
                    inventory,
                    document,
                    bindings[document.resource_id],
                )
            )
        evidence_binding = self._evidence_binding(run_facts, bindings)

        receipt: dict[str, Any] = {
            "schema_version": 4,
            "protocol_version": self.protocol_version,
            "status": "passed",
            "passed": True,
            "claim_scope": "semantic_candidate_evidence",
            "write_authority": "legacy_only",
            "comparison_ref": "completed immutable mapping owners",
            "final_p2_coverage_claim": False,
            "cutover_claim": False,
            "scope": {
                "claim_scope": "semantic_candidate_evidence",
                "write_authority": "legacy_only",
                "comparison_ref": "completed immutable mapping owners",
                "final_coverage_gate": "P2 L1-L6 product-backed evidence before final coverage selection",
                "p3_fence_cutover_out_of_scope": True,
            },
            "run": {"status": "complete"},
            "resources": resources,
            "summary": {
                "resource_count": len(resources),
                "operation_count": len(resources) * len(_OPERATIONS),
                "mismatch_count": sum(
                    operation["mismatch_count"]
                    for resource in resources
                    for operation in resource["operations"].values()
                ),
                "unexplained_mismatch_count": 0,
                "classified_mismatches": self._classified_mismatches(resources),
                "raw_activity_audit_count": sum(
                    1
                    for resource in resources
                    if resource["operations"]["activity"].get("raw_activity_audit")
                    == {
                        "profile": _ACTIVITY_AUDIT_PROFILE,
                        "status": "passed",
                    }
                ),
                "used_rules": sorted(
                    {
                        mismatch["rule_id"]
                        for resource in resources
                        for operation in resource["operations"].values()
                        for mismatch in operation["classified_mismatches"]
                    }
                ),
            },
            "evidence_binding": evidence_binding,
        }
        receipt["receipt_digest"] = self._digest(receipt)
        return receipt

    async def compare(self, run_id: uuid.UUID | str) -> dict[str, Any]:
        """Short alias for the one service entry point."""
        return await self.compare_run(run_id)

    async def _compare_resource(
        self,
        run: MigrationRun,
        inventory: LegacyInventory,
        document: LegacyInventoryDocument,
        binding: _NativeBinding,
    ) -> dict[str, Any]:
        del run, inventory
        native_revision_id = binding.native_revision_id
        operations: dict[str, Any] = {}
        for operation in _OPERATIONS:
            selector = document.current_commit
            legacy = await self._read(
                self.legacy_reader,
                operation,
                document,
                selector=selector,
                fixed_ref=binding.fixed_ref,
            )
            candidate_selector = native_revision_id
            if operation == "activity":
                raw_candidate, activity_binding_fact = await self._read_candidate_activity(
                    document,
                    selector=native_revision_id,
                    fixed_ref=binding.fixed_ref,
                )
                binding.evidence_fact["activity_audit"] = activity_binding_fact
                candidate = self._project_candidate_activity(
                    legacy,
                    raw_candidate,
                    document,
                    native_revision_id,
                )
            else:
                raw_candidate = await self._read(
                    self.candidate_reader,
                    operation,
                    document,
                    selector=candidate_selector,
                    fixed_ref=binding.fixed_ref,
                )
                candidate = raw_candidate
            _, _, mismatches = self._compare_operation(
                operation,
                legacy,
                candidate,
                document,
                native_revision_id,
            )
            operation_result: dict[str, Any] = {
                "normalized_equal": True,
                "mismatch_count": len(mismatches),
                "mismatch_classes": sorted({mismatch["classification"] for mismatch in mismatches}),
                "classified_mismatches": self._count_operation_mismatches(mismatches),
            }
            if operation == "activity":
                operation_result["raw_activity_audit"] = {
                    "profile": _ACTIVITY_AUDIT_PROFILE,
                    "status": "passed",
                }
            operations[operation] = operation_result
        return {"operations": operations}

    async def _read_candidate_activity(
        self,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        reader = getattr(self.candidate_reader, "activity_evidence", None)
        if reader is None:
            raise ShadowComparisonError(
                "candidate reader cannot prove the raw native activity audit"
            )
        value = reader(document, selector=selector, fixed_ref=fixed_ref)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, NativeActivityEvidence):
            raise ShadowComparisonError(
                "candidate reader returned an invalid raw native activity audit"
            )
        envelope = self._jsonable(value.envelope)
        binding_fact = self._jsonable(value.binding_fact)
        if not isinstance(envelope, dict) or not isinstance(binding_fact, dict):
            raise ShadowComparisonError(
                "candidate raw native activity audit is not an object"
            )
        if binding_fact.get("profile") != _ACTIVITY_AUDIT_PROFILE:
            raise ShadowComparisonError(
                "candidate raw native activity audit profile is invalid"
            )
        return envelope, binding_fact

    async def _read(
        self,
        reader: ShadowReader,
        operation: str,
        document: LegacyInventoryDocument,
        *,
        selector: str,
        fixed_ref: str,
    ) -> dict[str, Any]:
        method = getattr(reader, operation, None)
        if method is None:
            raise ShadowComparisonError(f"shadow reader has no {operation} method")
        value = method(document, selector=selector, fixed_ref=fixed_ref)
        if inspect.isawaitable(value):
            value = await value
        result = self._jsonable(value)
        if not isinstance(result, dict):
            raise ShadowComparisonError(f"{operation} reader must return an envelope object")
        return result

    @classmethod
    def _project_candidate_activity(
        cls,
        legacy: dict[str, Any],
        candidate: dict[str, Any],
        document: LegacyInventoryDocument,
        native_revision_id: str,
    ) -> dict[str, Any]:
        legacy_events = legacy.get("events")
        candidate_events = candidate.get("events")
        if not isinstance(legacy_events, list) or len(legacy_events) != 1:
            raise ShadowComparisonError("activity legacy envelope must contain one fixed-ref event")
        if not isinstance(candidate_events, list) or len(candidate_events) != 1:
            raise ShadowComparisonError("activity candidate envelope must contain one current-head event")
        legacy_event = legacy_events[0]
        candidate_event = candidate_events[0]
        if not isinstance(legacy_event, dict) or not isinstance(candidate_event, dict):
            raise ShadowComparisonError("activity event must be an object")

        activity = document.activity
        cls._validate_legacy_activity(legacy_event, activity)
        projected = copy.deepcopy(candidate)
        event = copy.deepcopy(candidate_event)
        event["hash"] = native_revision_id
        event["projection_revision"] = native_revision_id
        for key in ("action", "subject", "summary"):
            if key in event or key in legacy_event:
                event[key] = getattr(activity, key)
        for key in ("path_from", "path_to"):
            if key in event or key in legacy_event:
                event[key] = getattr(activity, key)
        if "changed_paths" in event or "changed_paths" in legacy_event:
            event["changed_paths"] = [dict(path) for path in activity.changed_paths]
        if "files" in event or "files" in legacy_event:
            event["files"] = cls._activity_files(activity)
        if "actor" in event or "actor" in legacy_event:
            event["actor"] = activity.actor
        if "agent" in event or "agent" in legacy_event:
            event["agent"] = activity.actor

        legacy_author = legacy_event.get("author")
        candidate_author = event.get("author")
        if isinstance(legacy_author, dict):
            if legacy_author.get("id") != activity.actor:
                raise ShadowComparisonError("legacy activity actor is not bound to the inventory")
            if not isinstance(candidate_author, dict):
                raise ShadowComparisonError("candidate activity author shape differs")
            author = copy.deepcopy(candidate_author)
            author["id"] = activity.actor
            display = legacy_author.get("display")
            if not isinstance(display, str):
                raise ShadowComparisonError("legacy activity display is not a string")
            author["display"] = display.removesuffix(" (Git)")
            event["author"] = author
        elif isinstance(legacy_author, str):
            if legacy_author != activity.actor:
                raise ShadowComparisonError("activity actor is not bound to the inventory")
            event["author"] = activity.actor
        else:
            raise ShadowComparisonError("activity author shape is unsupported")
        projected["events"] = [event]
        return projected

    @staticmethod
    def _validate_legacy_activity(event: dict[str, Any], activity: LegacyActivitySemantics) -> None:
        if event.get("action") != activity.action:
            raise ShadowComparisonError("legacy activity action differs from the inventory")
        if event.get("subject") != activity.subject:
            raise ShadowComparisonError("legacy activity subject differs from the inventory")
        if event.get("summary") != activity.summary:
            raise ShadowComparisonError("legacy activity summary differs from the inventory")

    @staticmethod
    def _activity_files(activity: LegacyActivitySemantics) -> list[dict[str, str]]:
        result = []
        for change in activity.changed_paths:
            kind = change.get("change")
            label = "added" if kind == "create" else "deleted" if kind == "delete" else "modified"
            path = change.get("path_to") or change.get("path_from")
            if not isinstance(path, str):
                raise ShadowComparisonError("inventory activity path data is incomplete")
            result.append({"path": path, "change": label})
        return result

    @classmethod
    def _compare_operation(
        cls,
        operation: str,
        legacy: dict[str, Any],
        candidate: dict[str, Any],
        document: LegacyInventoryDocument,
        native_revision_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        differences = cls._leaf_diffs(legacy, candidate, operation)
        normalized_legacy = copy.deepcopy(legacy)
        normalized_candidate = copy.deepcopy(candidate)
        mismatches = []
        for path, left, right in differences:
            rule_id = _PATH_TO_RULE.get(f"{operation}:{path}")
            if rule_id is None:
                raise ShadowComparisonError(f"unapproved mismatch at {operation}:{path}")
            normalized_left, normalized_right = cls._normalize_pair(
                operation,
                path,
                left,
                right,
                rule_id,
                document,
                native_revision_id,
            )
            if normalized_left != normalized_right:
                raise ShadowComparisonError(
                    f"{rule_id} normalization does not establish parity at {operation}:{path}"
                )
            cls._set_path(normalized_legacy, path, normalized_left)
            cls._set_path(normalized_candidate, path, normalized_right)
            mismatches.append(
                {
                    "path": path,
                    "rule_id": rule_id,
                    "classification": _RULE_CLASSIFICATION[rule_id],
                }
            )
        if normalized_legacy != normalized_candidate:
            raise ShadowComparisonError(f"unapproved mismatch at {operation}: normalized envelopes differ")
        return normalized_legacy, normalized_candidate, mismatches

    @classmethod
    def _normalize_pair(
        cls,
        operation: str,
        path: str,
        legacy: Any,
        candidate: Any,
        rule_id: str,
        document: LegacyInventoryDocument,
        native_revision_id: str,
    ) -> tuple[Any, Any]:
        head = document.current_commit
        if rule_id in {"BR-01", "BR-04"}:
            if legacy != head or candidate != native_revision_id:
                raise ShadowComparisonError(f"{rule_id} values at {operation}:{path} are not C9-bound")
            return head, head
        if rule_id == "BR-02":
            if (legacy, candidate) != ("legacy-git-log", "fixed-ref-bridge"):
                raise ShadowComparisonError("BR-02 values are not the approved fixed-ref pair")
            return "fixed-ref-history", "fixed-ref-history"
        if rule_id == "BR-03":
            if (legacy, candidate) != ("git-parent", "fixed-ref-snapshot"):
                raise ShadowComparisonError("BR-03 values are not the approved snapshot pair")
            return "snapshot-diff", "snapshot-diff"
        if rule_id == "BR-05":
            if not isinstance(legacy, str) or not isinstance(candidate, str) or "\r" in candidate:
                raise ShadowComparisonError("BR-05 may normalize only CRLF formatting")
            body = document.body.decode("utf-8", errors="strict")
            left = legacy.replace("\r\n", "\n")
            if left != candidate or candidate != body:
                raise ShadowComparisonError("BR-05 formatting-only normalization changed content")
            return left, candidate
        if rule_id == "BR-06":
            if not isinstance(legacy, str) or not isinstance(candidate, str):
                raise ShadowComparisonError("BR-06 display values must be strings")
            if not legacy.endswith(" (Git)") or legacy.removesuffix(" (Git)") != candidate:
                raise ShadowComparisonError("BR-06 values are not the approved display decoration pair")
            return candidate, candidate
        if rule_id == "BR-07":
            boundary = cls._lineage_boundary(document)
            if (legacy, candidate) != ("legacy-document-start", boundary):
                raise ShadowComparisonError("BR-07 values are not the inventory boundary pair")
            return boundary, boundary
        raise ShadowComparisonError(f"unknown bridge rule {rule_id}")

    @staticmethod
    def _leaf_diffs(left: Any, right: Any, operation: str, path: str = "$") -> list[tuple[str, Any, Any]]:
        if isinstance(left, dict) or isinstance(right, dict):
            if not isinstance(left, dict) or not isinstance(right, dict) or set(left) != set(right):
                raise ShadowComparisonError(f"unapproved mismatch at {operation}:{path}")
            result: list[tuple[str, Any, Any]] = []
            for key in sorted(left):
                result.extend(NativeRevisionShadowComparator._leaf_diffs(left[key], right[key], operation, f"{path}.{key}"))
            return result
        if isinstance(left, list) or isinstance(right, list):
            if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
                raise ShadowComparisonError(f"unapproved mismatch at {operation}:{path}")
            result = []
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                result.extend(NativeRevisionShadowComparator._leaf_diffs(left_item, right_item, operation, f"{path}[{index}]"))
            return result
        return [] if left == right else [(path, left, right)]

    @staticmethod
    def _set_path(root: Any, path: str, value: Any) -> None:
        if not path.startswith("$."):
            raise ShadowComparisonError(f"cannot normalize invalid path {path}")
        tokens = []
        for key, index in _PATH_TOKEN_RE.findall(path[2:]):
            tokens.append(int(index) if index else key)
        current = root
        for token in tokens[:-1]:
            current = current[token]
        current[tokens[-1]] = value

    @classmethod
    def _run_facts(cls, run: Any, *, label: str) -> dict[str, str]:
        try:
            run_id = uuid.UUID(str(cls._value(run, "run_id")))
            namespace_id = uuid.UUID(str(cls._value(run, "namespace_id")))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ShadowComparisonError(f"{label} has invalid UUID facts") from exc
        fixed_git_oid = cls._value(run, "fixed_git_oid")
        inventory_digest = cls._value(run, "inventory_digest")
        coverage_version = cls._value(run, "coverage_version")
        status = cls._value(run, "status")
        if (
            not isinstance(fixed_git_oid, str)
            or _OID_RE.fullmatch(fixed_git_oid) is None
            or not isinstance(inventory_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", inventory_digest) is None
            or not isinstance(coverage_version, str)
            or not coverage_version
            or status != "complete"
        ):
            raise ShadowComparisonError(f"{label} has invalid immutable scope facts")
        return {
            "run_id": str(run_id),
            "namespace_id": str(namespace_id),
            "fixed_git_oid": fixed_git_oid,
            "inventory_digest": inventory_digest,
            "coverage_version": coverage_version,
            "status": status,
        }

    @classmethod
    def _mapping_evidence_fact(
        cls,
        *,
        run: Any,
        document: LegacyInventoryDocument,
        mapping: Any,
        owner_facts: dict[str, str],
        native_revision_id: str,
    ) -> dict[str, Any]:
        try:
            run_namespace = uuid.UUID(str(cls._value(run, "namespace_id")))
            mapping_namespace = uuid.UUID(str(cls._value(mapping, "namespace_id")))
            mapping_resource = uuid.UUID(str(cls._value(mapping, "resource_id")))
            mapping_owner = uuid.UUID(str(cls._value(mapping, "run_id")))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ShadowComparisonError("completed mapping has invalid UUID facts") from exc
        legacy_head_oid = cls._value(mapping, "legacy_git_oid")
        path_at_revision = cls._value(mapping, "path_at_revision")
        resolution = cls._value(mapping, "resolution")
        lineage_ordinal = cls._value(mapping, "lineage_ordinal")
        mapping_native_id = cls._value(mapping, "native_revision_id")
        mapping_fixed_ref = cls._value(mapping, "fixed_git_oid")
        if (
            mapping_namespace != run_namespace
            or mapping_resource != document.resource_id
            or legacy_head_oid != document.current_commit
            or not isinstance(legacy_head_oid, str)
            or _OID_RE.fullmatch(legacy_head_oid) is None
            or path_at_revision != document.current_path
            or not isinstance(path_at_revision, str)
            or not path_at_revision.strip()
            or resolution != "native"
            or not isinstance(mapping_native_id, str)
            or mapping_native_id != native_revision_id
            or _OID_RE.fullmatch(mapping_native_id) is None
            or not isinstance(lineage_ordinal, int)
            or isinstance(lineage_ordinal, bool)
            or lineage_ordinal != len(document.lineage) - 1
            or not isinstance(mapping_fixed_ref, str)
            or _OID_RE.fullmatch(mapping_fixed_ref) is None
            or mapping_owner != uuid.UUID(owner_facts["run_id"])
            or mapping_fixed_ref != owner_facts["fixed_git_oid"]
        ):
            raise ShadowComparisonError(
                "completed current native mapping differs from its immutable owner facts"
            )
        return {
            "resource_id": str(mapping_resource),
            "legacy_head_oid": legacy_head_oid,
            "path_at_revision": path_at_revision,
            "native_revision_id": mapping_native_id,
            "lineage_ordinal": lineage_ordinal,
            "resolution": resolution,
            "owner_run": owner_facts,
        }

    @classmethod
    def _evidence_binding(
        cls,
        run_facts: dict[str, str],
        bindings: dict[uuid.UUID, _NativeBinding],
    ) -> dict[str, Any]:
        mappings = sorted(
            (copy.deepcopy(binding.evidence_fact) for binding in bindings.values()),
            key=lambda fact: (
                fact["resource_id"],
                fact["legacy_head_oid"],
                fact["native_revision_id"],
            ),
        )
        if any("activity_audit" not in fact for fact in mappings):
            raise ShadowComparisonError(
                "evidence binding is missing an audited native activity fact"
            )
        comparison_run_commitment = cls._domain_digest(
            f"{_EVIDENCE_BINDING_DOMAIN}/comparison-run",
            run_facts,
        )
        mapping_commitments = sorted(
            cls._domain_digest(
                f"{_EVIDENCE_BINDING_DOMAIN}/mapping-owner-activity",
                fact,
            )
            for fact in mappings
        )
        redacted_preimage = {
            "comparison_run": comparison_run_commitment,
            "mapping_owner_activity": mapping_commitments,
        }
        commitment = cls._domain_digest(
            _EVIDENCE_BINDING_DOMAIN,
            redacted_preimage,
        )
        owner_run_ids = {fact["owner_run"]["run_id"] for fact in mappings}
        return {
            "scheme": "sha256",
            "canonicalization": _EVIDENCE_BINDING_CANONICALIZATION,
            "domain": _EVIDENCE_BINDING_DOMAIN,
            "commitment": commitment,
            "components": redacted_preimage,
            "mapping_count": len(mappings),
            "owner_run_count": len(owner_run_ids),
        }

    @staticmethod
    def _domain_digest(domain: str, value: Any) -> str:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(domain.encode("utf-8") + b"\0" + canonical).hexdigest()

    @classmethod
    def _validate_inventory_binding(cls, run: MigrationRun, inventory: LegacyInventory) -> None:
        cls._run_facts(run, label="completed comparison run")
        if (
            cls._value(run, "namespace_id") != cls._value(inventory, "namespace_id")
            or cls._value(run, "inventory_digest") != cls._value(inventory, "inventory_digest")
            or cls._value(run, "coverage_version") != cls._value(inventory, "coverage_version")
        ):
            raise ShadowComparisonError("C9 inventory digest does not match the completed run")
        if cls._value(run, "fixed_git_oid") != cls._value(inventory, "fixed_git_oid"):
            raise ShadowComparisonError("C9 inventory fixed_ref does not match the completed run")

    @staticmethod
    def _documents(inventory: LegacyInventory) -> tuple[LegacyInventoryDocument, ...]:
        documents = getattr(inventory, "documents", None)
        if not isinstance(documents, tuple):
            documents = tuple(documents or ())
        seen: set[uuid.UUID] = set()
        for document in documents:
            if document.resource_id in seen:
                raise ShadowComparisonError("C9 inventory contains duplicate resources")
            seen.add(document.resource_id)
        return documents

    async def _native_bindings(
        self,
        run: MigrationRun,
        documents: tuple[LegacyInventoryDocument, ...],
        items: list[MigrationItem],
    ) -> dict[uuid.UUID, _NativeBinding]:
        comparison_run_facts = self._run_facts(run, label="completed comparison run")
        documents_by_id = {document.resource_id: document for document in documents}
        items_by_id: dict[uuid.UUID, MigrationItem] = {}
        for item in items:
            resource_id = self._value(item, "legacy_document_id")
            if resource_id in items_by_id:
                raise ShadowComparisonError("C9 migration items contain duplicate resources")
            document = documents_by_id.get(resource_id)
            if document is None:
                raise ShadowComparisonError("C9 migration item is outside the frozen inventory")
            expected_binding = (
                self._value(run, "run_id"),
                self._value(run, "namespace_id"),
                document.resource_id,
                document.resource_id,
                document.current_path,
                document.current_commit,
                document.body_digest,
                document.byte_size,
                "complete",
            )
            observed_binding = (
                self._value(item, "run_id"),
                self._value(item, "namespace_id"),
                resource_id,
                self._value(item, "native_resource_id"),
                self._value(item, "captured_path"),
                self._value(item, "legacy_head_oid"),
                self._value(item, "body_digest"),
                self._value(item, "byte_size"),
                self._value(item, "status"),
            )
            if observed_binding != expected_binding:
                raise ShadowComparisonError(
                    "C9 migration item binding differs from the completed run inventory"
                )
            native_id = self._value(item, "native_head_revision_id")
            if not isinstance(native_id, str) or _OID_RE.fullmatch(native_id) is None:
                raise ShadowComparisonError(
                    "completed C9 item binding has no valid native head token"
                )
            items_by_id[resource_id] = item

        result: dict[uuid.UUID, _NativeBinding] = {}
        for document in documents:
            mapping = await self.repository.exact_mapping(
                resource_id=document.resource_id,
                legacy_git_oid=document.current_commit,
            )
            native_id = self._value(mapping, "native_revision_id") if mapping else None
            fixed_ref = self._value(mapping, "fixed_git_oid") if mapping else None
            expected_mapping = (
                self._value(run, "namespace_id"),
                document.resource_id,
                document.current_commit,
                document.current_path,
                "native",
                len(document.lineage) - 1,
            )
            observed_mapping = (
                self._value(mapping, "namespace_id") if mapping else None,
                self._value(mapping, "resource_id") if mapping else None,
                self._value(mapping, "legacy_git_oid") if mapping else None,
                self._value(mapping, "path_at_revision") if mapping else None,
                self._value(mapping, "resolution") if mapping else None,
                self._value(mapping, "lineage_ordinal") if mapping else None,
            )
            if (
                observed_mapping != expected_mapping
                or not isinstance(native_id, str)
                or _OID_RE.fullmatch(native_id) is None
                or not isinstance(fixed_ref, str)
                or _OID_RE.fullmatch(fixed_ref) is None
            ):
                raise ShadowComparisonError(
                    "completed immutable mapping differs from the frozen inventory"
                )

            owner_run_id = self._value(mapping, "run_id")
            try:
                owner_run_uuid = uuid.UUID(str(owner_run_id))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ShadowComparisonError(
                    "immutable mapping owner has an invalid run id"
                ) from exc
            owner = (
                run
                if owner_run_uuid == uuid.UUID(comparison_run_facts["run_id"])
                else await self.repository.get_run(owner_run_uuid)
            )
            if owner is None:
                raise ShadowComparisonError(
                    "immutable mapping owner is not a completed fixed-ref run"
                )
            owner_facts = (
                comparison_run_facts
                if owner is run
                else self._run_facts(owner, label="immutable mapping owner run")
            )
            if (
                owner_facts["status"] != "complete"
                or owner_facts["namespace_id"] != comparison_run_facts["namespace_id"]
                or owner_facts["fixed_git_oid"] != fixed_ref
            ):
                raise ShadowComparisonError(
                    "immutable mapping owner is not a completed fixed-ref run"
                )

            current_item = items_by_id.get(document.resource_id)
            if owner_run_uuid == uuid.UUID(comparison_run_facts["run_id"]):
                if (
                    current_item is None
                    or self._value(current_item, "native_head_revision_id") != native_id
                ):
                    raise ShadowComparisonError(
                        "current-run mapping has no matching completed migration item"
                    )
            elif current_item is not None:
                raise ShadowComparisonError(
                    "current-run item attempts to re-home an immutable mapping"
                )
            evidence_fact = self._mapping_evidence_fact(
                run=run,
                document=document,
                mapping=mapping,
                owner_facts=owner_facts,
                native_revision_id=native_id,
            )
            result[document.resource_id] = _NativeBinding(
                fixed_ref=fixed_ref,
                native_revision_id=native_id,
                evidence_fact=evidence_fact,
            )
        return result

    @staticmethod
    def _lineage_boundary(document: LegacyInventoryDocument) -> str:
        if not document.lineage:
            raise ShadowComparisonError("inventory document has no lineage boundary")
        return document.lineage[0].legacy_git_oid

    @staticmethod
    def _count_operation_mismatches(
        mismatches: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        counts: dict[tuple[str, str], int] = {}
        for mismatch in mismatches:
            key = (mismatch["rule_id"], mismatch["classification"])
            counts[key] = counts.get(key, 0) + 1
        return [
            {"rule_id": rule_id, "classification": classification, "count": count}
            for (rule_id, classification), count in sorted(counts.items())
        ]

    @staticmethod
    def _classified_mismatches(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[tuple[str, str, str], int] = {}
        for resource in resources:
            for operation, result in resource["operations"].items():
                for mismatch in result["classified_mismatches"]:
                    key = (operation, mismatch["rule_id"], mismatch["classification"])
                    counts[key] = counts.get(key, 0) + mismatch["count"]
        return [
            {
                "operation": operation,
                "rule_id": rule_id,
                "classification": classification,
                "count": count,
            }
            for (operation, rule_id, classification), count in sorted(counts.items())
        ]

    @staticmethod
    def _value(value: Any, name: str) -> Any:
        return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)

    @staticmethod
    def _run_id(value: uuid.UUID | str) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ShadowComparisonError(f"invalid C9 run id: {value}") from exc

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): cls._jsonable(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._jsonable(child) for child in value]
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="strict")
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return cls._jsonable(model_dump())
        raise ShadowComparisonError(f"shadow envelope contains non-JSON value: {type(value).__name__}")

    @staticmethod
    def _digest(value: dict[str, Any]) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


NativeRevisionShadowService = NativeRevisionShadowComparator
