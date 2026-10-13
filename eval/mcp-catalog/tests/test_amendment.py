from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from mcp_catalog.amendment import (
    AmendmentPlan,
    validate_continuation_runtime_provenance,
)
from mcp_catalog.contracts import hash_json, load_run_manifest
from mcp_catalog.evidence import safe_json
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


def _plan(parent_inputs: dict[str, Any]) -> AmendmentPlan:
    plan = object.__new__(AmendmentPlan)
    plan.lineage = {"continuation_run_manifest_hash": CONTINUATION_MANIFEST_HASH}
    plan.source_pre_smoke_inputs = {"baseline": copy.deepcopy(parent_inputs)}
    plan.continuation_runtime_provenance = {}
    return plan


def _continue(
    parent_descriptor: dict[str, Any],
    current_descriptor: dict[str, Any],
    *,
    mutate_inputs: Any = None,
) -> tuple[AmendmentPlan, dict[str, Any], dict[str, Any]]:
    parent_inputs = _parent_inputs(parent_descriptor)
    current_inputs = copy.deepcopy(parent_inputs)
    current_inputs["run_manifest_hash"] = CONTINUATION_MANIFEST_HASH
    current_inputs["fixture"]["runtime_descriptor"] = safe_json(current_descriptor)
    if mutate_inputs is not None:
        mutate_inputs(current_inputs)
    plan = _plan(parent_inputs)
    plan.validate_pre_smoke_inputs(
        "baseline",
        current_inputs,
        current_descriptor_raw=current_descriptor,
        runtime=_runtime_observation(current_descriptor),
    )
    return plan, parent_inputs, current_inputs


def test_pid_reallocation_passes_without_rewriting_the_parent_seal() -> None:
    parent = _descriptor("parent")
    child = copy.deepcopy(parent)
    child["benchmark_cells"]["lightweight:stdio"]["evidence"]["process_identity"]["backend"]["pid"] += 1

    plan, parent_inputs, current_inputs = _continue(parent, child)
    provenance = plan.runtime_provenance_for("baseline")

    assert parent_inputs["fixture"]["runtime_descriptor"] == safe_json(parent)
    assert current_inputs["fixture"]["runtime_descriptor"] == safe_json(child)
    assert provenance is not None
    assert json.loads(provenance["current_descriptor_json"]) == child
    assert "/benchmark_cells/lightweight:stdio/evidence/process_identity/backend/pid" in provenance[
        "reallocated_identity_paths"
    ]
    assert provenance["parent_semantic_descriptor_sha256"] == provenance[
        "current_semantic_descriptor_sha256"
    ]


def test_parent_descriptor_cannot_be_reused_as_the_child_lease() -> None:
    parent = _descriptor("parent")
    current_inputs = copy.deepcopy(_parent_inputs(parent))
    current_inputs["run_manifest_hash"] = CONTINUATION_MANIFEST_HASH
    plan = _plan(_parent_inputs(parent))

    with pytest.raises(ValueError, match="new runtime lease"):
        plan.validate_pre_smoke_inputs(
            "baseline",
            current_inputs,
            current_descriptor_raw=parent,
            runtime=_runtime_observation(parent),
        )


def test_fresh_runtime_lease_provenance_is_recomputable() -> None:
    parent = _descriptor("old-compose")
    child = _descriptor("new-compose")
    plan, parent_inputs, _ = _continue(parent, child)
    provenance = plan.runtime_provenance_for("baseline")
    assert provenance is not None
    parsed = RuntimeDescriptor.from_dict(child)
    runtime = _runtime_observation(child)
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    runtime_cells = {
        cell: item["discovery"] for cell, item in runtime["cell_runtimes"].items()
    }
    artifact = {
        "arm": "baseline",
        "manifest": manifest.model_dump(mode="json"),
        "run_manifest_hash": CONTINUATION_MANIFEST_HASH,
        "source_revision": SOURCE_REVISION,
        "artifact_versions": ARTIFACT_VERSIONS,
        "pre_smoke_seal_inputs": parent_inputs,
        "runtime": {"cells": runtime_cells},
        "continuation_runtime_provenance": provenance,
    }

    validate_continuation_runtime_provenance(artifact)
    assert set(parsed.benchmark_cells) == set(runtime_cells)
    assert provenance["runtime_observation"]["readiness_status"] == "passed"
    assert provenance["current_descriptor_sha256"] == hash_json(child)
    assert len(provenance["reallocated_identity_paths"]) > 0


@pytest.mark.parametrize(
    "mutate_inputs",
    [
        lambda inputs: inputs.update(source_revision="b" * 40),
        lambda inputs: inputs["catalogs"]["http:default"].update(schema_hash="changed"),
        lambda inputs: inputs.update(oracle_hash="changed"),
        lambda inputs: inputs["environment"]["packages"].update(mcp="changed"),
        lambda inputs: inputs["fixture"]["runtime_identity"].update(backend_artifact_version="changed"),
        lambda inputs: inputs.update(run_manifest_hash="3" * 64),
        lambda inputs: inputs.update(provider_registry_hash="changed"),
        lambda inputs: inputs["fixture"]["reset_body"].update(scenario="changed"),
        lambda inputs: inputs["paired_order_plan"][0].update(arm_order=["candidate", "baseline"]),
        lambda inputs: inputs["fixture"]["runtime_descriptor"].update(capabilities=["stdio", "oidc"]),
        lambda inputs: inputs["fixture"]["runtime_descriptor"]["credentials"].update(
            password_env="CHANGED_PASSWORD_ENV"  # pragma: allowlist secret
        ),
        lambda inputs: inputs["fixture"]["runtime_descriptor"]["evidence"].update(
            arbitrary_runtime_id="changed"
        ),
    ],
    ids=(
        "source",
        "catalog-schema",
        "oracle",
        "package-version",
        "artifact-version",
        "budget-manifest-hash",
        "provider-snapshot",
        "reset-state",
        "paired-order",
        "runtime-capability",
        "credential-contract",
        "unknown-identity-field",
    ),
)
def test_result_affecting_near_misses_are_rejected(mutate_inputs: Any) -> None:
    parent = _descriptor("parent")
    child = _descriptor("new-runtime")
    parent_inputs = _parent_inputs(parent)
    current_inputs = copy.deepcopy(parent_inputs)
    current_inputs["run_manifest_hash"] = CONTINUATION_MANIFEST_HASH
    current_inputs["fixture"]["runtime_descriptor"] = safe_json(child)
    mutate_inputs(current_inputs)
    plan = _plan(parent_inputs)

    with pytest.raises(ValueError):
        plan.validate_pre_smoke_inputs(
            "baseline",
            current_inputs,
            current_descriptor_raw=child,
            runtime=_runtime_observation(child),
        )


def test_new_descriptor_must_report_all_processes_running() -> None:
    parent = _descriptor("parent")
    child = _descriptor("new-runtime")
    child["evidence"]["process_identity"]["backend"]["running"] = False
    current_inputs = copy.deepcopy(_parent_inputs(parent))
    current_inputs["run_manifest_hash"] = CONTINUATION_MANIFEST_HASH
    current_inputs["fixture"]["runtime_descriptor"] = safe_json(child)
    plan = _plan(_parent_inputs(parent))

    with pytest.raises(ValueError, match="running process"):
        plan.validate_pre_smoke_inputs(
            "baseline",
            current_inputs,
            current_descriptor_raw=child,
            runtime=_runtime_observation(child),
        )
