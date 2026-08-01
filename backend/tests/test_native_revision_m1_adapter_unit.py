"""Focused checks for the first-party M1 measurement adapter boundary."""

from __future__ import annotations

from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
import uuid

import pytest


_BACKEND = Path(__file__).resolve().parents[1]
_SCRIPT = _BACKEND / "scripts" / "native_revision_m1_adapter.py"
_TEXT_GREP_SCRIPT = _BACKEND / "scripts" / "native_revision_m1_text_grep_adapter.py"
_BINARY_SCRIPT = _BACKEND / "scripts" / "native_revision_m1_binary_adapter.py"


def _load_adapter():
    spec = importlib.util.spec_from_file_location("native_revision_m1_adapter", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ReceiptConnection:
    async def execute(self, *_args, **_kwargs):
        return "DELETE 0"

    async def fetchrow(self, *_args, **_kwargs):
        return {"bodies": 1, "body_bytes": 3, "distinct_digests": 1}


class _ReceiptPool:
    @asynccontextmanager
    async def acquire(self):
        yield _ReceiptConnection()

    async def close(self):
        return None


def _receipt_environment(_: str, __: str, ___: uuid.UUID):
    return (
        {
            "image_digest": "sha256:" + "1" * 64,
            "config_digest": "sha256:config",
            "source_revision": "a" * 40,
        },
        {"tier": "E0", "node_profile": {}, "storage_profile": {}},
    )


def _assert_frozen_receipt_shape(result: dict[str, object], samples: list[float]) -> None:
    """Pin the subset consumed unchanged by the frozen native runner."""
    receipt = result["receipt"]
    assert isinstance(receipt, dict)
    assert set(receipt) == {"inputs", "runtime", "environment", "latency", "resources", "requests"}
    assert receipt["latency"] == {"samples_or_artifact": samples, "unit": "ms"}


@pytest.mark.asyncio
@pytest.mark.parametrize("workload", ("W3-document-grep", "W3-text-file-grep"))
async def test_text_grep_adapter_emits_only_frozen_latency_receipt_fields(monkeypatch, workload):
    """The actual adapter result must remain acceptable to the frozen runner."""
    adapter = _load_script("native_revision_m1_text_grep_adapter_receipt_test", _TEXT_GREP_SCRIPT)
    pool = _ReceiptPool()
    owner = uuid.uuid4()
    denied = uuid.uuid4()
    reader = uuid.uuid4()
    allowed_vault = uuid.uuid4()
    denied_vault = uuid.uuid4()

    async def initialise(_dsn):
        return pool, "akb_revision_m1_measurement", owner, denied, reader, allowed_vault, denied_vault

    async def successful_workload(*_args, **_kwargs):
        return {"assertions": {"fixture": True}, "observations": {}}, [{"operation": "grep"}], [1.25]

    monkeypatch.setattr(adapter, "required_environment", lambda name: {
        "AKB_NATIVE_REVISION_WORKLOAD": workload,
        "AKB_NATIVE_REVISION_MEASUREMENT_DSN": "postgresql://measurement",
        "AKB_NATIVE_REVISION_RUN_ID": "receipt-test",
    }[name])
    monkeypatch.setattr(adapter, "initialise", initialise)
    monkeypatch.setattr(adapter, "_document_workload", successful_workload)
    monkeypatch.setattr(adapter, "_text_file_workload", successful_workload)
    monkeypatch.setattr(adapter, "source_revision", lambda: "a" * 40)
    monkeypatch.setattr(adapter, "receipt_provenance", _receipt_environment)
    monkeypatch.setattr(adapter, "run_artifact_path", lambda name: Path("/tmp") / f"{name}.json")
    monkeypatch.setattr(adapter, "write_bound_json", lambda path, _payload: {"path": str(path), "sha256": "b" * 64})

    result = await adapter.run()

    _assert_frozen_receipt_shape(result, [1.25])


@pytest.mark.asyncio
async def test_binary_adapter_emits_only_frozen_latency_receipt_fields(monkeypatch, tmp_path):
    """The W4 adapter does not widen the frozen result receipt schema either."""
    adapter = _load_script("native_revision_m1_binary_adapter_receipt_test", _BINARY_SCRIPT)
    pool = _ReceiptPool()
    owner = uuid.uuid4()
    namespace = uuid.uuid4()

    async def initialise(_dsn):
        return pool, "akb_revision_m1_measurement", owner, namespace

    async def successful_fixture(*_args, **_kwargs):
        return {"assertions": {"fixture": True}}, [2.5]

    root = tmp_path / "binary-measurement"
    root.mkdir()
    monkeypatch.setattr(adapter, "required_environment", lambda name: {
        "AKB_NATIVE_REVISION_WORKLOAD": "W4-public-file",
        "AKB_NATIVE_REVISION_BINARY_DRIVER": "fscas",
        "AKB_NATIVE_REVISION_MEASUREMENT_DSN": "postgresql://measurement",
        "AKB_NATIVE_REVISION_RUN_ID": "receipt-test",
    }[name])
    monkeypatch.setattr(adapter, "_initialise_database", initialise)
    monkeypatch.setattr(adapter, "_safe_root", lambda: root)
    monkeypatch.setattr(adapter, "_run_fixture", successful_fixture)
    monkeypatch.setattr(adapter, "source_revision", lambda: "a" * 40)
    monkeypatch.setattr(adapter, "receipt_provenance", _receipt_environment)
    monkeypatch.setattr(adapter, "run_artifact_path", lambda name: Path("/tmp") / f"{name}.json")
    monkeypatch.setattr(adapter, "write_bound_json", lambda path, _payload: {"path": str(path), "sha256": "b" * 64})

    result = await adapter.run()

    _assert_frozen_receipt_shape(result, [2.5])


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
