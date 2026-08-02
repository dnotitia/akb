#!/usr/bin/env python3
"""Public W3 exact-grep adapter under one writer plus four GET workers."""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND = SCRIPT_DIR.parent
for entry in (BACKEND, SCRIPT_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from native_revision_m1_adapter import (  # noqa: E402
    AdapterError,
    bounded_json_object,
    required_environment,
    receipt_safe_profile,
    source_revision,
)

PROTOCOL_VERSION = "akb-native-revision-m1-public-mixed-grep/v1"
WARMUP_SECONDS = 5.0
MEASUREMENT_SECONDS = 30.0
REPEATS = 3
GET_WORKERS = 4


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3)


def sanitize_grep_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only public identity and aggregate shape; never matched content."""
    safe: dict[str, Any] = {
        "returned_resources": int(raw.get("returned_docs") or raw.get("n_files") or 0),
        "returned_matches": int(raw.get("returned_matches") or 0),
        "total_resources": int(raw.get("total_docs") or raw.get("n_files") or 0),
        "total_matches": int(raw.get("total_matches") or 0),
        "truncated": bool(raw.get("truncated", False)),
    }
    resources: list[dict[str, Any]] = []
    for row in raw.get("results") or []:
        resource = {"uri": row["uri"]}
        if row.get("resource_type") == "file":
            resource.update(
                {
                    "resource_type": "file",
                    "revision": row["revision"],
                    "content_hash": row["content_hash"],
                }
            )
        resources.append(resource)
    if not resources:
        resources = [{"uri": uri} for uri in raw.get("files") or []]
    safe["resources"] = resources
    return safe


def _grep_params(*, pattern: str, vault: str | None, include_text_files: bool) -> dict[str, Any]:
    params: dict[str, Any] = {"q": pattern, "limit": 20}
    if vault is not None:
        params["vault"] = vault
    if include_text_files:
        params["measurement_include_text_files"] = True
    return params


async def _phase(
    client: httpx.AsyncClient,
    *,
    vault: str,
    path: str,
    pattern: str,
    duration: float,
    record: bool,
    repeat: int,
    include_text_files: bool,
) -> dict[str, Any]:
    endpoint = f"/api/v1/documents/{quote(vault, safe='')}/{quote(path, safe='/')}"
    stop = asyncio.Event()
    counters: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    latencies: dict[str, list[float]] = {"write": [], "get": [], "grep": []}
    spans: dict[str, list[float]] = {}
    sequence = 0
    phase_started = time.perf_counter()

    async def request(worker: str, kind: str, method: str, url: str, **kwargs):
        started = time.perf_counter()
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code >= 400:
                errors[f"{worker}:http_{response.status_code}"] += 1
            else:
                counters[worker] += 1
            return response
        except Exception as exc:  # safe class-only evidence
            errors[f"{worker}:{type(exc).__name__}"] += 1
            return None
        finally:
            if record:
                latencies[kind].append((time.perf_counter() - started) * 1000)
                completed = time.perf_counter()
                span = spans.setdefault(worker, [started, completed])
                span[0] = min(span[0], started)
                span[1] = max(span[1], completed)

    async def writer() -> None:
        nonlocal sequence
        while not stop.is_set():
            sequence += 1
            await request(
                "writer",
                "write",
                "PATCH",
                endpoint,
                json={
                    "content": f"# Mixed grep\n{pattern} repeat-{repeat} write-{sequence}\n",
                    "message": "M1 r5 mixed grep writer",
                },
            )
            await asyncio.sleep(0)

    async def getter(slot: int) -> None:
        while not stop.is_set():
            await request(f"get_{slot}", "get", "GET", endpoint)
            await asyncio.sleep(0)

    async def grepper() -> None:
        while not stop.is_set():
            await request(
                "grep",
                "grep",
                "GET",
                "/api/v1/grep",
                params=_grep_params(
                    pattern=pattern,
                    vault=vault,
                    include_text_files=include_text_files,
                ),
            )
            await asyncio.sleep(0)

    tasks = [asyncio.create_task(writer()), asyncio.create_task(grepper())]
    tasks.extend(asyncio.create_task(getter(slot)) for slot in range(GET_WORKERS))
    started = phase_started
    await asyncio.sleep(duration)
    stop.set()
    await asyncio.gather(*tasks)
    observed = time.perf_counter() - started
    exact = await client.get(
        "/api/v1/grep",
        params=_grep_params(
            pattern=pattern,
            vault=vault,
            include_text_files=include_text_files,
        ),
    )
    exact.raise_for_status()
    safe_spans = {
        worker: {
            "started_offset_seconds": round(values[0] - phase_started, 6),
            "ended_offset_seconds": round(values[1] - phase_started, 6),
        }
        for worker, values in spans.items()
    }
    writer_span = spans.get("writer")
    overlap = {
        f"writer_get_{slot}": bool(
            writer_span
            and (get_span := spans.get(f"get_{slot}"))
            and max(writer_span[0], get_span[0]) < min(writer_span[1], get_span[1])
        )
        for slot in range(GET_WORKERS)
    }
    return {
        "window_seconds": observed,
        "successful": dict(counters),
        "errors": dict(errors),
        "worker_spans": safe_spans,
        "writer_get_overlap": overlap,
        "latency": {
            kind: {
                "samples": len(values),
                "p50_ms": _percentile(values, 0.5),
                "p95_ms": _percentile(values, 0.95),
                "mean_ms": round(statistics.fmean(values), 3) if values else None,
            }
            for kind, values in latencies.items()
        },
        "exact_grep": sanitize_grep_result(exact.json()),
    }


async def _golden_suite(
    client: httpx.AsyncClient,
    *,
    vault: str,
    denied_vault: str,
    denied_uri: str,
    pattern: str,
    include_text_files: bool,
) -> dict[str, Any]:
    common = _grep_params(
        pattern=pattern,
        vault=vault,
        include_text_files=include_text_files,
    )

    async def get(**params):
        response = await client.get("/api/v1/grep", params={**common, **params})
        response.raise_for_status()
        return response.json()

    literal = await get(q=pattern)
    regex = await get(q=f"(?:{re.escape(pattern)})", regex=True)
    alternate_case = pattern.swapcase()
    case_insensitive = await get(q=alternate_case)
    case_sensitive = await get(q=alternate_case, case_sensitive=True)
    no_match = await get(q="m1-r5-deliberate-no-match-7f19c4")
    count = await get(q=pattern, count_only=True)
    files = await get(q=pattern, files_with_matches=True)
    denied = await client.get(
        "/api/v1/grep",
        params={
            **_grep_params(
                pattern=pattern,
                vault=denied_vault,
                include_text_files=include_text_files,
            ),
        },
    )
    if denied.status_code not in {200, 403}:
        denied.raise_for_status()
    denied_body = denied.json() if denied.status_code == 200 else {"results": []}
    unscoped = await client.get(
        "/api/v1/grep",
        params=_grep_params(
            pattern=pattern,
            vault=None,
            include_text_files=include_text_files,
        ),
    )
    unscoped.raise_for_status()
    unscoped_body = unscoped.json()
    assertions = {
        "literal": literal.get("total_matches", 0) > 0,
        "regex": regex.get("total_matches", 0) > 0,
        "case_insensitive": case_insensitive.get("total_matches", 0) > 0,
        "case_sensitive": (
            case_sensitive.get("total_matches", 0) == 0
            if alternate_case != pattern
            else case_sensitive.get("total_matches", 0) > 0
        ),
        "no_match": no_match.get("total_matches", 0) == 0,
        "count_shape": count.get("total_matches", 0) > 0 and isinstance(count.get("by_doc"), dict),
        "files_shape": files.get("n_files", 0) > 0 and isinstance(files.get("files"), list),
        "acl_denied": all(
            row.get("uri") != denied_uri for row in denied_body.get("results", [])
        ),
        "acl_unscoped_excluded": all(
            row.get("uri") != denied_uri for row in unscoped_body.get("results", [])
        ),
    }
    if not include_text_files:
        assertions["document_wire_frozen"] = all(
            not ({"resource_type", "revision", "content_hash"} & set(row))
            for row in literal.get("results", [])
        )
    return {
        "assertions": assertions,
        "literal": sanitize_grep_result(literal),
        "regex": sanitize_grep_result(regex),
        "case_insensitive": sanitize_grep_result(case_insensitive),
        "case_sensitive": sanitize_grep_result(case_sensitive),
        "no_match": sanitize_grep_result(no_match),
        "count": sanitize_grep_result(count),
        "files": sanitize_grep_result(files),
        "acl_status": denied.status_code,
    }


async def run() -> dict[str, Any]:
    arm = required_environment("AKB_NATIVE_REVISION_ARM")
    workload = required_environment("AKB_NATIVE_REVISION_WORKLOAD")
    if arm not in {"bare-git-current", "native-ledger"}:
        raise AdapterError("AKB_NATIVE_REVISION_ARM must be bare-git-current or native-ledger")
    if workload not in {"W3a-document-grep", "W3b-text-file-grep"}:
        raise AdapterError("unsupported mixed grep workload")
    if workload == "W3b-text-file-grep" and arm != "native-ledger":
        raise AdapterError("W3b is additive native-only evidence")
    base_url = required_environment("AKB_NATIVE_REVISION_PUBLIC_BASE_URL").rstrip("/")
    token = required_environment("AKB_NATIVE_REVISION_PUBLIC_TOKEN")
    vault = required_environment("AKB_NATIVE_REVISION_PUBLIC_VAULT")
    path = required_environment("AKB_NATIVE_REVISION_PUBLIC_DOCUMENT")
    pattern = required_environment("AKB_NATIVE_REVISION_GREP_PATTERN")
    denied_vault = required_environment("AKB_NATIVE_REVISION_DENIED_VAULT")
    denied_uri = required_environment("AKB_NATIVE_REVISION_DENIED_URI")
    expected_file_uri = (
        required_environment("AKB_NATIVE_REVISION_EXPECTED_FILE_URI")
        if workload == "W3b-text-file-grep"
        else None
    )
    headers = {"Authorization": f"Bearer {token}"}
    repeats: list[dict[str, Any]] = []
    golden: dict[str, Any] | None = None
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=15,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
    ) as client:
        for repeat in range(1, REPEATS + 1):
            warmup = await _phase(
                client,
                vault=vault,
                path=path,
                pattern=pattern,
                duration=WARMUP_SECONDS,
                record=False,
                repeat=repeat,
                include_text_files=workload == "W3b-text-file-grep",
            )
            measured = await _phase(
                client,
                vault=vault,
                path=path,
                pattern=pattern,
                duration=MEASUREMENT_SECONDS,
                record=True,
                repeat=repeat,
                include_text_files=workload == "W3b-text-file-grep",
            )
            golden = await _golden_suite(
                client,
                vault=vault,
                denied_vault=denied_vault,
                denied_uri=denied_uri,
                pattern=pattern,
                include_text_files=workload == "W3b-text-file-grep",
            )
            repeats.append(
                {
                    "repeat": repeat,
                    "warmup": {"window_seconds": warmup["window_seconds"], "errors": warmup["errors"]},
                    "measurement": measured,
                }
            )
    if any(item["measurement"]["errors"] for item in repeats):
        raise AdapterError("mixed grep public operation errors were observed")
    required_workers = {"writer", "grep", *(f"get_{slot}" for slot in range(GET_WORKERS))}
    if any(
        set(item["measurement"]["successful"]) != required_workers
        or any(item["measurement"]["successful"].get(worker, 0) < 1 for worker in required_workers)
        or not all(item["measurement"]["writer_get_overlap"].values())
        for item in repeats
    ):
        raise AdapterError("mixed grep workers did not all execute with writer/GET overlap")
    if any(item["measurement"]["exact_grep"].get("total_matches", 0) < 1 for item in repeats):
        raise AdapterError("mixed grep exact result disappeared after measurement")
    assert golden is not None
    if not all(golden["assertions"].values()):
        raise AdapterError(f"public W3 golden failed: {golden['assertions']}")
    if expected_file_uri is not None:
        for item in repeats:
            resources = item["measurement"]["exact_grep"].get("resources", [])
            matching = [row for row in resources if row.get("uri") == expected_file_uri]
            if not matching or any(
                row.get("resource_type") != "file"
                or not row.get("revision")
                or not row.get("content_hash")
                for row in matching
            ):
                raise AdapterError("W3b public grep did not preserve current text-File identity")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "source": {
            "source_revision": source_revision(),
            "image_digest": required_environment("AKB_NATIVE_REVISION_RUNTIME_IMAGE_DIGEST"),
            "config_digest": required_environment("AKB_NATIVE_REVISION_RUNTIME_CONFIG_DIGEST"),
        },
        "profile": receipt_safe_profile(
            bounded_json_object("AKB_NATIVE_REVISION_RUNTIME_PROFILE_JSON", required=True)
        ),
        "arm": arm,
        "workload": workload,
        "public_operation_ids": [
            "documentsUpdateDocument",
            "documentsGetDocument",
            "searchGrepDocuments",
        ],
        "load": {
            "warmup_seconds": WARMUP_SECONDS,
            "measurement_seconds": MEASUREMENT_SECONDS,
            "writer_workers": 1,
            "get_workers": GET_WORKERS,
            "repeats": REPEATS,
        },
        "writer_get_interference": repeats,
        "public_golden": golden,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, sort_keys=True))
