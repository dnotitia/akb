from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mcp_catalog.contracts import load_run_manifest, load_task_corpus, source_blind_violations_for
from mcp_catalog.execution import (
    ToolCallRecord,
    ToolCallRecorder,
    TrialOutcome,
    bind_tool_calls,
    capture_result_fields_for_task,
    render_task_prompt,
)
from mcp_catalog.runtime import StateObservation


ROOT = Path(__file__).parents[1]
VAULT = "catalog-bench-vault-authorization"
PARENT = "akb://catalog-bench-vault-authorization"
FILE_NAME = "sample-note.txt"
IMAGE_NAME = "sample-image.png"
DOCUMENT_TITLE = "catalog-bench-stdio-document"
IMAGE_ALT = "benchmark sample image"
IMAGE_MARKDOWN = f"![{IMAGE_ALT}](/api/assets/fixture-image)"


def test_stdio_pair_declares_source_blind_local_file_targets() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    stdio_tasks = [task for task in tasks if task.pair_id == "stdio-local"]

    manifest.validate_tasks(tasks)
    assert len(stdio_tasks) == 2
    assert {task.locale for task in stdio_tasks} == {"ko-KR", "en-US"}
    assert source_blind_violations_for(stdio_tasks, manifest.operation_map) == []
    for task in stdio_tasks:
        assert task.fixture.transports == ["stdio"]
        assert task.fixture.local_files == [FILE_NAME, IMAGE_NAME]
        assert task.allowed_first_operations == ["file_upload"]
        assert task.material_attempt_limits == {
            "file_upload": 1,
            "image_upload": 1,
            "create": 1,
        }
        assert FILE_NAME in task.prompt
        assert IMAGE_NAME in task.prompt
        assert DOCUMENT_TITLE in task.prompt
        assert IMAGE_ALT in task.prompt
        assert "complete content" in task.prompt.casefold() or "본문" in task.prompt
        assert [attempt.tool_name for attempt in task.expected_material_attempts] == [
            "akb_put_file",
            "akb_put_image",
            "akb_put",
        ]
        assert [attempt.arguments for attempt in task.expected_material_attempts] == [
            {"parent": PARENT, "collection": ""},
            {"parent": PARENT, "alt_text": IMAGE_ALT},
            {
                "parent": PARENT,
                "title": DOCUMENT_TITLE,
                "type": "note",
                "status": "draft",
            },
        ]
        assert [attempt.local_file_arguments for attempt in task.expected_material_attempts] == [
            {"file_path": FILE_NAME},
            {"file_path": IMAGE_NAME},
            {},
        ]

    assert stdio_tasks[0].expected_material_attempts == stdio_tasks[1].expected_material_attempts
    assert stdio_tasks[0].expected_result_bindings == stdio_tasks[1].expected_result_bindings
    assert stdio_tasks[0].expected_result_bindings[0].model_dump(mode="json") == {
        "source_attempt": 2,
        "source_field": "markdown",
        "target_attempt": 3,
        "target_argument": "content",
        "relation": "contains",
    }
    for task in stdio_tasks:
        assert task.expected_final_state.probe.service == "app"
        assert task.expected_final_state.probe.path == f"/api/v1/browse/{VAULT}?depth=0"
        assert task.expected_final_state.must[0].value == [
            {"name": FILE_NAME, "type": "file"},
            {"name": DOCUMENT_TITLE, "type": "document"},
        ]
    assert (ROOT / "fixtures" / IMAGE_NAME).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_stdio_prompt_exposes_only_the_declared_absolute_fixture_paths() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    task = next(task for task in tasks if task.id == "stdio-local-b")
    consumer_root = Path("/private/runtime/node-consumer")
    prompt = render_task_prompt(
        task,
        {
            FILE_NAME: consumer_root / FILE_NAME,
            IMAGE_NAME: consumer_root / IMAGE_NAME,
        },
    )

    assert str(consumer_root / FILE_NAME) in prompt
    assert str(consumer_root / IMAGE_NAME) in prompt
    assert source_blind_violations_for([task.model_copy(update={"prompt": prompt})], manifest.operation_map) == []


@pytest.mark.asyncio
async def test_image_result_capture_reads_the_public_mcp_text_json_envelope() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "stdio-local-b")
    result = {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"url": "/api/assets/fixture-image", "markdown": IMAGE_MARKDOWN}),
            }
        ],
        "isError": False,
    }
    recorder = ToolCallRecorder(
        operation_map=manifest.operation_map,
        secrets=(),
        capture_result_fields=capture_result_fields_for_task(task),
    )

    async def image_upload(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return result

    returned = await recorder(
        None,
        image_upload,
        "akb_put_image",
        {"parent": PARENT, "file_path": "/tmp/sample-image.png", "alt_text": IMAGE_ALT},
    )

    assert returned is result
    assert recorder.calls[0].result_fields == {"markdown": IMAGE_MARKDOWN}

    text_result = json.dumps({"url": "/api/assets/fixture-image", "markdown": IMAGE_MARKDOWN})

    async def text_image_upload(_name: str, _arguments: dict[str, object]) -> str:
        return text_result

    await recorder(None, text_image_upload, "akb_put_image", {"parent": PARENT})
    assert recorder.calls[1].result_fields == {"markdown": IMAGE_MARKDOWN}

    async def content_blocks_image_upload(_name: str, _arguments: dict[str, object]) -> list[dict[str, str]]:
        return [{"type": "text", "text": text_result}]

    await recorder(None, content_blocks_image_upload, "akb_put_image", {"parent": PARENT})
    assert recorder.calls[2].result_fields == {"markdown": IMAGE_MARKDOWN}


@pytest.mark.asyncio
async def test_image_result_capture_is_structured_bounded_and_keeps_cleanup_data_for_the_model() -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "stdio-local-b")
    result = {
        "structuredContent": {
            "url": "/api/assets/fixture-image",
            "markdown": IMAGE_MARKDOWN,
            "unneeded": "not captured",
        },
        "content": [{"type": "text", "text": "image upload succeeded"}],
    }
    recorder = ToolCallRecorder(
        operation_map=manifest.operation_map,
        secrets=(),
        capture_result_fields=capture_result_fields_for_task(task),
    )

    async def image_upload(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return result

    returned = await recorder(
        None,
        image_upload,
        "akb_put_image",
        {"parent": PARENT, "file_path": "/tmp/sample-image.png", "alt_text": IMAGE_ALT},
    )

    assert returned is result
    assert recorder.calls[0].result_fields == {"markdown": IMAGE_MARKDOWN}
    assert result["structuredContent"]["url"] == "/api/assets/fixture-image"
    assert "not captured" not in recorder.calls[0].result_preview
    assert '"url"' not in recorder.calls[0].result_preview

    oversized = {
        "structuredContent": {"markdown": "x" * 2049},
    }

    async def oversized_upload(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return oversized

    await recorder(None, oversized_upload, "akb_put_image", {"parent": PARENT})
    assert recorder.calls[-1].result_fields == {}


def _stdio_calls(
    consumer_root: Path,
    *,
    document_content: str,
    image_markdown: str = IMAGE_MARKDOWN,
) -> list[ToolCallRecord]:
    file_args = {
        "parent": PARENT,
        "file_path": str(consumer_root / FILE_NAME),
        "collection": "",
    }
    image_args = {
        "parent": PARENT,
        "file_path": str(consumer_root / IMAGE_NAME),
        "alt_text": IMAGE_ALT,
    }
    document_args = {
        "parent": PARENT,
        "title": DOCUMENT_TITLE,
        "content": document_content,
        "type": "note",
        "status": "draft",
    }
    return [
        ToolCallRecord(
            order=1,
            tool_name="akb_put_file",
            logical_operation="file_upload",
            raw_model_args=file_args,
            server_args=file_args,
            effective_server_args=file_args,
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=True,
        ),
        ToolCallRecord(
            order=2,
            tool_name="akb_put_image",
            logical_operation="image_upload",
            raw_model_args=image_args,
            server_args=image_args,
            effective_server_args=image_args,
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=True,
            result_fields={"markdown": image_markdown},
        ),
        ToolCallRecord(
            order=3,
            tool_name="akb_put",
            logical_operation="create",
            raw_model_args=document_args,
            server_args=document_args,
            effective_server_args=document_args,
            raw_args_valid=True,
            server_args_equal_raw=True,
            transport_succeeded=True,
            server_succeeded=True,
        ),
    ]


def _score_stdio(task, consumer_root: Path, calls: list[ToolCallRecord], *, items=None) -> TrialOutcome:
    outcome = TrialOutcome(
        task_id=task.id,
        category=task.category,
        locale=task.locale,
        arm="baseline",
        model_class="primary",
        model_id="model",
        transport="stdio",
        first_logical_operation=calls[0].logical_operation if calls else "none",
        final_answer_text="The file and document are ready.",
        tool_calls=calls,
    )
    before = StateObservation(True, 200, {"items": []})
    after = StateObservation(
        True,
        200,
        {
            "items": items
            if items is not None
            else [
                {"name": FILE_NAME, "type": "file"},
                {"name": DOCUMENT_TITLE, "type": "document"},
            ]
        },
    )
    outcome.finalize(task, before, after, consumer_root=str(consumer_root))
    return outcome


@pytest.mark.parametrize(
    ("document_content", "success"),
    (
        (f"Attached image:\n\n{IMAGE_MARKDOWN}", True),
        ("![benchmark sample image](/api/assets/unrelated)", False),
        ("There is no inline image in this document.", False),
    ),
)
def test_stdio_image_markdown_must_flow_into_the_created_document(
    tmp_path: Path,
    document_content: str,
    success: bool,
) -> None:
    _manifest, tasks = load_run_manifest(ROOT / "config" / "run.json"), load_task_corpus(ROOT / "corpus" / "tasks.json")
    task = next(task for task in tasks if task.id == "stdio-local-b")
    consumer_root = tmp_path / "node-consumer"
    consumer_root.mkdir()
    for filename in (FILE_NAME, IMAGE_NAME):
        shutil.copyfile(ROOT / "fixtures" / filename, consumer_root / filename)
    outcome = _score_stdio(
        task,
        consumer_root,
        _stdio_calls(consumer_root, document_content=document_content),
    )

    assert outcome.tool_outcome_match is success
    assert outcome.success is success


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_file_path",
        "root_escape",
        "missing_file_path",
        "parent_only",
        "missing_local_file",
        "wrong_image_path",
        "svg_source",
        "wrong_parent",
        "wrong_alt",
        "wrong_tool",
        "wrong_order",
        "wrong_title",
        "failed_file_upload",
        "missing_file_attempt",
        "missing_document_attempt",
        "extra_material_attempt",
        "missing_result_field",
    ),
)
def test_stdio_attempts_reject_wrong_sources_targets_and_outcomes(tmp_path: Path, mutation: str) -> None:
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "stdio-local-b")
    consumer_root = tmp_path / "node-consumer"
    consumer_root.mkdir()
    for filename in (FILE_NAME, IMAGE_NAME):
        shutil.copyfile(ROOT / "fixtures" / filename, consumer_root / filename)
    calls = _stdio_calls(consumer_root, document_content=f"Attached image:\n\n{IMAGE_MARKDOWN}")

    if mutation == "wrong_file_path":
        args = {**calls[0].server_args, "file_path": str(consumer_root / "other.txt")}
        calls[0] = calls[0].model_copy(update={"raw_model_args": args, "server_args": args, "effective_server_args": args})
    elif mutation == "root_escape":
        args = {**calls[0].server_args, "file_path": str(consumer_root.parent / FILE_NAME)}
        calls[0] = calls[0].model_copy(update={"raw_model_args": args, "server_args": args, "effective_server_args": args})
    elif mutation in {"missing_file_path", "parent_only"}:
        args = {key: value for key, value in calls[0].server_args.items() if key != "file_path"}
        calls[0] = calls[0].model_copy(update={"raw_model_args": args, "server_args": args, "effective_server_args": args})
    elif mutation == "missing_local_file":
        (consumer_root / FILE_NAME).unlink()
    elif mutation == "wrong_image_path":
        args = {**calls[1].server_args, "file_path": str(consumer_root / "other.png")}
        calls[1] = calls[1].model_copy(update={"raw_model_args": args, "server_args": args, "effective_server_args": args})
    elif mutation == "svg_source":
        args = {**calls[1].server_args, "file_path": str(consumer_root / "sample-image.svg")}
        calls[1] = calls[1].model_copy(
            update={
                "raw_model_args": args,
                "server_args": args,
                "effective_server_args": args,
                "server_succeeded": False,
                "server_error_code": "invalid_argument",
            }
        )
    elif mutation == "wrong_parent":
        args = {**calls[1].server_args, "parent": "akb://other-vault"}
        calls[1] = calls[1].model_copy(update={"raw_model_args": args, "server_args": args, "effective_server_args": args})
    elif mutation == "wrong_alt":
        args = {**calls[1].server_args, "alt_text": "different alt"}
        calls[1] = calls[1].model_copy(update={"raw_model_args": args, "server_args": args, "effective_server_args": args})
    elif mutation == "wrong_tool":
        calls[1] = calls[1].model_copy(update={"tool_name": "akb_put_file", "logical_operation": "file_upload"})
    elif mutation == "wrong_order":
        calls = [calls[1], calls[0], calls[2]]
    elif mutation == "wrong_title":
        args = {**calls[2].server_args, "title": "other-document"}
        calls[2] = calls[2].model_copy(update={"raw_model_args": args, "server_args": args, "effective_server_args": args})
    elif mutation == "failed_file_upload":
        calls[0] = calls[0].model_copy(update={"server_succeeded": False, "server_error_code": "internal_error"})
    elif mutation == "missing_file_attempt":
        calls = calls[1:]
    elif mutation == "missing_document_attempt":
        calls = calls[:2]
    elif mutation == "extra_material_attempt":
        calls.append(calls[2].model_copy(update={"order": 4}))
    elif mutation == "missing_result_field":
        calls[1] = calls[1].model_copy(update={"result_fields": {}})

    calls = [call.model_copy(update={"order": order}) for order, call in enumerate(calls, 1)]
    outcome = _score_stdio(task, consumer_root, calls)

    assert outcome.tool_outcome_match is False
    assert outcome.required_attempts_completed is False
    assert outcome.success is False


def test_stdio_final_browse_requires_the_uploaded_file_and_created_document(tmp_path: Path) -> None:
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "stdio-local-b")
    consumer_root = tmp_path / "node-consumer"
    consumer_root.mkdir()
    for filename in (FILE_NAME, IMAGE_NAME):
        shutil.copyfile(ROOT / "fixtures" / filename, consumer_root / filename)
    calls = _stdio_calls(consumer_root, document_content=IMAGE_MARKDOWN)

    missing_file = _score_stdio(
        task,
        consumer_root,
        calls,
        items=[{"name": DOCUMENT_TITLE, "type": "document"}],
    )
    missing_document = _score_stdio(
        task,
        consumer_root,
        calls,
        items=[{"name": FILE_NAME, "type": "file"}],
    )

    assert missing_file.tool_outcome_match is True
    assert missing_file.state_contract_passed is False
    assert missing_file.success is False
    assert missing_document.tool_outcome_match is True
    assert missing_document.state_contract_passed is False
    assert missing_document.success is False


@pytest.mark.asyncio
async def test_stdio_vault_skill_preflight_does_not_count_as_a_material_attempt(tmp_path: Path) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "stdio-local-b")
    consumer_root = tmp_path / "node-consumer"
    consumer_root.mkdir()
    for filename in (FILE_NAME, IMAGE_NAME):
        shutil.copyfile(ROOT / "fixtures" / filename, consumer_root / filename)
    calls = _stdio_calls(consumer_root, document_content=IMAGE_MARKDOWN)
    file_args = calls[0].server_args
    recorder = ToolCallRecorder(operation_map=manifest.operation_map, secrets=())

    async def preflight(_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "error": "Apply the vault instructions, then retry this write.",
                            "code": "vault_skill_required",
                        },
                        separators=(",", ":"),
                    ),
                }
            ],
            "isError": False,
        }

    await recorder(None, preflight, "akb_put_file", file_args)
    challenge = bind_tool_calls(
        [("akb_put_file", json.dumps(file_args, separators=(",", ":")))],
        recorder.calls,
        manifest.operation_map,
        (),
    )

    outcome = _score_stdio(task, consumer_root, [*challenge, *calls])

    assert outcome.preparatory_call_count == 1
    assert outcome.material_call_count == 3
    assert outcome.tool_outcome_match is True
    assert outcome.success is True


def test_uncommitted_image_cleanup_is_allowed_only_after_a_failed_material_attempt(tmp_path: Path) -> None:
    task = next(task for task in load_task_corpus(ROOT / "corpus" / "tasks.json") if task.id == "stdio-local-b")
    consumer_root = tmp_path / "node-consumer"
    consumer_root.mkdir()
    for filename in (FILE_NAME, IMAGE_NAME):
        shutil.copyfile(ROOT / "fixtures" / filename, consumer_root / filename)
    calls = _stdio_calls(consumer_root, document_content=IMAGE_MARKDOWN)
    cleanup_args = {"parent": PARENT, "url": "/api/assets/fixture-image"}
    cleanup = ToolCallRecord(
        order=4,
        tool_name="akb_discard_image",
        logical_operation="cleanup",
        raw_model_args=cleanup_args,
        server_args=cleanup_args,
        effective_server_args=cleanup_args,
        raw_args_valid=True,
        server_args_equal_raw=True,
        transport_succeeded=True,
        server_succeeded=True,
    )

    failed_document = calls[2].model_copy(
        update={"server_succeeded": False, "server_error_code": "internal_error"}
    )
    cleaned_failure = _score_stdio(
        task,
        consumer_root,
        [calls[0], calls[1], failed_document, cleanup],
        items=[{"name": FILE_NAME, "type": "file"}],
    )
    premature_cleanup = _score_stdio(
        task,
        consumer_root,
        [calls[0], calls[1], cleanup, calls[2]],
    )

    assert cleaned_failure.preparatory_call_count == 1
    assert cleaned_failure.safety is True
    assert cleaned_failure.success is False
    assert premature_cleanup.tool_outcome_match is True
    assert premature_cleanup.safety is False
    assert premature_cleanup.success is False
