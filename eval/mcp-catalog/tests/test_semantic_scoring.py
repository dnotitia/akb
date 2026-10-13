from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.execution import (
    ToolCallRecord,
    ToolCallRecorder,
    TrialOutcome,
    _registered_readonly_observation,
    _finalize_semantic_metrics,
    _captured_material_positions_by_stage,
    _material_attempt_positions_by_stage,
    _vault_skill_binding_digest,
    bind_tool_calls,
    capture_result_fields_for_task,
    _resolved_material_stage_calls,
    response_matches_rubric,
)
from mcp_catalog.runtime import StateObservation
from mcp_catalog.state import evaluate_state_contract
from paired_artifact_factory import _state_observations


ROOT = Path(__file__).parents[1]


def _tasks():
    return {task.id: task for task in load_task_corpus(ROOT / "corpus" / "tasks.json")}


def _record(order: int, raw: dict[str, object]) -> ToolCallRecord:
    arguments = dict(raw.get("arguments", {}))
    return ToolCallRecord(
        order=order,
        tool_name=str(raw["tool_name"]),
        logical_operation=str(raw["logical_operation"]),
        resource_type=raw.get("resource_type", "unknown"),
        operation_kind=raw.get("operation_kind", "unknown"),
        raw_model_args=arguments,
        server_args=arguments,
        effective_server_args=arguments,
        raw_args_valid=True,
        server_args_equal_raw=True,
        transport_succeeded=True,
        server_succeeded=bool(raw.get("succeeded", True)),
        server_error_code=raw.get("error_code"),
        stage_id=raw.get("stage_id"),
        vault_skill_binding_sha256=raw.get(
            "vault_skill_binding_sha256",
            _vault_skill_binding_digest(str(raw["tool_name"]), arguments, str(raw.get("ack_token") or raw.get("retry_ack")))
            if raw.get("ack_token") or raw.get("retry_ack")
            else None,
        ),
        vault_skill_ack=raw.get("ack_token"),
        vault_skill_retry_ack=raw.get("retry_ack"),
    )


def _score(
    task_id: str,
    final_answer: str,
    calls: list[ToolCallRecord],
    before: object,
    after: object,
) -> TrialOutcome:
    task = _tasks()[task_id]
    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="fixture-model",
        transport="http",
        final_answer_text=final_answer,
        first_logical_operation=calls[0].logical_operation if calls else "none",
        tool_calls=calls,
    )
    outcome.finalize(
        task,
        StateObservation(True, 200, before),
        StateObservation(True, 200, after),
    )
    return outcome


def _score_browse_state(task_id: str, items: list[dict[str, object]], calls: list[ToolCallRecord] | None = None) -> TrialOutcome:
    task = _tasks()[task_id]
    observations = task.expected_final_state.observation_sets
    before = [
        StateObservation(
            True,
            item.resolved_before_expected_status if item.check_before else item.probe.expected_status,
            {"detail": "Vault not found"},
        )
        for item in observations
    ]
    after = [
        StateObservation(True, item.probe.expected_status, {"items": items})
        for item in observations
    ]
    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="fixture-model",
        transport="http",
        final_answer_text="모든 단계가 완료됐습니다. 문서 내용을 확인했습니다.",
        first_logical_operation=calls[0].logical_operation if calls else "none",
        tool_calls=calls or [],
    )
    outcome.finalize(task, before, after)
    return outcome


def _knowledge_workflow_calls(content: str = "A short note for the team.") -> list[ToolCallRecord]:
    vault = "catalog-bench-knowledge"
    uri = "akb://catalog-bench-knowledge/coll/notes/doc/quick-update.md"
    calls = [
        _record(1, {
            "tool_name": "akb_create_vault",
            "logical_operation": "create",
            "resource_type": "vault",
            "stage_id": "create_vault_1",
            "arguments": {"name": vault},
        }),
        _record(2, {
            "tool_name": "akb_create_collection",
            "logical_operation": "create",
            "resource_type": "collection",
            "stage_id": "create_collection_1",
            "arguments": {"vault": vault, "path": "notes"},
        }),
        _record(3, {
            "tool_name": "akb_put",
            "logical_operation": "create",
            "resource_type": "document",
            "stage_id": "create_document_1",
            "arguments": {
                "vault": vault,
                "collection": "notes",
                "title": "quick-update",
                "content": content,
            },
        }),
        _record(4, {
            "tool_name": "akb_get",
            "logical_operation": "read",
            "resource_type": "document",
            "stage_id": "read_document_1",
            "arguments": {"uri": uri},
        }),
        _record(5, {
            "tool_name": "akb_provenance",
            "logical_operation": "read",
            "resource_type": "document",
            "stage_id": "read_document_2",
            "arguments": {"uri": uri},
        }),
    ]
    calls[2].result_fields["uri"] = uri
    return calls


_BROWSE_DOCUMENT_CASES = (
    (
        "knowledge-workflow-ko",
        {
            # BrowseItem from the preserved baseline primary/HTTP trace.
            "name": "quick-update",
            "path": "notes/quick-update.md",
            "type": "document",
            "uri": "akb://catalog-bench-knowledge/coll/notes/doc/quick-update.md",
        },
    ),
    (
        "overlapping-document-ko",
        {
            "name": "status-note",
            "path": "status-note.md",
            "type": "document",
            "uri": "akb://catalog-bench-overlap/doc/status-note.md",
        },
    ),
    (
        "grep-write-ko",
        {
            "name": "grep-target",
            "path": "grep-target.md",
            "type": "document",
            "uri": "akb://catalog-bench-grep/doc/grep-target.md",
        },
    ),
)


def _same_title_decoys(item: dict[str, object]) -> tuple[dict[str, object], ...]:
    path = str(item["path"])
    uri = str(item["uri"])
    filename = path.rsplit("/", 1)[-1]
    parent_path = path.rsplit("/", 1)[0] if "/" in path else ""
    scheme, _, authority_and_path = uri.partition("://")
    authority, _, uri_path = authority_and_path.partition("/")
    return (
        {
            **item,
            "path": f"{parent_path + '/' if parent_path else ''}other-document.md",
            "uri": f"{uri.rsplit('/', 1)[0]}/other-document.md",
        },
        {
            **item,
            "path": f"archive/{filename}",
            "uri": f"{scheme}://{authority}/coll/archive/doc/{filename}",
        },
        {**item, "uri": f"{scheme}://another-vault/{uri_path}"},
        {**item, "name": "different-title"},
        {**item, "type": "file"},
    )


@pytest.mark.parametrize(
    ("task_id", "item"),
    _BROWSE_DOCUMENT_CASES,
)
def test_document_browse_state_accepts_the_public_title_and_canonical_identity(
    task_id: str,
    item: dict[str, object],
) -> None:
    outcome = _score_browse_state(task_id, [item])

    assert outcome.state_contract_passed is True


@pytest.mark.parametrize(("task_id", "item"), _BROWSE_DOCUMENT_CASES)
def test_document_browse_state_rejects_same_title_at_a_different_identity(
    task_id: str,
    item: dict[str, object],
) -> None:
    assert _score_browse_state(task_id, [item]).state_contract_passed is True
    decoys = _same_title_decoys(item)
    for decoy in decoys:
        assert _score_browse_state(task_id, [decoy]).state_contract_passed is False


def test_document_delete_state_requires_absence_by_title_after_a_collection_move() -> None:
    still_present = {
        "name": "revision-note",
        "path": "moved/revision-note.md",
        "type": "document",
        "uri": "akb://catalog-bench-revisions/coll/moved/doc/revision-note.md",
    }

    assert _score_browse_state("document-revisions-ko", []).state_contract_passed is True
    assert _score_browse_state("document-revisions-ko", [still_present]).state_contract_passed is False


def test_knowledge_workflow_still_rejects_wrong_content_and_missing_document_creation() -> None:
    task = _tasks()["knowledge-workflow-ko"]
    document = {
        "name": "quick-update",
        "path": "notes/quick-update.md",
        "type": "document",
        "uri": "akb://catalog-bench-knowledge/coll/notes/doc/quick-update.md",
    }
    valid_calls = _knowledge_workflow_calls()
    valid = _score_browse_state(task.id, [document], valid_calls)
    wrong_content = _knowledge_workflow_calls("A different body.")
    wrong = _score_browse_state(task.id, [document], wrong_content)
    missing_document = [call for call in _knowledge_workflow_calls() if call.stage_id != "create_document_1"]
    missing = _score_browse_state(task.id, [], missing_document)

    assert valid.state_contract_passed is True
    assert valid.tool_outcome_match is True
    assert valid.required_attempts_completed is True
    assert wrong.state_contract_passed is True
    assert wrong.tool_outcome_match is False
    assert wrong.required_attempts_completed is False
    assert wrong.success is False
    assert missing.state_contract_passed is False
    assert missing.tool_outcome_match is False
    assert missing.required_attempts_completed is False
    assert missing.success is False


def test_tracked_synthetic_and_real_shaped_traces_keep_semantics_separate() -> None:
    document = json.loads((ROOT / "tests" / "fixtures" / "semantic-traces.json").read_text())

    for case in document["cases"]:
        calls = [_record(order, raw) for order, raw in enumerate(case["calls"], 1)]
        outcome = _score(case["task_id"], case["final_answer"], calls, case["before"], case["after"])
        for field, expected in case["expected"].items():
            assert getattr(outcome, field) == expected, f"{case['id']}:{field}"


def test_exhaustive_inspection_near_miss_requires_every_match() -> None:
    calls = [
        _record(
            1,
            {
                "tool_name": "akb_list_vaults",
                "logical_operation": "list",
                "resource_type": "vault",
                "arguments": {},
            },
        )
    ]
    calls.extend(
        _record(
            index + 2,
            {
                "tool_name": "akb_vault_info",
                "logical_operation": "read",
                "resource_type": "vault",
                "arguments": {"name": f"target-{index}"},
            },
        )
        for index in range(12)
    )
    state = {"vaults": [{"name": f"target-{index}"} for index in range(13)]}

    near_miss = _score(
        "exhaustive-inspection-en",
        "Inspected the target vaults and prepared a summary table.",
        calls,
        state,
        state,
    )
    complete = _score(
        "exhaustive-inspection-en",
        "Inspected every target vault and prepared a summary table.",
        [
            *calls,
            _record(
                14,
                {
                    "tool_name": "akb_vault_info",
                    "logical_operation": "read",
                    "resource_type": "vault",
                    "arguments": {"name": "target-12"},
                },
            ),
        ],
        state,
        state,
    )

    assert near_miss.user_outcome_completed is False
    assert near_miss.success is False
    assert complete.user_outcome_completed is True
    assert complete.success is True


def test_nonexistent_tool_and_fabricated_success_are_counted_without_safety_conflation() -> None:
    state = {"vaults": []}
    nonexistent = _record(
        1,
        {
            "tool_name": "akb_magic_vault",
            "logical_operation": "unknown",
            "resource_type": "unknown",
            "arguments": {},
            "succeeded": False,
        },
    ).model_copy(update={"tool_exists": False})
    hallucinated = _score(
        "create-vault-en",
        "Created successfully at akb://fabricated/vault.",
        [nonexistent],
        state,
        state,
    )

    assert hallucinated.nonexistent_tool_attempts == 1
    assert hallucinated.fabricated_resource_claims == 1
    assert hallucinated.unsupported_success_claims == 1
    assert hallucinated.safety is True
    assert hallucinated.success is False
    assert hallucinated.semantic_reasons == [
        "nonexistent_tool_attempt:1",
        "fabricated_resource_claim:1",
        "unsupported_success_claim:1",
    ]


def test_collection_relation_oracle_requires_canonical_paths_bodies_and_revision() -> None:
    task = _tasks()["collection-relation-lifecycle-en"]
    contract = task.expected_final_state
    after_by_path = {
        "/api/v1/browse/catalog-bench-relations?depth=-1": {
            "items": [
                {"type": "collection", "path": "notes"},
                {"type": "document", "path": "notes/left.md"},
                {"type": "document", "path": "notes/right.md"},
            ]
        },
        "/api/v1/documents/catalog-bench-relations/notes/left.md": {
            "content": "left",
            "current_commit": "a" * 40,
        },
        "/api/v1/documents/catalog-bench-relations/notes/right.md": {
            "content": "right",
            "current_commit": "b" * 40,
        },
    }
    before = [
        StateObservation(True, item.resolved_before_expected_status, {})
        for item in contract.observation_sets
    ]
    after = [
        StateObservation(
            True,
            item.probe.expected_status,
            after_by_path.get(item.probe.path, {"relations": []}),
        )
        for item in contract.observation_sets
    ]

    passed, _checks = evaluate_state_contract(contract, before, after)

    assert passed is True

    near_miss = [*after]
    left_index = next(
        index
        for index, item in enumerate(contract.observation_sets)
        if item.probe.path.endswith("/notes/left.md")
    )
    near_miss[left_index] = StateObservation(
        True,
        contract.observation_sets[left_index].probe.expected_status,
        {"content": "left-ish", "current_commit": "a" * 40},
    )
    near_miss_passed, _near_miss_checks = evaluate_state_contract(contract, before, near_miss)

    assert near_miss_passed is False


def _relation_lifecycle_trace(
    task,
    *,
    wrong_document: bool = False,
    wrong_relation: bool = False,
    wrong_collection: bool = False,
    browse_pair: bool = False,
    wrong_browse_vault: bool = False,
    wrong_browse_uri: bool = False,
    browse_uri: bool = False,
    browse_root: bool = False,
    root_browse_has_collection: bool = True,
    reverse_verifications: bool = False,
    omit_stage: str | None = None,
    extra_browse_after_unlink: bool = True,
    unlink_before_prior_read: bool = False,
):
    left_uri = "akb://catalog-bench-relations/coll/notes/doc/left.md"
    right_uri = "akb://catalog-bench-relations/coll/notes/doc/right.md"
    source_by_stage = {
        "create_document_1": left_uri,
        "create_document_2": right_uri,
    }
    tool_by_stage = {
        "create_vault_1": "akb_create_vault",
        "create_collection_1": "akb_create_collection",
        "create_document_1": "akb_put",
        "create_document_2": "akb_put",
        "update_relation_1": "akb_link",
        "read_relation_1": "akb_relations",
        "delete_relation_1": "akb_unlink",
        "verify_left_document": "akb_get",
        "verify_right_document": "akb_get",
        "verify_collection": "akb_browse",
        "verify_unlinked_relationships": "akb_relations",
    }
    skipped_content_reads = {"verify_left_document", "verify_right_document"}
    calls = []
    attempts = list(task.expected_material_attempts)
    if unlink_before_prior_read:
        read_index = next(index for index, attempt in enumerate(attempts) if attempt.stage_id == "read_relation_1")
        unlink_index = next(index for index, attempt in enumerate(attempts) if attempt.stage_id == "delete_relation_1")
        attempts[read_index], attempts[unlink_index] = attempts[unlink_index], attempts[read_index]
    if reverse_verifications:
        last_mutation = max(
            index
            for index, attempt in enumerate(attempts)
            if attempt.logical_operation not in {"read", "list", "search"}
        )
        attempts = [*attempts[: last_mutation + 1], *reversed(attempts[last_mutation + 1 :])]
    for attempt in attempts:
        if attempt.stage_id == omit_stage:
            continue
        if attempt.stage_id in skipped_content_reads:
            continue
        arguments = dict(attempt.arguments)
        for binding in task.expected_result_bindings:
            if binding.target_stage == attempt.stage_id:
                if browse_pair and binding.relation == "collection_uri":
                    continue
                value = source_by_stage[binding.source_stage]
                if binding.relation == "collection_uri":
                    value = value.split("/doc/", 1)[0]
                arguments[binding.target_argument] = value
        if attempt.stage_id == "update_relation_1":
            if wrong_document:
                arguments["source"] = "akb://catalog-bench-relations/coll/notes/doc/unrelated.md"
            if wrong_relation:
                arguments["relation"] = "contains"
        if attempt.stage_id == "verify_collection" and wrong_collection:
            arguments["collection" if browse_pair else "uri"] = (
                "unrelated" if browse_pair else "akb://catalog-bench-relations/coll/unrelated"
            )
        if attempt.stage_id == "verify_collection" and browse_pair:
            arguments.update({"action": "browse", "vault": "catalog-bench-relations", "collection": "notes"})
            if browse_uri:
                arguments["uri"] = "akb://catalog-bench-relations/coll/notes"
            if wrong_collection:
                arguments["collection"] = "unrelated"
            if wrong_browse_vault:
                arguments["vault"] = "other-vault"
        if attempt.stage_id == "verify_collection" and browse_root:
            arguments = {
                "action": "browse",
                "depth": -1,
                "uri": "akb://catalog-bench-relations",
            }
        if attempt.stage_id == "verify_collection" and wrong_browse_uri:
            arguments["uri"] = "akb://catalog-bench-relations/coll/unrelated"
        result_preview = None
        if attempt.stage_id == "verify_collection" and browse_root:
            collection_uri = (
                "akb://catalog-bench-relations/coll/notes"
                if root_browse_has_collection
                else "akb://catalog-bench-relations/coll/unrelated"
            )
            result_preview = json.dumps(
                {"items": [{"type": "collection", "uri": collection_uri, "name": collection_uri.rsplit("/", 1)[-1]}]},
                separators=(",", ":"),
            )
        result_fields = {"uri": source_by_stage[attempt.stage_id]} if attempt.stage_id in source_by_stage else {}
        calls.append(
            ToolCallRecord(
                order=len(calls) + 1,
                tool_name=(
                    "akb_discover"
                    if attempt.stage_id == "verify_collection" and (browse_pair or browse_root)
                    else tool_by_stage[attempt.stage_id]
                ),
                canonical_operation=(
                    "akb_discover"
                    if attempt.stage_id == "verify_collection" and (browse_pair or browse_root)
                    else tool_by_stage[attempt.stage_id]
                ),
                logical_operation=attempt.logical_operation,
                resource_type=attempt.resource_type,
                stage_id=attempt.stage_id,
                raw_model_args=arguments,
                server_args=arguments,
                effective_server_args=arguments,
                raw_args_valid=True,
                server_args_equal_raw=True,
                transport_succeeded=True,
                server_succeeded=True,
                result_fields=result_fields,
                result_preview=result_preview,
            )
        )
        if attempt.stage_id == "delete_relation_1" and extra_browse_after_unlink:
            browse_arguments = {
                "uri": "akb://catalog-bench-relations/coll/notes",
                "depth": -1,
            }
            calls.append(
                ToolCallRecord(
                    order=len(calls) + 1,
                    tool_name="akb_browse",
                    canonical_operation="akb_browse",
                    logical_operation="list",
                    resource_type="document",
                    raw_model_args=browse_arguments,
                    server_args=browse_arguments,
                    effective_server_args=browse_arguments,
                    raw_args_valid=True,
                    server_args_equal_raw=True,
                    transport_succeeded=True,
                    server_succeeded=True,
                )
            )

    before = []
    after = []
    for observation in task.expected_final_state.observation_sets:
        before.append(
            StateObservation(
                True,
                observation.resolved_before_expected_status if observation.check_before else None,
                {},
            )
        )
        path = observation.probe.path
        if "/browse/" in path:
            payload = {
                "items": [
                    {"type": "collection", "path": "notes"},
                    {"type": "document", "path": "notes/left.md"},
                    {"type": "document", "path": "notes/right.md"},
                ]
            }
        elif path.endswith("/notes/left.md"):
            payload = {"content": "left", "current_commit": "a" * 40}
        elif path.endswith("/notes/right.md"):
            payload = {"content": "right", "current_commit": "b" * 40}
        else:
            payload = {
                "relations": (
                    [{"source": left_uri, "target": right_uri, "relation": "contains"}]
                    if wrong_relation
                    else []
                )
            }
        after.append(StateObservation(True, observation.probe.expected_status, payload))

    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="fixture-model",
        transport="http",
        final_answer_text="Both documents and the collection remain, and the relation is absent.",
        first_logical_operation="create",
        tool_calls=calls,
    )
    outcome.finalize(task, before, after)
    return outcome


def test_relation_workflow_accepts_alternate_public_reads_and_rejects_wrong_edges() -> None:
    task = _tasks()["collection-relation-lifecycle-en"]

    complete = _relation_lifecycle_trace(task)
    wrong_document = _relation_lifecycle_trace(task, wrong_document=True)
    wrong_relation = _relation_lifecycle_trace(task, wrong_relation=True)
    wrong_collection = _relation_lifecycle_trace(task, wrong_collection=True, extra_browse_after_unlink=False)
    browse_pair = _relation_lifecycle_trace(task, browse_pair=True, extra_browse_after_unlink=False)
    browse_root = _relation_lifecycle_trace(task, browse_root=True, extra_browse_after_unlink=False)
    browse_root_missing_collection = _relation_lifecycle_trace(
        task,
        browse_root=True,
        root_browse_has_collection=False,
        extra_browse_after_unlink=False,
    )
    reverse_verifications = _relation_lifecycle_trace(
        task,
        reverse_verifications=True,
        extra_browse_after_unlink=False,
    )
    missing_unlinked_check = _relation_lifecycle_trace(
        task,
        reverse_verifications=True,
        omit_stage="verify_unlinked_relationships",
        extra_browse_after_unlink=False,
    )
    out_of_order_mutation = _relation_lifecycle_trace(
        task,
        unlink_before_prior_read=True,
        extra_browse_after_unlink=False,
    )
    wrong_pair_vault = _relation_lifecycle_trace(
        task,
        browse_pair=True,
        wrong_browse_vault=True,
        extra_browse_after_unlink=False,
    )
    wrong_pair_collection = _relation_lifecycle_trace(
        task,
        browse_pair=True,
        wrong_collection=True,
        extra_browse_after_unlink=False,
    )
    conflicting_uri_vault = _relation_lifecycle_trace(
        task,
        browse_pair=True,
        browse_uri=True,
        wrong_browse_vault=True,
        extra_browse_after_unlink=False,
    )
    wrong_browse_uri = _relation_lifecycle_trace(
        task,
        wrong_browse_uri=True,
        extra_browse_after_unlink=False,
    )

    assert complete.state_contract_passed is True
    assert complete.tool_outcome_match is True
    assert complete.multi_step_ordering is True
    assert complete.result_binding_accuracy is True
    assert complete.trial_error is False
    assert complete.success is True
    assert wrong_document.result_binding_accuracy is False
    assert wrong_document.success is False
    assert wrong_relation.state_contract_passed is False
    assert wrong_relation.success is False
    assert wrong_collection.result_binding_accuracy is False
    assert wrong_collection.success is False
    assert browse_pair.result_binding_accuracy is True
    assert browse_pair.multi_step_ordering is True
    assert browse_pair.success is True
    assert browse_root.result_binding_accuracy is True
    assert browse_root.success is True
    assert browse_root_missing_collection.result_binding_accuracy is False
    assert browse_root_missing_collection.success is False
    assert reverse_verifications.multi_step_ordering is True
    assert reverse_verifications.result_binding_accuracy is True
    assert reverse_verifications.success is True
    assert missing_unlinked_check.multi_step_ordering is False
    assert missing_unlinked_check.result_binding_accuracy is False
    assert missing_unlinked_check.success is False
    assert out_of_order_mutation.multi_step_ordering is False
    assert out_of_order_mutation.success is False
    assert wrong_pair_vault.result_binding_accuracy is False
    assert wrong_pair_collection.result_binding_accuracy is False
    assert conflicting_uri_vault.result_binding_accuracy is False
    assert wrong_browse_uri.result_binding_accuracy is False


def test_publication_contract_accepts_only_registered_read_observations() -> None:
    task = _tasks()["table-publication-ko"]
    tool_by_stage = {
        "create_vault_1": "akb_create_vault",
        "create_table_1": "akb_create_table",
        "search_table_1": "akb_sql",
        "create_publication_1": "akb_publish",
        "list_publication_1": "akb_publications",
        "read_publication_1": "akb_publication_snapshot",
        "list_publication_2": "akb_publications",
        "delete_publication_1": "akb_unpublish",
    }
    calls = [
        ToolCallRecord(
            order=index,
            tool_name=tool_by_stage[attempt.stage_id],
            canonical_operation=tool_by_stage[attempt.stage_id],
            logical_operation=attempt.logical_operation,
            resource_type=attempt.resource_type,
            operation_kind="material",
            stage_id=attempt.stage_id,
            raw_model_args=dict(attempt.arguments),
            server_args=dict(attempt.arguments),
            effective_server_args=dict(attempt.arguments),
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=True,
        )
        for index, attempt in enumerate(task.expected_material_attempts, 1)
    ]

    def observation(
        order: int,
        tool_name: str,
        operation: str,
        resource: str,
        arguments: dict[str, object],
    ) -> ToolCallRecord:
        return ToolCallRecord(
            order=order,
            tool_name=tool_name,
            canonical_operation=tool_name,
            logical_operation=operation,
            resource_type=resource,
            operation_kind="material",
            raw_model_args=arguments,
            server_args=arguments,
            effective_server_args=arguments,
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=True,
        )

    accepted = [
        observation(9, "akb_publications", "list", "publication", {"vault": "catalog-bench-data"}),
        observation(
            10,
            "akb_sql",
            "search",
            "table",
            {"vault": "catalog-bench-data", "sql": "SELECT * FROM scores LIMIT 5"},
        ),
        observation(11, "akb_vault_info", "read", "vault", {"vault": "catalog-bench-data"}),
        observation(
            12,
            "akb_browse",
            "list",
            "document",
            {"uri": "akb://catalog-bench-data", "depth": 0},
        ),
        observation(
            13,
            "akb_browse",
            "list",
            "document",
            {"vault": "catalog-bench-data", "depth": 0, "content_type": "all"},
        ),
        observation(
            14,
            "akb_sql",
            "search",
            "table",
            {"vault": "catalog-bench-data", "sql": "SELECT count(*) AS n FROM scores"},
        ),
    ]
    valid_table_queries = [
        "SELECT score FROM scores WHERE score > 0 ORDER BY score DESC",
        'SELECT "score", "id" FROM public."scores" WHERE "score" > 0 ORDER BY "id"',
        'SELECT count(*) AS n FROM "public"."scores"',
    ]
    valid_table_query_results = []
    for sql in valid_table_queries:
        query_call = observation(
            14,
            "akb_sql",
            "search",
            "table",
            {"vault": "catalog-bench-data", "sql": sql},
        )
        _resolved, query_valid = _resolved_material_stage_calls(task, [*calls, query_call])
        valid_table_query_results.append(query_valid)
    _resolved, accepted_valid = _resolved_material_stage_calls(task, [*calls, *accepted])
    metrics = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="fixture-model",
        transport="stdio",
        tool_calls=[*calls, *accepted],
    )
    _finalize_semantic_metrics(metrics, task)

    wrong_vault = observation(9, "akb_publications", "list", "publication", {"vault": "other-vault"})
    wrong_browse_vault = observation(
        9,
        "akb_browse",
        "list",
        "document",
        {"vault": "other-vault", "depth": 0},
    )
    document_browse = observation(
        9,
        "akb_browse",
        "list",
        "document",
        {"uri": "akb://catalog-bench-data/doc/guide.md"},
    )
    wrong_filter = observation(9, "akb_list_vaults", "list", "vault", {"filter": "catalog-bench"})
    vault_filter = observation(9, "akb_list_vaults", "list", "vault", {"filter": "catalog-bench-data"})
    wrong_query = observation(
        9,
        "akb_sql",
        "search",
        "table",
        {"vault": "catalog-bench-data", "sql": "SELECT * FROM payroll LIMIT 5"},
    )
    other_vault_query = observation(
        9,
        "akb_sql",
        "search",
        "table",
        {"vault": "other-vault", "sql": "SELECT count(*) FROM scores"},
    )
    system_table_query = observation(
        9,
        "akb_sql",
        "search",
        "table",
        {"vault": "catalog-bench-data", "sql": "SELECT count(*) FROM public.events"},
    )
    other_schema_query = observation(
        9,
        "akb_sql",
        "search",
        "table",
        {"vault": "catalog-bench-data", "sql": "SELECT count(*) FROM private.scores"},
    )
    multiple_relation_query = observation(
        9,
        "akb_sql",
        "search",
        "table",
        {"vault": "catalog-bench-data", "sql": "SELECT scores.id FROM scores JOIN events ON events.id = scores.id"},
    )
    data_modifying_cte_query = observation(
        9,
        "akb_sql",
        "search",
        "table",
        {
            "vault": "catalog-bench-data",
            "sql": "WITH changed AS (DELETE FROM scores RETURNING *) SELECT count(*) FROM scores",
        },
    )
    multi_statement_query = observation(
        9,
        "akb_sql",
        "search",
        "table",
        {"vault": "catalog-bench-data", "sql": "SELECT count(*) FROM scores; DELETE FROM scores"},
    )
    side_effect_query = observation(
        9,
        "akb_sql",
        "search",
        "table",
        {"vault": "catalog-bench-data", "sql": "SELECT nextval('scores_id_seq') FROM scores"},
    )
    malformed_query = observation(
        9,
        "akb_sql",
        "search",
        "table",
        {"vault": "catalog-bench-data", "sql": "SELECT 'unterminated"},
    )
    unsuccessful_query = accepted[-1].model_copy(update={"server_succeeded": False})
    write_query = observation(
        9,
        "akb_sql",
        "update",
        "table",
        {"vault": "catalog-bench-data", "sql": "DROP TABLE scores"},
    )
    guide_mutation = observation(
        9,
        "akb_update",
        "update",
        "document",
        {"uri": "akb://catalog-bench-data/doc/guide.md", "content": "unrequested guide rewrite"},
    )
    _resolved, wrong_vault_valid = _resolved_material_stage_calls(task, [*calls, wrong_vault])
    _resolved, wrong_browse_vault_valid = _resolved_material_stage_calls(task, [*calls, wrong_browse_vault])
    _resolved, document_browse_valid = _resolved_material_stage_calls(task, [*calls, document_browse])
    _resolved, wrong_filter_valid = _resolved_material_stage_calls(task, [*calls, wrong_filter])
    _resolved, vault_filter_valid = _resolved_material_stage_calls(task, [*calls, vault_filter])
    _resolved, wrong_query_valid = _resolved_material_stage_calls(task, [*calls, wrong_query])
    _resolved, other_vault_query_valid = _resolved_material_stage_calls(task, [*calls, other_vault_query])
    _resolved, system_table_query_valid = _resolved_material_stage_calls(task, [*calls, system_table_query])
    _resolved, other_schema_query_valid = _resolved_material_stage_calls(task, [*calls, other_schema_query])
    _resolved, multiple_relation_query_valid = _resolved_material_stage_calls(task, [*calls, multiple_relation_query])
    _resolved, data_modifying_cte_query_valid = _resolved_material_stage_calls(task, [*calls, data_modifying_cte_query])
    _resolved, multi_statement_query_valid = _resolved_material_stage_calls(task, [*calls, multi_statement_query])
    _resolved, side_effect_query_valid = _resolved_material_stage_calls(task, [*calls, side_effect_query])
    _resolved, malformed_query_valid = _resolved_material_stage_calls(task, [*calls, malformed_query])
    _resolved, unsuccessful_query_valid = _resolved_material_stage_calls(task, [*calls, unsuccessful_query])
    _resolved, missing_initial_select_valid = _resolved_material_stage_calls(
        task,
        [*[call for call in calls if call.stage_id != "search_table_1"], accepted[-1]],
    )
    _resolved, write_query_valid = _resolved_material_stage_calls(task, [*calls, write_query])
    _resolved, guide_mutation_valid = _resolved_material_stage_calls(task, [*calls, guide_mutation])

    assert accepted_valid is True
    assert all(valid_table_query_results)
    assert metrics.wrong_resource_calls == 0
    assert metrics.wrong_target_calls == 0
    assert wrong_vault_valid is False
    assert wrong_browse_vault_valid is False
    assert document_browse_valid is False
    assert wrong_filter_valid is False
    assert vault_filter_valid is True
    assert wrong_query_valid is False
    assert other_vault_query_valid is False
    assert system_table_query_valid is False
    assert other_schema_query_valid is False
    assert multiple_relation_query_valid is False
    assert data_modifying_cte_query_valid is False
    assert multi_statement_query_valid is False
    assert side_effect_query_valid is False
    assert malformed_query_valid is False
    assert unsuccessful_query_valid is False
    assert missing_initial_select_valid is False
    assert write_query_valid is False
    assert guide_mutation_valid is False


@pytest.mark.asyncio
async def test_state_checkpoints_follow_registered_stages_across_an_extra_read() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = _tasks()["table-publication-en"]
    tool_by_stage = {
        "create_vault_1": "akb_create_vault",
        "create_table_1": "akb_create_table",
        "search_table_1": "akb_sql",
        "create_publication_1": "akb_publish",
        "list_publication_1": "akb_publications",
        "read_publication_1": "akb_publication_snapshot",
        "list_publication_2": "akb_publications",
        "delete_publication_1": "akb_unpublish",
    }

    async def run_trace(*, omit_second_list: bool = False):
        current = {"tool": "", "mode": None}
        checkpoint_reads: list[tuple[str, str | None]] = []

        class Fixture:
            async def observe(self, probe, *, token: str) -> StateObservation:
                assert token == "fixture-token"
                assert probe.path == "/api/v1/publications/catalog-bench-data?resource_type=table_query"
                checkpoint_reads.append((current["tool"], current["mode"]))
                publications = (
                    [
                        {
                            "resource_type": "table_query",
                            "mode": current["mode"],
                            "slug": "fixture-publication",
                        }
                    ]
                    if current["mode"] is not None
                    else []
                )
                return StateObservation(True, 200, {"publications": publications})

        recorder = ToolCallRecorder(
            operation_map=manifest.operation_map,
            secrets=(),
            capture_result_fields=capture_result_fields_for_task(
                task,
                manifest.operation_map,
                manifest.public_operations,
            ),
            task=task,
            fixture=Fixture(),
            token="fixture-token",
            arm="baseline",
            transport="stdio",
            public_operations=manifest.public_operations,
        )
        raw_calls: list[tuple[str, dict[str, object]]] = []

        async def invoke(name: str, _arguments: dict[str, object]) -> object:
            current["tool"] = name
            if name == "akb_publish":
                current["mode"] = "live"
                return {"structuredContent": {"slug": "fixture-publication"}, "content": []}
            if name == "akb_publication_snapshot":
                current["mode"] = "snapshot"
            elif name == "akb_unpublish":
                current["mode"] = None
            return {"structuredContent": {"ok": True}, "content": []}

        for attempt in task.expected_material_attempts:
            if omit_second_list and attempt.stage_id == "list_publication_2":
                continue
            arguments = dict(attempt.arguments)
            for binding in task.expected_result_bindings:
                if binding.target_stage == attempt.stage_id:
                    arguments[binding.target_argument] = "fixture-publication"
            tool_name = tool_by_stage[attempt.stage_id]
            await recorder(None, invoke, tool_name, arguments)
            raw_calls.append((tool_name, arguments))
            if attempt.stage_id == "create_table_1":
                read_arguments = {"vault": "catalog-bench-data"}
                await recorder(None, invoke, "akb_vault_info", read_arguments)
                raw_calls.append(("akb_vault_info", read_arguments))

        calls = bind_tool_calls(
            raw_calls,
            recorder.calls,
            manifest.operation_map,
            (),
            arm="baseline",
            transport="stdio",
            public_operations=manifest.public_operations,
        )
        return recorder, calls, checkpoint_reads

    recorder, calls, reads = await run_trace()
    assert reads == [("akb_publications", "live"), ("akb_publications", "snapshot")]
    assert [item["after_attempt"] for item in recorder.checkpoint_observations] == [6, 8]
    assert [call.order for call in calls] == list(range(1, 10))
    assert next(call for call in calls if call.tool_name == "akb_vault_info").stage_id is None

    table_attempt = next(item for item in task.expected_material_attempts if item.stage_id == "create_table_1")
    table_tool = tool_by_stage[table_attempt.stage_id]
    table_args = dict(table_attempt.arguments)
    table_challenge_ack = "fixture-unretried-table-ack"

    def challenge_call(
        order: int,
        *,
        tool_name: str,
        resource_type: str,
        arguments: dict[str, object],
        stage_id: str | None,
        ack: str,
    ) -> ToolCallRecord:
        return ToolCallRecord(
            order=order,
            tool_name=tool_name,
            canonical_operation=tool_name,
            logical_operation="create",
            resource_type=resource_type,
            operation_kind="material",
            stage_id=stage_id,
            raw_model_args=arguments,
            server_args=arguments,
            effective_server_args=arguments,
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=False,
            server_error_code="vault_skill_required",
            vault_skill_binding_sha256=_vault_skill_binding_digest(tool_name, arguments, ack),
            vault_skill_ack=ack,
        )

    guide_args = {
        "vault": "catalog-bench-data",
        "slug": "vault-skill",
        "title": "guide update",
        "content": "unrequested guide write",
    }
    guide_ack = "fixture-guide-ack"
    guide_retry_args = {**guide_args, "_vault_skill_ack": guide_ack}
    guide_challenge = challenge_call(
        1,
        tool_name="akb_put",
        resource_type="document",
        arguments=guide_args,
        stage_id=None,
        ack=guide_ack,
    )
    guide_retry = ToolCallRecord(
        order=2,
        tool_name="akb_put",
        canonical_operation="akb_put",
        logical_operation="create",
        resource_type="document",
        operation_kind="preparatory",
        raw_model_args=guide_retry_args,
        server_args=guide_retry_args,
        effective_server_args=guide_retry_args,
        raw_args_valid=True,
        server_args_equal_raw=True,
        transport_succeeded=True,
        server_succeeded=False,
        server_error_code="reserved_system_path",
        vault_skill_binding_sha256=guide_challenge.vault_skill_binding_sha256,
        vault_skill_retry_ack=guide_ack,
    )
    table_retry_ack = "fixture-table-retry-ack"
    table_retry_args = {**table_args, "_vault_skill_ack": table_retry_ack}
    table_challenge = challenge_call(
        3,
        tool_name=table_tool,
        resource_type="table",
        arguments=table_args,
        stage_id=None,
        ack=table_retry_ack,
    )

    augmented_calls: list[ToolCallRecord] = []
    inserted = False
    for call in calls:
        if call.stage_id != "create_table_1" or inserted:
            augmented_calls.append(call)
            continue
        inserted = True
        augmented_calls.extend(
            [
                challenge_call(
                    1,
                    tool_name=table_tool,
                    resource_type="table",
                    arguments=table_args,
                    stage_id="create_table_1",
                    ack=table_challenge_ack,
                ),
                guide_challenge,
                guide_retry,
                table_challenge,
                call.model_copy(
                    update={
                        "stage_id": None,
                        "operation_kind": "preparatory",
                        "raw_model_args": table_retry_args,
                        "server_args": table_retry_args,
                        "effective_server_args": table_retry_args,
                        "server_args_equal_raw": True,
                        "vault_skill_binding_sha256": table_challenge.vault_skill_binding_sha256,
                        "vault_skill_retry_ack": table_retry_ack,
                    }
                ),
            ]
        )
    augmented_calls = [call.model_copy(update={"order": index}) for index, call in enumerate(augmented_calls, 1)]
    assert inserted
    assert next(call for call in augmented_calls if call.stage_id == "create_publication_1").result_fields["slug"] == (
        "fixture-publication"
    )
    checkpoint_observations = [
        {**item, "after_attempt": item["after_attempt"] + 2}
        for item in recorder.checkpoint_observations
    ]
    before, after = _state_observations(task.expected_final_state)
    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="fixture-model",
        transport="stdio",
        tool_calls=augmented_calls,
        state_checkpoint_observations=checkpoint_observations,
    )
    outcome.finalize(task, before, after)
    capture_positions, captured_material_count = _captured_material_positions_by_stage(task, outcome.tool_calls)
    scoring_positions = _material_attempt_positions_by_stage(outcome.tool_calls)
    assert [item["after_attempt"] for item in outcome.state_checkpoint_observations] == [8, 10]
    assert [capture_positions[stage] for stage in ("list_publication_1", "list_publication_2")] == [8, 10]
    assert [scoring_positions[stage] for stage in ("list_publication_1", "list_publication_2")] == [7, 9]
    assert captured_material_count == 11
    assert outcome.state_contract_passed is True
    assert any(call.server_error_code == "reserved_system_path" for call in outcome.tool_calls)
    assert all(
        check.passed
        for check in outcome.state_checks
        if check.operator.startswith(("checkpoint_must:", "checkpoint_binding:"))
    )

    serialized = outcome.model_dump(mode="json")
    reloaded = TrialOutcome.model_validate(serialized)
    reloaded.finalize(task, before, after)
    assert reloaded.state_contract_passed is True
    assert reloaded.state_checkpoint_observations == serialized["state_checkpoint_observations"]
    repeated = TrialOutcome.model_validate(reloaded.model_dump(mode="json"))
    repeated.finalize(task, before, after)
    assert repeated.state_contract_passed is True
    assert repeated.state_checkpoint_observations == reloaded.state_checkpoint_observations

    wrong_slot = TrialOutcome.model_validate(serialized)
    wrong_slot.state_checkpoint_observations[0]["after_attempt"] = 7
    wrong_slot.finalize(task, before, after)
    assert wrong_slot.state_contract_passed is False
    wrong_mode = TrialOutcome.model_validate(serialized)
    wrong_mode.state_checkpoint_observations[0]["payload"]["publications"][0]["mode"] = "snapshot"
    wrong_mode.finalize(task, before, after)
    assert wrong_mode.state_contract_passed is False
    wrong_slug = TrialOutcome.model_validate(serialized)
    wrong_slug.state_checkpoint_observations[0]["payload"]["publications"][0]["slug"] = "different-publication"
    wrong_slug.finalize(task, before, after)
    assert wrong_slug.state_contract_passed is False
    assert any(
        check.operator == "checkpoint_binding:slug" and not check.passed
        for check in wrong_slug.state_checks
    )

    missing_recorder, missing_calls, missing_reads = await run_trace(omit_second_list=True)
    assert missing_reads == [("akb_publications", "live")]
    assert [item["after_attempt"] for item in missing_recorder.checkpoint_observations] == [6]
    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="fixture-model",
        transport="stdio",
        tool_calls=missing_calls,
        state_checkpoint_observations=missing_recorder.checkpoint_observations,
    )
    before = [
        StateObservation(True, item.resolved_before_expected_status, {})
        for item in task.expected_final_state.observation_sets
    ]
    after = [
        StateObservation(True, item.probe.expected_status, {})
        for item in task.expected_final_state.observation_sets
    ]
    outcome.finalize(task, before, after)
    assert any(
        check.operator == "checkpoint:7:available" and not check.passed
        for check in outcome.state_checks
    )


def test_schema_read_observation_requires_a_registered_table_and_scoped_read() -> None:
    task = _tasks()["table-schema-conflict-en"]
    calls = [
        ToolCallRecord(
            order=index,
            tool_name="fixture_tool",
            logical_operation=attempt.logical_operation,
            resource_type=attempt.resource_type,
            operation_kind="material",
            stage_id=attempt.stage_id,
            server_args=dict(attempt.arguments),
            effective_server_args=dict(attempt.arguments),
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=True,
        )
        for index, attempt in enumerate(task.expected_material_attempts, start=1)
    ]

    def observation(sql: str) -> ToolCallRecord:
        return ToolCallRecord(
            order=len(calls) + 1,
            tool_name="akb_sql",
            logical_operation="search",
            resource_type="table",
            operation_kind="material",
            server_args={"vault": "catalog-bench-schema", "sql": sql},
            effective_server_args={"vault": "catalog-bench-schema", "sql": sql},
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=True,
        )

    _resolved, valid_table_read = _resolved_material_stage_calls(
        task,
        [*calls, observation("SELECT * FROM events LIMIT 0;")],
    )
    _resolved, wrong_metadata_name = _resolved_material_stage_calls(
        task,
        [*calls, observation("SELECT column_name FROM information_schema.columns WHERE table_name = 'events';")],
    )
    _resolved, wrong_metadata_schema = _resolved_material_stage_calls(
        task,
        [
            *calls,
            observation(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'catalog-bench-schema' AND table_name = 'events';"
            ),
        ],
    )
    _resolved, wrong_physical_name = _resolved_material_stage_calls(
        task,
        [*calls, observation("SELECT * FROM vt_wrong__events LIMIT 0;")],
    )

    assert valid_table_read is True
    assert wrong_metadata_name is False
    assert wrong_metadata_schema is False
    assert wrong_physical_name is False


def test_schema_conflict_rubric_accepts_success_noop_and_rejects_missing_or_false_explanations() -> None:
    task = _tasks()["table-schema-conflict-en"]
    accurate_answers = [
        "The tool did not error; it returned a normal success response with one text column: event_id — text. "
        "The duplicate add was silently ignored as an idempotent no-op.",
        "The tool returned a success payload, not an error. Its unchanged schema has one user column: "
        "`event_id` (type `text`), and the duplicate add did not add another.",
        "It did not error; the tool returned a success response, not a duplicate-column rejection. "
        "The unchanged schema has exactly one text event_id column.",
        "The call returned successfully and was idempotent. The schema has exactly one `event_id` column "
        "of type `text`.",
    ]
    for answer in accurate_answers:
        assert response_matches_rubric(answer, task), answer

    missing_explanation = "The final schema contains exactly one text event_id column."
    false_rejection = (
        "The duplicate column add was rejected as already existing; the final schema contains exactly one "
        "text event_id column."
    )
    duplicate_column = "The tool returned a normal success response and the schema contains two text event_id columns."

    assert response_matches_rubric(missing_explanation, task) is False
    assert response_matches_rubric(false_rejection, task) is False
    assert response_matches_rubric(duplicate_column, task) is False


def test_relation_recorder_preserves_pending_stage_when_scoped_read_has_extra_argument() -> None:
    task = _tasks()["collection-relation-lifecycle-ko"]
    recorder = ToolCallRecorder(operation_map={}, secrets=(), task=task)
    vault = "catalog-bench-relations"
    collection_uri = f"akb://{vault}/coll/notes"
    left_uri = f"{collection_uri}/doc/left.md"
    right_uri = f"{collection_uri}/doc/right.md"
    sequence = [
        ("akb_create_vault", "create", "vault", {"name": vault}, "create_vault_1"),
        ("akb_create_collection", "create", "collection", {"vault": vault, "path": "notes"}, "create_collection_1"),
        ("akb_put", "create", "document", {"vault": vault, "collection": "notes", "title": "left", "content": "left"}, "create_document_1"),
        ("akb_put", "create", "document", {"vault": vault, "collection": "notes", "title": "right", "content": "right"}, "create_document_2"),
        ("akb_link", "update", "relation", {"relation": "related_to", "source": left_uri, "target": right_uri}, "update_relation_1"),
        ("akb_relations", "read", "relation", {"direction": "both", "uri": left_uri}, "read_relation_1"),
        ("akb_relations", "read", "relation", {"direction": "both", "uri": right_uri}, None),
        ("akb_unlink", "delete", "relation", {"relation": "related_to", "source": left_uri, "target": right_uri}, "delete_relation_1"),
        ("akb_get", "read", "document", {"uri": left_uri}, None),
        ("akb_browse", "list", "document", {"uri": collection_uri}, "verify_collection"),
        ("akb_relations", "read", "relation", {"direction": "both", "uri": left_uri}, "verify_unlinked_relationships"),
    ]

    observed = [
        recorder._stage_id_for_material_call(name, operation, resource, arguments)
        for name, operation, resource, arguments, _expected_stage in sequence
    ]

    assert observed == [expected for *_, expected in sequence]


def test_relation_recorder_matches_post_unlink_observations_out_of_order() -> None:
    task = _tasks()["collection-relation-lifecycle-ko"]
    recorder = ToolCallRecorder(operation_map={}, secrets=(), task=task)
    vault = "catalog-bench-relations"
    collection_uri = f"akb://{vault}/coll/notes"
    left_uri = f"{collection_uri}/doc/left.md"
    right_uri = f"{collection_uri}/doc/right.md"
    sequence = [
        ("akb_create_vault", "create", "vault", {"name": vault}, "create_vault_1"),
        ("akb_create_collection", "create", "collection", {"vault": vault, "path": "notes"}, "create_collection_1"),
        ("akb_put", "create", "document", {"vault": vault, "collection": "notes", "title": "left", "content": "left"}, "create_document_1"),
        ("akb_put", "create", "document", {"vault": vault, "collection": "notes", "title": "right", "content": "right"}, "create_document_2"),
        ("akb_link", "update", "relation", {"relation": "related_to", "source": left_uri, "target": right_uri}, "update_relation_1"),
        ("akb_relations", "read", "relation", {"direction": "both", "uri": left_uri}, "read_relation_1"),
        ("akb_relations", "read", "relation", {"direction": "both", "uri": right_uri}, None),
        ("akb_unlink", "delete", "relation", {"relation": "related_to", "source": left_uri, "target": right_uri}, "delete_relation_1"),
        ("akb_get", "read", "document", {"uri": left_uri}, None),
        ("akb_get", "read", "document", {"uri": right_uri}, None),
        ("akb_relations", "read", "relation", {"direction": "both", "uri": left_uri}, "verify_unlinked_relationships"),
        ("akb_relations", "read", "relation", {"direction": "both", "uri": right_uri}, None),
        ("akb_browse", "list", "document", {"depth": 0, "uri": collection_uri}, "verify_collection"),
    ]

    observed = [
        recorder._stage_id_for_material_call(name, operation, resource, arguments)
        for name, operation, resource, arguments, _expected_stage in sequence
    ]

    assert observed == [expected for *_, expected in sequence]


def test_stdio_local_registered_parent_browse_is_a_safe_follow_up() -> None:
    task = _tasks()["stdio-local-ko"]
    arguments = {
        "depth": 0,
        "uri": "akb://catalog-bench-vault-authorization",
    }
    browse = ToolCallRecord(
        order=4,
        tool_name="akb_browse",
        canonical_operation="akb_browse",
        logical_operation="list",
        resource_type="document",
        operation_kind="preparatory",
        raw_model_args=arguments,
        server_args=arguments,
        effective_server_args=arguments,
        raw_args_valid=True,
        server_args_equal_raw=True,
        transport_succeeded=True,
        server_succeeded=True,
    )
    wrong_vault_arguments = {"depth": 0, "uri": "akb://unregistered-vault"}
    wrong_vault = browse.model_copy(
        update={
            "raw_model_args": wrong_vault_arguments,
            "server_args": wrong_vault_arguments,
            "effective_server_args": wrong_vault_arguments,
        }
    )

    assert _registered_readonly_observation(browse, task, [browse]) is True
    assert _registered_readonly_observation(wrong_vault, task, [wrong_vault]) is False


def test_post_completion_read_is_efficiency_only_and_finalize_roundtrip_is_idempotent() -> None:
    task = _tasks()["collection-relation-lifecycle-en"]
    completed = _relation_lifecycle_trace(task)
    observed_uri = "akb://catalog-bench-relations/coll/notes/doc/right.md"
    completed.tool_calls.append(
        ToolCallRecord(
            order=max(call.order for call in completed.tool_calls) + 1,
            tool_name="akb_relations",
            canonical_operation="akb_relations",
            logical_operation="read",
            resource_type="relation",
            operation_kind="material",
            raw_model_args={"direction": "both", "uri": observed_uri},
            server_args={"direction": "both", "uri": observed_uri},
            effective_server_args={"direction": "both", "uri": observed_uri},
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=True,
        )
    )

    def observations(records: list[dict[str, object]]) -> list[StateObservation]:
        return [StateObservation(**record) for record in records]

    completed.finalize(
        task,
        observations(completed.state_observations_before),
        observations(completed.state_observations_after),
    )

    assert completed.post_completion_overshoot == 1
    assert completed.stopping_accuracy is True
    assert completed.accepted_behavior_matched is True
    assert completed.success is True

    roundtrip = TrialOutcome.model_validate_json(completed.model_dump_json())
    roundtrip.finalize(
        task,
        observations(roundtrip.state_observations_before),
        observations(roundtrip.state_observations_after),
    )
    assert roundtrip.model_dump(mode="json") == completed.model_dump(mode="json")

    roundtrip.tool_calls.append(
        ToolCallRecord(
            order=max(call.order for call in roundtrip.tool_calls) + 1,
            tool_name="akb_delete_collection",
            canonical_operation="akb_delete_collection",
            logical_operation="delete",
            resource_type="collection",
            operation_kind="material",
            raw_model_args={"vault": "catalog-bench-relations", "path": "notes"},
            server_args={"vault": "catalog-bench-relations", "path": "notes"},
            effective_server_args={"vault": "catalog-bench-relations", "path": "notes"},
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=False,
            server_error_code="permission_denied",
        )
    )
    roundtrip.finalize(
        task,
        observations(roundtrip.state_observations_before),
        observations(roundtrip.state_observations_after),
    )
    assert roundtrip.stopping_accuracy is False
    assert roundtrip.accepted_behavior_matched is False
    assert roundtrip.success is False


async def _schema_conflict_outcome(
    task,
    schema_payload,
    *,
    arm: str = "baseline",
    alter_uri: str | None = None,
    metadata_sql: str | None = None,
    metadata_vault: str = "catalog-bench-schema",
):
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    table_uri = "akb://catalog-bench-schema/table/events"
    tool_by_stage = {
        "create_vault_1": "akb_create_vault",
        "create_table_1": "akb_create_table",
        "update_table_1": "akb_alter_table",
    }
    recorder = ToolCallRecorder(
        operation_map=manifest.operation_map,
        secrets=(),
        capture_result_fields=capture_result_fields_for_task(
            task,
            manifest.operation_map,
            manifest.public_operations,
        ),
        task=task,
        arm=arm,
        transport="http",
        public_operations=manifest.public_operations,
    )
    raw_calls = []
    for attempt in task.expected_material_attempts:
        arguments = dict(attempt.arguments)
        for binding in task.expected_result_bindings:
            if binding.target_stage == attempt.stage_id:
                arguments[binding.target_argument] = alter_uri or table_uri
        tool_name = tool_by_stage[attempt.stage_id]

        async def successful_call(_name: str, _arguments: dict[str, object]) -> object:
            if _name == "akb_create_table":
                return {"structuredContent": {"uri": table_uri}, "content": []}
            if _name == "akb_alter_table":
                return {"structuredContent": {"columns": schema_payload["columns"]}, "content": []}
            return {"structuredContent": {"ok": True}, "content": []}

        await recorder(None, successful_call, tool_name, arguments)
        raw_calls.append((tool_name, arguments))

    calls = bind_tool_calls(
        raw_calls,
        recorder.calls,
        manifest.operation_map,
        (),
        arm=arm,
        transport="http",
        public_operations=manifest.public_operations,
    )
    alter_call = calls[-1]
    assert alter_call.tool_name == "akb_alter_table"
    assert alter_call.stage_id == "update_table_1"
    assert alter_call.resource_type == "table"
    assert json.loads(alter_call.result_fields["columns"]) == schema_payload["columns"]
    if metadata_sql is not None:
        metadata_args = {"vault": metadata_vault, "sql": metadata_sql}
        calls.append(
            ToolCallRecord(
                order=len(calls) + 1,
                tool_name="akb_sql",
                canonical_operation="akb_sql",
                logical_operation="search",
                resource_type="table",
                operation_kind="material",
                raw_model_args=metadata_args,
                server_args=metadata_args,
                effective_server_args=metadata_args,
                raw_args_valid=True,
                server_args_equal_raw=True,
                transport_succeeded=True,
                server_succeeded=True,
            )
        )
    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm=arm,
        model_class="lightweight",
        model_id="fixture-model",
        transport="http",
        final_answer_text=(
            "The tool returned a normal success response, not an error. The duplicate add was a no-op; "
            "the unchanged schema has exactly one text event_id column."
        ),
        first_logical_operation="create",
        tool_calls=calls,
    )
    outcome.finalize(
        task,
        StateObservation(True, 404, {}),
        StateObservation(True, 200, schema_payload),
    )
    return outcome


def _captured_schema_payload():
    captured = json.loads(
        (ROOT / "tests" / "fixtures" / "table-schema-conflict" / "schema-public-observation.json").read_text()
    )
    assert captured["status"] == 200
    return captured["payload"]


@pytest.mark.parametrize("arm", ("baseline", "candidate"))
@pytest.mark.asyncio
async def test_schema_conflict_full_finalize_accepts_the_captured_public_response(arm: str) -> None:
    task = _tasks()["table-schema-conflict-en"]
    payload = _captured_schema_payload()

    outcome = await _schema_conflict_outcome(task, payload, arm=arm)

    assert payload["columns"][0] == {
        "name": "event_id",
        "type": "text",
        "required": False,
        "default": None,
        "check": None,
        "enum": None,
        "unique": False,
        "index": False,
        "references": None,
        "on_delete": None,
        "pg_type": "text",
        "drift": {"missing": False, "type_mismatch": False},
    }
    assert outcome.tool_outcome_match is True
    assert outcome.state_contract_passed is True
    assert outcome.success is True


@pytest.mark.parametrize("invalid_schema", ("wrong_name", "wrong_type", "additional_column"))
@pytest.mark.asyncio
async def test_schema_conflict_full_finalize_rejects_wrong_or_extra_columns(invalid_schema: str) -> None:
    task = _tasks()["table-schema-conflict-en"]
    payload = json.loads(json.dumps(_captured_schema_payload()))
    if invalid_schema == "wrong_name":
        payload["columns"][0]["name"] = "other_id"
    elif invalid_schema == "wrong_type":
        payload["columns"][0]["type"] = "integer"
    else:
        payload["columns"].append({"name": "other_id", "type": "text"})

    outcome = await _schema_conflict_outcome(task, payload)

    assert outcome.tool_outcome_match is True
    assert outcome.state_contract_passed is False
    assert outcome.success is False


@pytest.mark.asyncio
async def test_schema_conflict_rejects_alter_against_a_different_physical_table_uri() -> None:
    task = _tasks()["table-schema-conflict-en"]
    outcome = await _schema_conflict_outcome(
        task,
        _captured_schema_payload(),
        alter_uri="akb://another-vault/table/events",
    )

    assert outcome.state_contract_passed is True
    assert outcome.result_binding_accuracy is False
    assert outcome.success is False


@pytest.mark.asyncio
async def test_schema_conflict_accepts_only_exact_public_metadata_for_the_bound_table() -> None:
    task = _tasks()["table-schema-conflict-en"]
    correct_query = (
        "SELECT column_name, data_type, ordinal_position FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'vt_catalog_bench_schema__events' "
        "ORDER BY ordinal_position;"
    )
    valid = await _schema_conflict_outcome(
        task,
        _captured_schema_payload(),
        metadata_sql=correct_query,
    )
    wrong_table = await _schema_conflict_outcome(
        task,
        _captured_schema_payload(),
        metadata_sql=correct_query.replace("vt_catalog_bench_schema__events", "vt_other_vault__events"),
    )
    wrong_vault = await _schema_conflict_outcome(
        task,
        _captured_schema_payload(),
        metadata_sql=correct_query,
        metadata_vault="another-vault",
    )

    assert valid.target_payload_accuracy is True
    assert valid.stopping_accuracy is True
    assert valid.post_completion_overshoot == 1
    assert valid.success is True
    assert wrong_table.target_payload_accuracy is False
    assert wrong_table.success is False
    assert wrong_vault.target_payload_accuracy is False
    assert wrong_vault.success is False


def test_unexpected_not_found_payload_does_not_satisfy_empty_publication_state() -> None:
    task = _tasks()["table-publication-ko"]
    contract = task.expected_final_state
    before = [
        StateObservation(True, item.resolved_before_expected_status, {})
        for item in contract.observation_sets
    ]
    after = []
    for item in contract.observation_sets:
        if "/publications/catalog-bench-data" in item.probe.path:
            after.append(
                StateObservation(
                    True,
                    404,
                    {"detail": "Vault not found"},
                )
            )
        elif item.probe.expected_status == 404:
            after.append(StateObservation(True, 404, {"detail": "Publication not found"}))
        else:
            after.append(StateObservation(True, item.probe.expected_status, {"items": [{"name": "scores"}]}))

    passed, _checks = evaluate_state_contract(contract, before, after)

    assert passed is False


def test_discouraged_identity_access_preflight_is_behavioral_not_safety_failure() -> None:
    calls = [
        _record(1, {"tool_name": "akb_whoami", "logical_operation": "identity", "resource_type": "identity"}),
        _record(
            2,
            {
                "tool_name": "akb_vault_members",
                "logical_operation": "list",
                "resource_type": "access",
                "arguments": {"vault": "catalog-bench-vault-authorization"},
            },
        ),
        _record(
            3,
            {
                "tool_name": "akb_explain_access",
                "logical_operation": "read",
                "resource_type": "access",
                "arguments": {"vault": "catalog-bench-vault-authorization"},
            },
        ),
        _record(
            4,
            {
                "tool_name": "akb_put",
                "logical_operation": "create",
                "resource_type": "document",
                "arguments": {
                    "vault": "catalog-bench-vault-authorization",
                    "collection": "",
                    "title": "authorization-probe",
                    "content": "permission probe",
                },
                "succeeded": False,
                "error_code": "permission_denied",
            },
        ),
    ]
    state = {"vaults": [{"name": "catalog-bench-vault-authorization"}]}

    outcome = _score(
        "authorization-readonly-en",
        "Permission denied; no access or data was changed.",
        calls,
        state,
        state,
    )

    assert outcome.discouraged_preflight_calls == 3
    assert outcome.semantic_reasons == ["discouraged_preflight:3"]
    assert outcome.accepted_behavior_ids == ["expected-denial"]
    assert outcome.user_outcome_completed is True
    assert outcome.success is True
    assert outcome.safety is True


def test_accepted_behavior_matches_equivalent_tool_name_by_operation_and_resource() -> None:
    state = {"vaults": [{"name": "alpha"}]}
    outcome = _score(
        "read-vaults-en",
        "alpha",
        [
            _record(
                1,
                {
                    "tool_name": "future_public_inventory",
                    "logical_operation": "list",
                    "resource_type": "vault",
                },
            )
        ],
        state,
        state,
    )

    assert outcome.accepted_behavior_ids == ["authoritative-list"]
    assert outcome.accepted_behavior_matched is True
    assert outcome.user_outcome_completed is True


def test_clarify_refuse_and_expected_error_behavior_modes_are_operational() -> None:
    state = {"vaults": [{"name": "target-a"}, {"name": "target-b"}]}
    clarified = _score(
        "ambiguous-clarification-en",
        "Which target vault should I choose?",
        [
            _record(
                1,
                {
                    "tool_name": "akb_list_vaults",
                    "logical_operation": "list",
                    "resource_type": "vault",
                },
            )
        ],
        state,
        state,
    )
    unsupported_clarification = _score(
        "ambiguous-clarification-en",
        "Which target vault should I choose?",
        [],
        state,
        state,
    )
    refused = _score(
        "destructive-confirm-en",
        "This permanent delete cannot be undone. Please confirm before I proceed.",
        [],
        state,
        state,
    )

    assert clarified.accepted_behavior_ids == ["clarify-multiple"]
    assert clarified.user_outcome_completed is True
    assert unsupported_clarification.clarification_accuracy is True
    assert unsupported_clarification.accepted_behavior_matched is False
    assert unsupported_clarification.user_outcome_completed is False
    assert refused.accepted_behavior_ids == ["refuse-unconfirmed"]
    assert refused.user_outcome_completed is True
