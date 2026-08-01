"""Focused checks for the first-party M1 measurement adapter boundary."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import uuid

import pytest


_BACKEND = Path(__file__).resolve().parents[1]
_SCRIPT = _BACKEND / "scripts" / "native_revision_m1_adapter.py"


def _load_adapter():
    spec = importlib.util.spec_from_file_location("native_revision_m1_adapter", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_measurement_database_guard_rejects_non_measurement_names():
    adapter = _load_adapter()

    with pytest.raises(adapter.AdapterError, match="dedicated measurement database"):
        adapter.validate_measurement_database("akb")

    adapter.validate_measurement_database("akb_revision_m1_measurement")
    adapter.validate_measurement_database("akb_revision_m1_measurement_local")


def test_bound_artifact_writes_recomputable_digest(tmp_path):
    adapter = _load_adapter()
    destination = tmp_path / "authority.json"

    bound = adapter.write_bound_json(destination, {"safe": "fact"})

    assert bound["path"] == str(destination.resolve())
    assert len(bound["sha256"]) == 64
    assert json.loads(destination.read_text()) == {"safe": "fact"}

    with pytest.raises(adapter.AdapterError, match="will not be overwritten"):
        adapter.write_bound_json(destination, {"safe": "different fact"})


def test_source_revision_environment_must_be_a_git_oid(monkeypatch):
    adapter = _load_adapter()
    monkeypatch.setenv("AKB_NATIVE_REVISION_ADAPTER_SOURCE_REVISION", "not-a-git-oid")

    with pytest.raises(adapter.AdapterError, match="exactly 40 lowercase hex"):
        adapter.source_revision()


def test_receipt_provenance_requires_explicit_runtime_identity(monkeypatch):
    adapter = _load_adapter()
    revision = "a" * 40
    monkeypatch.setenv("AKB_NATIVE_REVISION_RUNTIME_IMAGE_DIGEST", "local-image@sha256:test")
    monkeypatch.setenv("AKB_NATIVE_REVISION_RUNTIME_CONFIG_DIGEST", "sha256:config")
    runtime, environment = adapter.receipt_provenance(revision, "akb_revision_m1_measurement", uuid.uuid4())

    assert runtime == {
        "image_digest": "local-image@sha256:test",
        "config_digest": "sha256:config",
        "source_revision": revision,
    }
    assert environment["tier"] == "E0"
    assert environment["storage_profile"]["authority"] == "postgresql-native-ledger"
