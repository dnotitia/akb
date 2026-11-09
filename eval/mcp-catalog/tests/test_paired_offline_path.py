from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

import mcp_catalog.cli as cli
import mcp_catalog.catalog as catalog
import mcp_catalog.execution as execution
import mcp_catalog.runner as runner
from mcp_catalog.contracts import load_run_manifest, load_task_corpus
from mcp_catalog.evidence import validate_model_input_evidence
from mcp_catalog.runtime import RuntimeDescriptor, RuntimeFixture
from paired_artifact_factory import _catalog_snapshot, provider_name_for_model, provider_registry_snapshot
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("scenario", ["normal", "output_limit", "provider_error", "cancel", "late_secret", "orphaned"])
@pytest.mark.asyncio
async def test_paired_cli_offline_runs_registered_plan_and_exports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str
) -> None:
    manifest_data = json.loads((ROOT / "config/run.json").read_text())
    manifest_data["source_revision"] = "a" * 40
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data))
    manifest = load_run_manifest(manifest_path)
    tasks = load_task_corpus(ROOT / "corpus/tasks.json")
    missing_before = {
        expectation.probe.path.split("?", 1)[0]
        for task in tasks
        for expectation in task.expected_final_state.observation_sets
        if expectation.check_before
        and expectation.resolved_before_expected_status == 404
        and expectation.probe.result_binding is None
    }
    started_tokens: set[str] = set()
    for spec in manifest.models:
        monkeypatch.setenv(spec.base_url_env, "https://openrouter.ai/api/v1")
        monkeypatch.setenv(spec.provider_key_env, "offline-provider-key")
    monkeypatch.setenv("AKB_E2E_USERNAME", "offline-user")
    monkeypatch.setenv("AKB_E2E_PASSWORD", "offline-password")
    descriptors = {}
    for arm in ("baseline", "candidate"):
        desc = descriptor_dict()
        desc["scenario"] = "app-control-plane"
        desc["services"]["fixture"]["reset"]["body"]["scenario"] = "app-control-plane"
        desc["services"]["stdio"]["consumer_root"] = str(tmp_path)
        desc["evidence"]["source_revision"] = "a" * 40
        desc["benchmark_cells"] = {}
        for index, cell_name in enumerate(("primary:http", "primary:stdio", "lightweight:http", "lightweight:stdio")):
            cell = deepcopy(desc)
            cell.pop("benchmark_cells")
            cell["services"]["app"]["origin"] = f"http://127.0.0.1:{11000 + index + (100 if arm == 'candidate' else 0)}"
            cell["services"]["fixture"]["origin"] = f"http://127.0.0.1:{12000 + index}"
            desc["benchmark_cells"][cell_name] = cell
        path = tmp_path / f"{arm}-descriptor.json"
        path.write_text(json.dumps(desc))
        descriptors[arm] = path
    for name in ("sample-note.txt", "sample-image.png"):
        (tmp_path / name).write_text("offline fixture")

    executable = tmp_path / "node_modules/akb-mcp/bin/akb-mcp.mjs"
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    original_fixture_init = RuntimeFixture.__init__
    original_http_client = httpx.AsyncClient

    def fixture_init(self, descriptor):
        original_fixture_init(self, descriptor)

        async def fixture_response(request):
            token = f"offline-token-{descriptor.app_origin.rsplit(':', 1)[-1]}"
            if request.url.path.endswith("/reset"):
                started_tokens.discard(token)
                return httpx.Response(200, json={"status": "ready", "scenario": "app-control-plane"})
            if request.url.path.endswith("/discover"):
                return httpx.Response(
                    200,
                    json={"actors": {"reader": {"username": "offline-reader"}}, "runtime": descriptor.raw["evidence"]},
                )
            if request.url.path.endswith("/auth/login"):
                return httpx.Response(200, json={"token": "offline-session"})
            if request.url.path.endswith("/auth/tokens") and request.method == "POST":
                return httpx.Response(200, json={"token": token, "token_id": "offline-id"})
            if "/auth/tokens/" in request.url.path and request.method == "DELETE":
                return httpx.Response(204)
            if (
                (request.url.path in missing_before and token not in started_tokens)
                or (request.url.path.startswith("/api/v1/public/") and request.url.path.endswith("/meta"))
                or ("/assets/" in request.url.path and request.url.path.endswith("/metadata"))
            ):
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"status": "ready", "scenario": "app-control-plane"})

        self.client._transport = httpx.MockTransport(fixture_response)

    monkeypatch.setattr(RuntimeFixture, "__init__", fixture_init)

    registry_snapshot = provider_registry_snapshot(manifest)

    async def registry_response(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [item["model_record"] for item in registry_snapshot["models"].values()]}
            )
        for model_id, item in registry_snapshot["models"].items():
            if request.url.path.endswith(f"/models/{model_id}/endpoints"):
                return httpx.Response(200, json=item["endpoint_snapshot"])
        return httpx.Response(404)

    class OfflineAsyncClient(original_http_client):
        def __init__(self, *args, **kwargs):
            if kwargs.get("timeout") == 30.0 and kwargs.get("follow_redirects") is False:
                kwargs["transport"] = httpx.MockTransport(registry_response)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(runner.httpx, "AsyncClient", OfflineAsyncClient)

    servers = {}
    for arm in ("baseline", "candidate"):
        for transport in ("http", "stdio"):
            server = FastMCP("offline", instructions="Synthetic server instructions.", version="1")
            snapshot = _catalog_snapshot(manifest, "baseline", transport)

            def empty_tool(
                vault: str = "",
                alt_text: str = "",
                file_path: str = "",
                resource_type: str = "",
                query_sql: str = "",
                collection: str = "",
                title: str = "",
                content: str = "",
                name: str = "",
                columns: list[dict] | None = None,
                sql: str = "",
                slug: str = "",
            ) -> dict:
                return {
                    "vaults": [],
                    "note": "late-discovered-name",
                    "url": "/api/assets/00000000-0000-0000-0000-000000000001",
                    "slug": "offline-publication",
                    "uri": "akb://catalog-bench-relations/doc/offline.md",
                }

            for item in snapshot.tools:
                tool = FunctionTool.from_function(empty_tool, name=item["name"], description=item["description"])
                server.add_tool(tool.model_copy(update={"parameters": item["inputSchema"]}))
            servers[(arm, transport)] = server
    monkeypatch.setattr(
        catalog,
        "_create_transport",
        lambda spec: servers[("candidate" if ":1110" in spec.app_origin else "baseline", spec.transport)],
    )

    original_openai = execution.AsyncOpenAI
    clients = []
    requests = []
    injected = []
    captured_seals = []
    if scenario == "orphaned":
        original_release = execution.BudgetLedger.release_all_reservations

        async def leak_one(self):
            if not injected:
                injected.append("reservation")
                await self.reserve_trial(0.01)
            return await original_release(self)

        monkeypatch.setattr(execution.BudgetLedger, "release_all_reservations", leak_one)

    def openai_client(*args, **kwargs):
        async def handle(request):
            body = json.loads(request.content)
            trial = execution.CURRENT_TRIAL.get()
            if trial is not None:
                started_tokens.add(trial.token)
            candidate_trial = trial is not None and trial.token.startswith("offline-token-1110")
            if candidate_trial:
                await asyncio.sleep(0.01)
            spec = next(item for item in manifest.models if item.model_id == body["model"])
            requests.append(body)
            tool_seen = any(message.get("role") == "tool" for message in body["messages"])
            if scenario == "late_secret" and not injected and trial is None and tool_seen:
                injected.append("smoke")
                captured_seals.append(execution.MODEL_INPUT_EVIDENCE.get()[-1].model_input_sha256)
                monkeypatch.setenv("AKB_E2E_USERNAME", "late-discovered-name")
            if (
                scenario in {"output_limit", "provider_error", "cancel"}
                and not injected
                and trial is not None
                and candidate_trial
                and tool_seen
            ):
                injected.append(trial.task.id)
                if scenario == "cancel":
                    target = next(
                        task for task in asyncio.all_tasks() if task.get_name() == "catalog-benchmark-candidate"
                    )
                    target.cancel()
                    await asyncio.sleep(0.1)
                if scenario == "provider_error":
                    return httpx.Response(503, json={"error": {"message": "offline provider unavailable"}})
                return httpx.Response(
                    200,
                    json={
                        "id": "offline-limit",
                        "object": "chat.completion",
                        "created": 1,
                        "model": spec.model_id,
                        "choices": [
                            {"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": None}}
                        ],
                        "openrouter_metadata": {
                            "requested": spec.model_id,
                            "endpoints": {
                                "available": [
                                    {
                                        "model": runner._expected_openrouter_canonical_slug(spec),
                                        "provider": provider_name_for_model(spec),
                                        "selected": True,
                                    }
                                ]
                            },
                        },
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": 0.0000001},
                    },
                )
            steps = [("akb_list_vaults", {})]
            if trial is not None and trial.task.id.startswith("stdio-image-files-"):
                steps = [
                    (
                        "akb_put_image",
                        {
                            "vault": "catalog-bench-vault-authorization",
                            "alt_text": "cleanup probe",
                            "file_path": str(tmp_path / "sample-image.png"),
                        },
                    )
                ]
            elif trial is not None and trial.task.id.startswith("table-publication-"):
                steps = [
                    ("akb_create_vault", {"name": "catalog-bench-data"}),
                    (
                        "akb_create_table",
                        {
                            "vault": "catalog-bench-data",
                            "name": "scores",
                            "columns": [{"name": "label", "type": "text"}],
                        },
                    ),
                    ("akb_sql", {"vault": "catalog-bench-data", "sql": "SELECT * FROM scores LIMIT 5"}),
                    (
                        "akb_publish",
                        {
                            "resource_type": "table_query",
                            "vault": "catalog-bench-data",
                            "query_sql": "SELECT * FROM scores LIMIT 5",
                        },
                    ),
                    ("akb_publications", {"vault": "catalog-bench-data"}),
                    ("akb_publication_snapshot", {"slug": "offline-publication"}),
                    ("akb_publications", {"vault": "catalog-bench-data"}),
                ]
            elif trial is not None and trial.task.id.startswith("collection-relation-lifecycle-"):
                steps = [
                    (
                        "akb_put",
                        {
                            "vault": "catalog-bench-relations",
                            "collection": "notes",
                            "title": "left",
                            "content": "left",
                        },
                    )
                ]
            tool_index = sum(message.get("role") == "tool" for message in body["messages"])
            done = tool_index >= len(steps)
            tool_name, arguments = steps[min(tool_index, len(steps) - 1)]
            tool_arguments = json.dumps(arguments)
            message = (
                {"role": "assistant", "content": "Done."}
                if done
                else {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-offline",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": tool_arguments},
                        }
                    ],
                }
            )
            return httpx.Response(
                200,
                json={
                    "id": "offline",
                    "object": "chat.completion",
                    "created": 1,
                    "model": spec.model_id,
                    "choices": [{"index": 0, "finish_reason": "stop" if done else "tool_calls", "message": message}],
                    "openrouter_metadata": {
                        "requested": spec.model_id,
                        "endpoints": {
                            "available": [
                                {
                                    "model": runner._expected_openrouter_canonical_slug(spec),
                                    "provider": provider_name_for_model(spec),
                                    "selected": True,
                                }
                            ]
                        },
                    },
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, "cost": 0.0000001},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        return original_openai(*args, http_client=client, **kwargs)

    monkeypatch.setattr(execution, "AsyncOpenAI", openai_client)
    outputs = {arm: tmp_path / f"{arm}.json" for arm in ("baseline", "candidate")}
    checkpoints = {arm: tmp_path / f"{arm}.checkpoint.json" for arm in outputs}
    comparison = tmp_path / "comparison.json"
    argv = [
        "run-paired",
        "--manifest",
        str(manifest_path),
        "--corpus",
        str(ROOT / "corpus/tasks.json"),
        "--coverage",
        str(ROOT / "corpus/tool-coverage.json"),
        "--baseline-descriptor",
        str(descriptors["baseline"]),
        "--candidate-descriptor",
        str(descriptors["candidate"]),
        "--baseline-output",
        str(outputs["baseline"]),
        "--candidate-output",
        str(outputs["candidate"]),
        "--comparison-output",
        str(comparison),
        "--baseline-checkpoint",
        str(checkpoints["baseline"]),
        "--candidate-checkpoint",
        str(checkpoints["candidate"]),
    ]

    def invoke_cli(resume=False):
        monkeypatch.setattr(sys, "argv", ["mcp-catalog-bench", *argv, *(["--resume"] if resume else [])])
        try:
            cli.main()
        except SystemExit as exc:
            return exc.code
        raise AssertionError("CLI did not exit")

    try:
        code = await asyncio.wait_for(asyncio.to_thread(invoke_cli), timeout=600)
        if scenario == "late_secret":
            assert len(injected) == 1
            assert code == 1
            assert all(path.exists() for path in (*outputs.values(), comparison))
            for path in outputs.values():
                text = path.read_text()
                assert "late-discovered-name" not in text
                validate_model_input_evidence(json.loads(text))
            assert any("withheld_model_input" in path.read_text() for path in outputs.values())
            assert all(json.loads(path.read_text())["status"] == "incomplete" for path in outputs.values())
            assert any(captured_seals[0] in path.read_text() for path in outputs.values())
            assert all(
                json.loads(path.read_text())["paired_budget_used"]["model_requests"] > 0 for path in outputs.values()
            )
            assert json.loads(comparison.read_text())["verdict"] == "inconclusive"
            return
        if scenario == "orphaned":
            assert injected == ["reservation"]
            assert code == 1
            for path in outputs.values():
                artifact = json.loads(path.read_text())
                assert artifact["status"] == "incomplete"
                assert "orphaned reservation" in str(artifact["incomplete_reasons"])
                assert artifact["paired_budget_used"]["final_reserved_cost_usd"] == "0"
            return
        first_comparison = json.loads(comparison.read_text())
        assert first_comparison["verdict"] == "inconclusive"
        assert first_comparison["gate"]["reasons"]
        assert "paired artifacts are missing shared execution evidence" not in str(first_comparison["gate"])
        if scenario == "normal":
            assert first_comparison["gate"]["reasons"] == ["paired registered outcomes are identical"]
        first_request_count = len(requests)
        first_documents = {arm: json.loads(path.read_text()) for arm, path in checkpoints.items()}
        first_spent = {arm: doc["spent"] for arm, doc in first_documents.items()}
        if scenario != "cancel":
            for doc in first_documents.values():
                outcomes = [record["outcome"] for record in doc["records"].values()]
                outcomes.extend(cell["outcome"] for cell in doc["smoke_gate"].values())
                for field in ("model_requests", "input_tokens", "output_tokens"):
                    assert doc["spent"][field] == sum(outcome[field] for outcome in outcomes)
                assert doc["spent"]["cost_usd"] == pytest.approx(sum(outcome["cost_usd"] for outcome in outcomes))
            paired_budget = json.loads(outputs["baseline"].read_text())["paired_budget_used"]
            for field in ("model_requests", "provider_setup_requests", "input_tokens", "output_tokens"):
                assert paired_budget[field] == sum(spent[field] for spent in first_spent.values())
            assert paired_budget["cost_usd"] == pytest.approx(sum(spent["cost_usd"] for spent in first_spent.values()))
        snapshots = {arm: path.read_bytes() for arm, path in checkpoints.items()}
        await asyncio.sleep(0.05)
        assert snapshots == {arm: path.read_bytes() for arm, path in checkpoints.items()}
        resumed_code = await asyncio.wait_for(asyncio.to_thread(invoke_cli, True), timeout=600)
        if scenario != "cancel":
            assert resumed_code == code
            assert json.loads(comparison.read_text())["gate"] == first_comparison["gate"]
            assert len(requests) == first_request_count
            assert {
                arm: json.loads(path.read_text())["spent"]["model_requests"] for arm, path in checkpoints.items()
            } == {arm: spent["model_requests"] for arm, spent in first_spent.items()}
            assert {arm: json.loads(path.read_text())["spent"]["cost_usd"] for arm, path in checkpoints.items()} == {
                arm: spent["cost_usd"] for arm, spent in first_spent.items()
            }
        else:
            assert len(requests) > first_request_count
            assert "paired execution identity is invalid or duplicated" not in str(
                json.loads(comparison.read_text())["gate"]
            )
            assert "paired artifacts are missing shared execution evidence" not in str(
                json.loads(comparison.read_text())["gate"]
            )
            assert "shared budget" not in str(json.loads(comparison.read_text())["gate"])
            assert json.loads(comparison.read_text())["gate"]["reasons"] == ["paired registered outcomes are identical"]
            baseline_artifact = json.loads(outputs["baseline"].read_text())
            candidate_artifact = json.loads(outputs["candidate"].read_text())
            altered_baseline = deepcopy(baseline_artifact)
            altered_candidate = deepcopy(candidate_artifact)
            for artifact in (altered_baseline, altered_candidate):
                artifact["resume_source"]["source_accounting"]["residual"]["model_requests"] += 1
            with pytest.raises(ValueError, match="does not match its parent receipts"):
                runner._validate_shared_budget(
                    altered_baseline,
                    altered_candidate,
                    manifest=manifest,
                    tasks=load_task_corpus(ROOT / "corpus/tasks.json"),
                    descriptors={arm: RuntimeDescriptor.from_file(path) for arm, path in descriptors.items()},
                )
            duplicated_baseline = deepcopy(baseline_artifact)
            duplicated_candidate = deepcopy(candidate_artifact)
            for artifact in (duplicated_baseline, duplicated_candidate):
                artifact["paired_execution"]["reused"].append(artifact["paired_execution"]["reused"][0])
            with pytest.raises(ValueError, match="identity is invalid or duplicated"):
                runner._validate_paired_execution_evidence(duplicated_baseline, duplicated_candidate)
            for artifact in (baseline_artifact, candidate_artifact):
                retry = next(event for event in artifact["paired_execution"]["events"] if event.get("attempt") == 2)
                retry["retry_of_sequence"] = 0
            with pytest.raises(ValueError, match="identity is invalid or duplicated"):
                runner._validate_paired_execution_evidence(baseline_artifact, candidate_artifact)
            for arm, path in checkpoints.items():
                resumed = json.loads(path.read_text())
                for key, record in first_documents[arm]["records"].items():
                    if record["status"] in {"completed", "failed"}:
                        assert resumed["records"][key] == record
    finally:
        for client in clients:
            await client.aclose()
    assert code in (0, 1)
    assert all(path.exists() for path in (*outputs.values(), comparison))
    if scenario == "normal":
        assert [json.loads(outputs[arm].read_text())["status"] for arm in ("baseline", "candidate")] == [
            "complete",
            "complete",
        ]
    else:
        assert len(injected) == 1
    for arm, path in checkpoints.items():
        doc = json.loads(path.read_text())
        assert doc["lifecycle"] == "finalized"
        expected_count = sum(len(task.fixture.transports) for task in tasks) * len(manifest.models) * manifest.repeats
        assert len(doc["records"]) == expected_count
        if scenario == "normal":
            assert all(record["status"] == "completed" for record in doc["records"].values())
        assert any(
            record["status"] == "completed" and not record["outcome"]["success"] for record in doc["records"].values()
        )
        expected = {
            (model.class_name, transport, task.id, repeat, task.locale)
            for model in manifest.models
            for transport in manifest.transports
            for task in tasks
            if transport in task.fixture.transports
            for repeat in range(1, manifest.repeats + 1)
        }
        identities = {
            (
                record["key"]["model_class"],
                record["key"]["transport"],
                record["key"]["task_id"],
                record["key"]["repeat_index"],
                record["key"]["locale"],
            )
            for record in doc["records"].values()
        }
        assert identities == expected
        assert Counter((item[0], item[1]) for item in identities) == {
            (model.class_name, transport): sum(transport in task.fixture.transports for task in tasks)
            * manifest.repeats
            for model in manifest.models
            for transport in manifest.transports
        }
        assert doc["reserved_cost_usd"] == 0
        validate_model_input_evidence(doc)
        artifact = json.loads(outputs[arm].read_text())
        assert artifact["completed_trials"] == expected_count
        if scenario != "cancel":
            assert artifact["checkpoint"]["reused_trials"] == expected_count
        assert artifact["paired_budget_used"]["final_reserved_cost_usd"] == "0"
        validate_model_input_evidence(artifact)
        if scenario in {"provider_error", "output_limit"} and arm == "candidate":
            assert any(
                record["outcome"]["failure_kind"] == ("provider" if scenario == "provider_error" else "output_limit")
                for record in doc["records"].values()
            )
    assert len(requests) >= expected_count * len(manifest.arms) * 2
