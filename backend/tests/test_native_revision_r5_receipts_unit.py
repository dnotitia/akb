from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = _BACKEND / "scripts" / f"native_revision_m1_{name}_adapter.py"
    spec = importlib.util.spec_from_file_location(f"r5_{name}_receipt_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_text_public_body_fact_contains_only_digest_and_size():
    adapter = _load("text")

    fact = adapter.public_body_fact({"content": "private body", "current_commit": "a" * 40})

    assert fact == {
        "revision": "a" * 40,
        "sha256": "aaecb569221e2e49869a9b3e5d61280a2098fb65b08bae1198e892e8f6f00aba",  # pragma: allowlist secret
        "byte_size": 12,
    }
    assert "private body" not in repr(fact)


def test_mixed_grep_receipt_redacts_matches_lines_titles_paths_and_private_ids():
    adapter = _load("mixed_grep")
    raw = {
        "pattern": "private-pattern",
        "regex": False,
        "returned_docs": 1,
        "returned_matches": 1,
        "total_docs": 1,
        "total_matches": 1,
        "truncated": False,
        "results": [{
            "uri": "akb://measure/doc/public.md",
            "path": "private/path.md",
            "title": "private title",
            "resource_id": "private-id",
            "matches": [{"text": "private matched line"}],
        }],
    }

    safe = adapter.sanitize_grep_result(raw)

    assert safe == {
        "returned_resources": 1,
        "returned_matches": 1,
        "total_resources": 1,
        "total_matches": 1,
        "truncated": False,
        "resources": [{"uri": "akb://measure/doc/public.md"}],
    }
    assert "private" not in repr(safe)


def test_w3a_uses_default_grep_request_while_w3b_explicitly_opts_in():
    adapter = _load("mixed_grep")

    assert adapter._grep_params(
        pattern="needle", vault="measure", include_text_files=False
    ) == {"q": "needle", "limit": 20, "vault": "measure"}
    assert adapter._grep_params(
        pattern="needle", vault="measure", include_text_files=True
    ) == {
        "q": "needle",
        "limit": 20,
        "vault": "measure",
        "measurement_include_text_files": True,
    }


def test_internal_text_grep_receipt_helpers_strip_private_locators_and_matches():
    adapter = _load("text_grep")

    request = adapter._safe_request_outcome(
        {
            "resource_id": "private-id",
            "path": "private/path.py",
            "surface": "file",
            "revision_id": "a" * 40,
            "byte_size": 17,
            "latency_ms": 1.2,
        }
    )
    observation = adapter._safe_grep_observation(
        {
            "total_resources": 1,
            "total_matches": 1,
            "results": [
                {
                    "uri": "akb://private/coll/private/file/private-id",
                    "path": "private/path.py",
                    "title": "private title",
                    "resource_type": "file",
                    "revision": "a" * 40,
                    "content_hash": "b" * 64,
                    "matches": [{"text": "private matched content"}],
                }
            ],
        }
    )

    assert request == {
        "surface": "file",
        "revision_id": "a" * 40,
        "byte_size": 17,
        "latency_ms": 1.2,
    }
    assert observation == {
        "total_resources": 1,
        "total_matches": 1,
        "resources": [
            {
                "resource_type": "file",
                "revision": "a" * 40,
                "content_hash": "b" * 64,
            }
        ],
    }
    assert "private" not in repr({"request": request, "observation": observation})


def test_runtime_profile_keeps_coarse_numbers_but_binds_private_inputs():
    adapter = _load("text")

    safe = adapter.receipt_safe_profile(
        {
            "cpu_count": 8,
            "memory_bytes": 1024,
            "topology": "single-node",
            "host": "private.internal",
            "token": "secret",
            "artifact_path": "/private/path",
        }
    )

    assert safe["coarse"] == {
        "cpu_count": 8,
        "memory_bytes": 1024,
        "topology": "single-node",
    }
    assert set(safe["binding"]) == {"sha256", "byte_size"}
    assert "private" not in repr(safe)
    assert "secret" not in repr(safe)


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "returned_docs": 1,
            "returned_matches": 1,
            "total_docs": 1,
            "total_matches": 1,
            "truncated": False,
            "results": [{"uri": "akb://measure/doc/public.md", "matches": []}],
        }


class _Client:
    async def request(self, *_args, **_kwargs):
        await asyncio.sleep(0.001)
        return _Response()

    async def get(self, *_args, **_kwargs):
        await asyncio.sleep(0.001)
        return _Response()


def test_concurrency_evidence_rejects_sequential_requests_inside_overlapping_worker_envelopes():
    adapter = _load("mixed_grep")
    intervals = [
        adapter.RequestInterval("writer", "write", 0.0, 1.0),
        adapter.RequestInterval("get_0", "get", 1.0, 2.0),
        adapter.RequestInterval("grep", "grep", 2.0, 3.0),
        adapter.RequestInterval("writer", "write", 3.0, 4.0),
    ]

    evidence = adapter._concurrency_evidence(intervals, get_workers=1)

    assert evidence["max_simultaneous_requests"] == 1
    assert evidence["writer_grep_overlap"] is False
    assert evidence["writer_get_overlap"] == {"writer_get_0": False}
    assert evidence["grep_get_overlap"] == {"grep_get_0": False}
    assert evidence["overlap_proven"] is False


def test_concurrency_evidence_accepts_actual_overlapping_request_intervals():
    adapter = _load("mixed_grep")
    intervals = [
        adapter.RequestInterval("writer", "write", 0.0, 3.0),
        adapter.RequestInterval("grep", "grep", 0.5, 2.5),
        adapter.RequestInterval("get_0", "get", 1.0, 2.0),
    ]

    evidence = adapter._concurrency_evidence(intervals, get_workers=1)

    assert evidence["max_simultaneous_requests"] == 3
    assert evidence["writer_grep_overlap"] is True
    assert evidence["writer_get_overlap"] == {"writer_get_0": True}
    assert evidence["grep_get_overlap"] == {"grep_get_0": True}
    assert evidence["overlap_proven"] is True


@pytest.mark.asyncio
async def test_mixed_phase_proves_every_worker_nonzero_and_actual_request_overlap():
    adapter = _load("mixed_grep")

    result = await adapter._phase(
        _Client(),
        vault="measure",
        path="public.md",
        pattern="public-pattern",
        duration=0.02,
        record=True,
        repeat=1,
        include_text_files=False,
    )

    assert set(result["successful"]) == {
        "writer",
        "grep",
        "get_0",
        "get_1",
        "get_2",
        "get_3",
    }
    assert all(count > 0 for count in result["successful"].values())
    assert result["concurrency"]["max_simultaneous_requests"] >= 2
    assert result["concurrency"]["overlap_proven"] is True
