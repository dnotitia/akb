"""Receipt contract for the public File-shaped W4 adapter."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.services.m1_binary_store import FilesystemCAS
from app.services.m1_file_measurement import MeasurementFileFacade


_BACKEND = Path(__file__).resolve().parents[1]
_SCRIPT = _BACKEND / "scripts" / "native_revision_m1_file_adapter.py"


def _adapter():
    spec = importlib.util.spec_from_file_location("native_revision_m1_file_adapter_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_w4_receipt_has_ordered_file_operations_without_capabilities(tmp_path):
    adapter = _adapter()
    files = MeasurementFileFacade(FilesystemCAS(tmp_path), base_url="https://akb.test")

    result = adapter.run_trace(files, vault="run-owned-vault", payload=b"receipt payload")

    trace = result["trace"]
    assert [item["operation"] for item in trace] == [
        "initiate", "client_transfer", "client_transfer_reuse", "confirm",
        "same_digest_retry", "get_download", "open", "delete", "open_after_delete",
    ]
    assert [item["status"] for item in trace] == [200, 200, 409, 200, 200, 200, 200, 200, 404]
    assert trace[0]["transfer_expires_in"] > 0
    assert trace[1]["transfer_one_time"] == "consumed"
    assert trace[2]["transfer_one_time"] == "rejected"
    assert trace[3]["logical_file_id"] == trace[3]["logical_file_id"]
    assert len(trace[3]["logical_revision_id"]) == 40
    assert trace[5]["downloaded_digest"] == result["digest"]
    assert trace[-1]["residue"] == "absent"
    assert "upload_url" not in str(trace)
    assert "download_url" not in str(trace)
