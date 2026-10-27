from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import mcp_catalog.historical_continuation as continuation
from mcp_catalog.contracts import hash_json, load_run_manifest
from mcp_catalog.evidence import canonical_json, safe_json
from mcp_catalog.runtime import RuntimeDescriptor


ROOT = Path(__file__).parents[1]
SOURCE_REVISION = "a" * 40
SOURCE_MANIFEST_HASH = "1" * 64
CONTINUATION_MANIFEST_HASH = "2" * 64
ARTIFACT_VERSIONS = {"backend_artifact_version": "backend-1", "proxy_artifact_version": "proxy-1"}
CELLS = ("primary:http", "primary:stdio", "lightweight:http", "lightweight:stdio")


def _process_identity(lease: str, cell: str) -> dict[str, dict[str, Any]]:
    base = sum(ord(char) for char in f"{lease}:{cell}")
    return {
        name: {"pid": base + offset, "running": True}
        for offset, name in enumerate(("backend", "embed", "stdio"), start=1000)
    }


def _dependency_identity(lease: str, cell: str) -> dict[str, Any]:
    network = f"network-{lease}-{cell}"
    return {
        "services": {
            service: {
                "container_id": f"{lease}-{cell}-{service}-container",
                "network_ids": [network],
                "volume_names": [f"compose-{lease}-{cell}-{service}-data"],
            }
            for service in ("postgres", "minio")
        }
    }


def _cell_evidence(lease: str, cell: str) -> dict[str, Any]:
    consumer_root = f"/tmp/akb361-{lease}/cells/{cell.replace(':', '-')}/node-consumer"
    return {
        "source_revision": SOURCE_REVISION,
        **ARTIFACT_VERSIONS,
        "credential_env": {"username": "AKB_E2E_USERNAME", "password": "AKB_E2E_PASSWORD"},  # pragma: allowlist secret
        "dependency_identity": _dependency_identity(lease, cell),
        "fixture": {"scenario": "empty", "namespace": f"fixture-{lease}-{cell}"},
        "process_identity": _process_identity(lease, cell),
        "stdio": {"consumer_root": consumer_root},
    }


def _cell_descriptor(lease: str, cell: str) -> dict[str, Any]:
    scenario = "empty"
    app_origin = "http://127.0.0.1:8000"
    fixture_origin = "http://127.0.0.1:8889"
    consumer_root = f"/tmp/akb361-{lease}/cells/{cell.replace(':', '-')}/node-consumer"
    return {
        "schema_version": 2,
        "status": "ready",
        "scenario": scenario,
        "services": {
            "app": {
                "origin": app_origin,
                "health": {"method": "GET", "url": "/readyz"},
                "discovery": {"method": "GET", "url": "/openapi.json"},
            },
            "fixture": {
                "origin": fixture_origin,
                "health": {"method": "GET", "url": "/health"},
                "reset": {"method": "POST", "url": "/reset", "body": {"scenario": scenario}},
                "discovery": {"method": "GET", "url": "/discover"},
            },
            "stdio": {
                "transport": "stdio",
                "executable": "akb-mcp",
                "consumer_root": consumer_root,
                "environment": {"AKB_MCP_URL": f"{app_origin}/mcp/", "AKB_PAT": "AKB_E2E_PAT"},
            },
        },
        "credentials": {
            "username_env": "AKB_E2E_USERNAME",
            "password_env": "AKB_E2E_PASSWORD",  # pragma: allowlist secret
            "pat_env": "AKB_E2E_PAT",
        },
        "profile": "transport-proxy",
        "capabilities": ["stdio"],
        "evidence": _cell_evidence(lease, cell),
    }


def _descriptor(lease: str) -> dict[str, Any]:
    cells = {cell: _cell_descriptor(lease, cell) for cell in CELLS}
    evidence_cells = {cell: copy.deepcopy(value["evidence"]) for cell, value in cells.items()}
    result = copy.deepcopy(cells["primary:http"])
    result["benchmark_cells"] = cells
    result["evidence"]["benchmark_cells"] = evidence_cells
    return result


def _parent_inputs(parent_descriptor: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "arm": "baseline",
        "source_revision": SOURCE_REVISION,
        "run_manifest_hash": SOURCE_MANIFEST_HASH,
        "task_corpus_hash": "3" * 64,
        "oracle_hash": "4" * 64,
        "catalogs": {"http:default": {"schema_hash": "catalog-schema"}},
        "fixture": {
            "scenario": "empty",
            "reset_url": "http://127.0.0.1:8889/reset",
            "reset_body": {"scenario": "empty"},
            "runtime_descriptor": safe_json(parent_descriptor),
            "runtime_identity": ARTIFACT_VERSIONS,
        },
        "environment": {
            "python": "3.14.0",
            "platform": "test-platform",
            "packages": {"mcp": "1.2.3"},
            "uv_lock_sha256": "5" * 64,
        },
        "paired_order_plan": [{"task_id": "sample", "repeat_index": 1, "arm_order": ["baseline", "candidate"]}],
        "provider_registry_hash": "6" * 64,
        "provider_registry_status": "verified",
    }


def _runtime_observation(descriptor_raw: dict[str, Any]) -> dict[str, Any]:
    descriptor = RuntimeDescriptor.from_dict(descriptor_raw)
    cell_runtimes: dict[str, Any] = {}
    for cell in CELLS:
        cell_descriptor = descriptor.cell_for(cell)
        evidence = cell_descriptor.raw["evidence"]
        discovery = {
            "runtime": {
                "source_revision": evidence["source_revision"],
                **ARTIFACT_VERSIONS,
            }
        }
        cell_runtimes[cell] = {
            "source_revision": SOURCE_REVISION,
            "artifact_versions": ARTIFACT_VERSIONS,
            "discovery": discovery,
        }
    return {
        "source_revision": SOURCE_REVISION,
        "artifact_versions": ARTIFACT_VERSIONS,
        "cell_runtimes": cell_runtimes,
    }


def _historical_artifact() -> tuple[dict[str, Any], dict[str, Any]]:
    parent = _descriptor("old-compose")
    child = _descriptor("new-compose")
    parent_inputs = _parent_inputs(parent)
    manifest = load_run_manifest(ROOT / "config" / "run.json").model_dump(mode="json")
    continuation_hash = hash_json(manifest)
    current_inputs = copy.deepcopy(parent_inputs)
    current_inputs["run_manifest_hash"] = continuation_hash
    current_inputs["fixture"]["runtime_descriptor"] = safe_json(child)
    parent_descriptor = parent_inputs["fixture"]["runtime_descriptor"]
    current_descriptor = current_inputs["fixture"]["runtime_descriptor"]
    parent_projection, child_projection, parent_identity, child_identity, changed_paths = (
        continuation._compare_runtime_descriptors(parent_descriptor, current_descriptor)
    )
    runtime = _runtime_observation(child)
    runtime_cells = {
        cell: item["discovery"] for cell, item in runtime["cell_runtimes"].items()
    }
    observations = {
        cell: {
            "readiness_status": "passed",
            "source_revision": SOURCE_REVISION,
            "artifact_versions": ARTIFACT_VERSIONS,
            "discovery_sha256": hash_json(discovery),
        }
        for cell, discovery in runtime_cells.items()
    }
    provenance = {
        "schema_version": 1,
        "parent_descriptor_sha256": hash_json(parent_descriptor),
        "current_descriptor_sha256": hash_json(child),
        "current_descriptor": safe_json(child),
        "current_descriptor_json": canonical_json(child),
        "parent_semantic_descriptor_sha256": hash_json(parent_projection),
        "current_semantic_descriptor_sha256": hash_json(child_projection),
        "parent_runtime_identity_sha256": hash_json(parent_identity),
        "current_runtime_identity_sha256": hash_json(child_identity),
        "reallocated_identity_paths": changed_paths,
        "current_pre_smoke_inputs_sha256": hash_json(current_inputs),
        "runtime_observation": {
            "readiness_status": "passed",
            "source_revision": SOURCE_REVISION,
            "artifact_versions": ARTIFACT_VERSIONS,
            "cells": observations,
        },
    }
    source_manifest = copy.deepcopy(manifest)
    source_manifest["budget"].update(
        {"max_model_requests": 3000, "max_wall_seconds": 10800, "max_requests_per_trial": 24}
    )
    artifact = {
        "arm": "baseline",
        "manifest": manifest,
        "run_manifest_hash": continuation_hash,
        "source_revision": SOURCE_REVISION,
        "artifact_versions": ARTIFACT_VERSIONS,
        "pre_smoke_seal_inputs": parent_inputs,
        "runtime": {"cells": runtime_cells},
        "continuation_runtime_provenance": provenance,
        "paired_budget_used": {"max_total_cost_usd": 50.0, "cost_usd": 5.0},
    }
    return artifact, source_manifest


def _with_lineage(artifact: dict[str, Any], source_manifest: dict[str, Any]) -> dict[str, Any]:
    artifact["continuation_lineage"] = {
        "schema_version": 1,
        "work_item": continuation.WORK_ITEM,
        "authorization_revision": continuation.AUTHORIZATION_REVISION,
        "authorization_content_hash": continuation.AUTHORIZATION_CONTENT_HASH,
        "source_run_manifest_hash": hash_json(source_manifest),
        "continuation_run_manifest_hash": artifact["run_manifest_hash"],
        "source_task_corpus_hash": continuation.SOURCE_TASK_CORPUS_HASH,
        "source_pre_smoke_seal_hash": continuation.SOURCE_PRE_SMOKE_SEAL_HASH,
        "source_manifest": source_manifest,
        "parent_files": {
            arm: {
                "artifact_sha256": continuation.SOURCE_ARTIFACT_SHA256[arm],
                "checkpoint_sha256": continuation.SOURCE_CHECKPOINT_SHA256[arm],
                "terminal_outcomes": continuation.SOURCE_TERMINAL_COUNTS[arm],
            }
            for arm in continuation.ARMS
        },
        "parent_terminal_outcomes": continuation.SOURCE_PAIRED_EVENT_COUNT,
        "newly_authorized_outcomes": 13,
        "accounting": {
            "account_limit_usd": str(continuation.ACCOUNT_LIMIT_USD),
            "prior_account_observation_usd": str(continuation.PRIOR_ACCOUNT_OBSERVATION_USD),
            "parent_attempt_cost_usd": str(continuation.SOURCE_COST_USD),
            "continuation_run_cap_usd": str(continuation.CONTINUATION_RUN_CAP_USD),
            "available_continuation_cost_usd": str(continuation.CONTINUATION_ALLOWANCE_USD),
        },
        "pending_identities": [
            {"cell": cell, "task_id": task_id, "repeat_index": repeat, "arm": arm}
            for cell, task_id, repeat, arm in sorted(continuation.EXPECTED_MISSING_IDENTITIES)
        ],
    }
    return artifact


def test_historical_continuation_artifact_lineage_and_runtime_are_reproducible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact, source_manifest = _historical_artifact()
    source_hash = hash_json(source_manifest)
    monkeypatch.setattr(continuation, "SOURCE_MANIFEST_HASH", source_hash)
    _with_lineage(artifact, source_manifest)

    assert continuation.validate_artifact_lineage(artifact) == source_hash
    descriptor_json = artifact["continuation_runtime_provenance"]["current_descriptor_json"]
    assert set(RuntimeDescriptor.from_dict(json.loads(descriptor_json)).benchmark_cells) == set(
        artifact["runtime"]["cells"]
    )


@pytest.mark.parametrize("tampering", ("pending", "manifest", "runtime"))
def test_historical_continuation_artifact_tampering_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tampering: str,
) -> None:
    artifact, source_manifest = _historical_artifact()
    source_hash = hash_json(source_manifest)
    monkeypatch.setattr(continuation, "SOURCE_MANIFEST_HASH", source_hash)
    _with_lineage(artifact, source_manifest)

    if tampering == "pending":
        artifact["continuation_lineage"]["pending_identities"].pop()
    elif tampering == "manifest":
        artifact["manifest"]["budget"]["max_cost_per_trial_usd"] = 0.2
        artifact["run_manifest_hash"] = hash_json(artifact["manifest"])
        artifact["continuation_lineage"]["continuation_run_manifest_hash"] = artifact["run_manifest_hash"]
    else:
        artifact["continuation_runtime_provenance"]["current_descriptor_sha256"] = "0" * 64

    with pytest.raises(ValueError):
        continuation.validate_artifact_lineage(artifact)


def test_historical_continuation_requires_a_recorded_lineage() -> None:
    with pytest.raises(ValueError, match="lineage is missing"):
        continuation.validate_artifact_lineage({})


def test_historical_continuation_runtime_rejects_missing_provenance() -> None:
    with pytest.raises(ValueError, match="provenance or parent seal is missing"):
        continuation.validate_continuation_runtime_provenance({})
