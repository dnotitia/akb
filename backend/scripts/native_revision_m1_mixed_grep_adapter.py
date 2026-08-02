#!/usr/bin/env python3
"""Public W3 exact-grep adapter under one writer plus four GET workers."""

from __future__ import annotations

import asyncio
import json
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


async def _phase(
    client: httpx.AsyncClient,
    *,
    vault: str,
    path: str,
    pattern: str,
    duration: float,
    record: bool,
    repeat: int,
) -> dict[str, Any]:
    endpoint = f"/api/v1/documents/{quote(vault, safe='')}/{quote(path, safe='/')}"
    stop = asyncio.Event()
    counters: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    latencies: dict[str, list[float]] = {"write": [], "get": [], "grep": []}
    sequence = 0

    async def request(kind: str, method: str, url: str, **kwargs):
        started = time.perf_counter()
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code >= 400:
                errors[f"{kind}:http_{response.status_code}"] += 1
            else:
                counters[kind] += 1
            return response
        except Exception as exc:  # safe class-only evidence
            errors[f"{kind}:{type(exc).__name__}"] += 1
            return None
        finally:
            if record:
                latencies[kind].append((time.perf_counter() - started) * 1000)

    async def writer() -> None:
        nonlocal sequence
        while not stop.is_set():
            sequence += 1
            await request(
                "write",
                "PATCH",
                endpoint,
                json={
                    "content": f"# Mixed grep\n{pattern} repeat-{repeat} write-{sequence}\n",
                    "message": "M1 r5 mixed grep writer",
                },
            )
            await asyncio.sleep(0)

    async def getter() -> None:
        while not stop.is_set():
            await request("get", "GET", endpoint)
            await asyncio.sleep(0)

    async def grepper() -> None:
        while not stop.is_set():
            await request(
                "grep",
                "GET",
                "/api/v1/grep",
                params={"q": pattern, "vault": vault, "limit": 20},
            )
            await asyncio.sleep(0)

    tasks = [asyncio.create_task(writer()), asyncio.create_task(grepper())]
    tasks.extend(asyncio.create_task(getter()) for _ in range(GET_WORKERS))
    started = time.perf_counter()
    await asyncio.sleep(duration)
    stop.set()
    await asyncio.gather(*tasks)
    observed = time.perf_counter() - started
    exact = await client.get(
        "/api/v1/grep",
        params={"q": pattern, "vault": vault, "limit": 20},
    )
    exact.raise_for_status()
    return {
        "window_seconds": observed,
        "successful": dict(counters),
        "errors": dict(errors),
        "latency": {
            kind: {
                "samples": len(values),
                "p50_ms": _percentile(values, 0.5),
                "p95_ms": _percentile(values, 0.95),
                "mean_ms": round(statistics.fmean(values), 3) if values else None,
            }
            for kind, values in latencies.items()
        },
        "exact_grep": exact.json(),
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
    expected_file_uri = (
        required_environment("AKB_NATIVE_REVISION_EXPECTED_FILE_URI")
        if workload == "W3b-text-file-grep"
        else None
    )
    headers = {"Authorization": f"Bearer {token}"}
    repeats: list[dict[str, Any]] = []
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
            )
            measured = await _phase(
                client,
                vault=vault,
                path=path,
                pattern=pattern,
                duration=MEASUREMENT_SECONDS,
                record=True,
                repeat=repeat,
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
    if any(item["measurement"]["exact_grep"].get("total_matches", 0) < 1 for item in repeats):
        raise AdapterError("mixed grep exact result disappeared after measurement")
    if expected_file_uri is not None:
        for item in repeats:
            resources = item["measurement"]["exact_grep"].get("results", [])
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
        "profile": bounded_json_object("AKB_NATIVE_REVISION_RUNTIME_PROFILE_JSON", required=True),
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
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, sort_keys=True))
