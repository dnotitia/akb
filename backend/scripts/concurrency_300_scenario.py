#!/usr/bin/env python3
"""Repeatable 300-concurrent-request scenario against a live AKB backend.

This is a correctness/availability load probe, not a throughput benchmark.  It
keeps application concurrency (4 s client keep-alive expiry) separate from the
direct-Uvicorn idle-resume race (30 s expiry with 6 s idle gaps).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import sys
import time
from collections import Counter
from typing import Any, Awaitable, Callable

import httpx
from PIL import Image


Request = Callable[[int], Awaitable[httpx.Response]]
Extractor = Callable[[httpx.Response], str | None]


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index] * 1000, 2)


async def run_phase(
    *,
    name: str,
    count: int,
    request: Request,
    probe_url: str,
    extract: Extractor | None = None,
    summarize_tags: bool = False,
) -> dict[str, Any]:
    gate = asyncio.Event()
    stop_probes = asyncio.Event()
    probe_latencies: list[float] = []
    probe_statuses: Counter[int] = Counter()
    probe_errors: Counter[str] = Counter()

    async def worker(index: int) -> tuple[int | None, float, str | None, str | None]:
        await gate.wait()
        started = time.perf_counter()
        try:
            response = await request(index)
            tag = extract(response) if extract is not None else None
            return response.status_code, time.perf_counter() - started, tag, None
        except Exception as exc:  # noqa: BLE001 - transport classes are report data
            return None, time.perf_counter() - started, None, type(exc).__name__

    async def probe_loop() -> None:
        # Match the generic Kubernetes API-container liveness contract. A
        # stricter client-side SLO can still be derived from the reported
        # latency distribution without turning an accepted probe into a false
        # transport failure here.
        timeout = httpx.Timeout(8.0, connect=8.0, pool=8.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            while not stop_probes.is_set():
                started = time.perf_counter()
                try:
                    response = await client.get(probe_url)
                    probe_latencies.append(time.perf_counter() - started)
                    probe_statuses[response.status_code] += 1
                except Exception as exc:  # noqa: BLE001 - reported below
                    probe_errors[type(exc).__name__] += 1
                try:
                    await asyncio.wait_for(stop_probes.wait(), timeout=0.05)
                except TimeoutError:
                    pass

    tasks = [asyncio.create_task(worker(index)) for index in range(count)]
    probe_task = asyncio.create_task(probe_loop())
    await asyncio.sleep(0)
    phase_started = time.perf_counter()
    gate.set()
    outcomes = await asyncio.gather(*tasks)
    wall_seconds = time.perf_counter() - phase_started
    stop_probes.set()
    await probe_task

    statuses: Counter[int] = Counter()
    errors: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    durations: list[float] = []
    ordered_tags: list[str | None] = []
    for status, duration, tag, error in outcomes:
        durations.append(duration)
        ordered_tags.append(tag)
        if status is not None:
            statuses[status] += 1
        if tag is not None:
            tags[tag] += 1
        if error is not None:
            errors[error] += 1

    result = {
        "name": name,
        "requests": count,
        "wall_seconds": round(wall_seconds, 3),
        "rps": round(count / wall_seconds, 2) if wall_seconds else 0.0,
        "status": dict(sorted(statuses.items())),
        "transport_errors": dict(sorted(errors.items())),
        "latency_ms": {
            "p50": percentile(durations, 0.50),
            "p95": percentile(durations, 0.95),
            "p99": percentile(durations, 0.99),
            "max": round(max(durations, default=0.0) * 1000, 2),
        },
        "livez": {
            "samples": len(probe_latencies),
            "status": dict(sorted(probe_statuses.items())),
            "errors": dict(sorted(probe_errors.items())),
            "p99_ms": percentile(probe_latencies, 0.99),
            "max_ms": round(max(probe_latencies, default=0.0) * 1000, 2),
        },
        "tags": dict(sorted(tags.items())),
        "_ordered_tags": ordered_tags,
    }
    printable = {key: value for key, value in result.items() if not key.startswith("_")}
    if summarize_tags:
        printable["tags"] = {
            "tagged_responses": sum(tags.values()),
            "unique_values": len(tags),
        }
    print(json.dumps(printable, ensure_ascii=False, sort_keys=True), flush=True)
    return result


def assert_all(result: dict[str, Any], *, status: int) -> str | None:
    expected = {status: result["requests"]}
    if result["status"] != expected or result["transport_errors"]:
        return (
            f"{result['name']}: expected status {expected} with no transport errors, "
            f"got {result['status']} / {result['transport_errors']}"
        )
    livez = result["livez"]
    if livez["errors"] or set(livez["status"]) - {200}:
        return f"{result['name']}: livez degraded: {livez}"
    return None


def make_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color=(0, 64, 89)).save(output, format="PNG")
    return output.getvalue()


async def main(base_url: str, concurrency: int) -> int:
    suffix = f"{int(time.time())}-{time.time_ns() % 1_000_000}"
    username = f"load300-{suffix}"
    password = "load-300-secret-12"
    vault = f"load300-{suffix}"
    png = make_png()
    failures: list[str] = []
    token = ""

    setup_timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(base_url=base_url, timeout=setup_timeout) as setup:
        response = await setup.post(
            "/api/v1/auth/register",
            json={"username": username, "email": f"{username}@test.dev", "password": password},
        )
        response.raise_for_status()
        response = await setup.post(
            "/api/v1/auth/login", json={"username": username, "password": password},
        )
        response.raise_for_status()
        jwt = response.json()["token"]
        response = await setup.post(
            "/api/v1/auth/tokens",
            headers={"Authorization": f"Bearer {jwt}"},
            json={"name": "300-concurrency"},
        )
        response.raise_for_status()
        token = response.json()["token"]

    headers = {"Authorization": f"Bearer {token}"}
    limits = httpx.Limits(
        max_connections=concurrency + 20,
        max_keepalive_connections=concurrency + 20,
        keepalive_expiry=4.0,
    )
    timeout = httpx.Timeout(180.0, connect=15.0, pool=180.0)
    probe_url = f"{base_url}/livez"

    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            limits=limits,
            timeout=timeout,
        ) as client:
            response = await client.post(
                "/api/v1/vaults",
                params={"name": vault, "description": "300 concurrency scenario", "public_access": "none"},
            )
            response.raise_for_status()

            response = await client.post(
                f"/api/v1/assets/{vault}",
                params={"filename": "hot.png"},
                headers={"Content-Type": "image/png"},
                content=png,
            )
            response.raise_for_status()
            hot_asset = response.json()["id"]
            content = f"# Load document\n\nConcurrentMarker300\n\n![hot](/api/assets/{hot_asset})"
            response = await client.post(
                "/api/v1/documents",
                json={
                    "vault": vault,
                    "collection": "load",
                    "title": "Load Document",
                    "content": content,
                    "type": "note",
                },
            )
            response.raise_for_status()
            doc_url = f"/api/v1/documents/{vault}/load/load-document.md"
            asset_url = (
                f"/api/assets/{hot_asset}?vault={vault}"
                "&document=load%2Fload-document.md"
            )

            result = await run_phase(
                name="hot_asset_read_300",
                count=concurrency,
                request=lambda _index: client.get(asset_url),
                probe_url=probe_url,
                extract=lambda item: item.headers.get("content-type", "").split(";", 1)[0],
            )
            if error := assert_all(result, status=200):
                failures.append(error)
            if result["tags"] != {"image/png": concurrency}:
                failures.append(f"hot asset content types differ: {result['tags']}")

            result = await run_phase(
                name="document_read_300",
                count=concurrency,
                request=lambda _index: client.get(doc_url),
                probe_url=probe_url,
            )
            if error := assert_all(result, status=200):
                failures.append(error)

            result = await run_phase(
                name="unique_asset_upload_300",
                count=concurrency,
                request=lambda index: client.post(
                    f"/api/v1/assets/{vault}",
                    params={"filename": f"unique-{index}.png"},
                    headers={"Content-Type": "image/png"},
                    content=png,
                ),
                probe_url=probe_url,
                extract=lambda item: item.json().get("id") if item.status_code == 201 else None,
                summarize_tags=True,
            )
            if error := assert_all(result, status=201):
                failures.append(error)
            unique_assets = result["_ordered_tags"]
            if len(set(unique_assets)) != concurrency or None in unique_assets:
                failures.append("unique asset uploads did not return 300 distinct ids")

            result = await run_phase(
                name="unique_asset_read_300",
                count=concurrency,
                request=lambda index: client.get(
                    f"/api/assets/{unique_assets[index]}?vault={vault}"
                ),
                probe_url=probe_url,
                extract=lambda item: item.headers.get("content-type", "").split(";", 1)[0],
            )
            if error := assert_all(result, status=200):
                failures.append(error)
            if result["tags"] != {"image/png": concurrency}:
                failures.append(f"unique asset content types differ: {result['tags']}")

            async def mixed_request(index: int) -> httpx.Response:
                if index % 3 == 0:
                    return await client.get(doc_url)
                if index % 3 == 1:
                    return await client.get(f"/api/v1/browse/{vault}", params={"depth": 2})
                return await client.get(
                    "/api/v1/search",
                    params={"q": "ConcurrentMarker300", "vault": vault, "limit": 5},
                )

            result = await run_phase(
                name="mixed_document_browse_search_300",
                count=concurrency,
                request=mixed_request,
                probe_url=probe_url,
            )
            if error := assert_all(result, status=200):
                failures.append(error)

            result = await run_phase(
                name="unique_asset_discard_300",
                count=concurrency,
                request=lambda index: client.delete(
                    f"/api/v1/assets/{vault}/{unique_assets[index]}"
                ),
                probe_url=probe_url,
                extract=lambda item: f"discarded:{str(item.json().get('discarded')).lower()}",
            )
            if error := assert_all(result, status=200):
                failures.append(error)
            if result["tags"] != {"discarded:true": concurrency}:
                failures.append(f"unique discard results differ: {result['tags']}")

            response = await client.get(doc_url)
            response.raise_for_status()
            expected_commit = response.json()["current_commit"]
            result = await run_phase(
                name="same_expected_commit_patch_300",
                count=concurrency,
                request=lambda index: client.patch(
                    doc_url,
                    json={
                        "content": f"# Winning body\n\ncomplete-candidate-{index}",
                        "expected_commit": expected_commit,
                    },
                ),
                probe_url=probe_url,
            )
            expected_occ = {200: 1, 409: concurrency - 1}
            if result["status"] != expected_occ or result["transport_errors"]:
                failures.append(
                    f"OCC expected {expected_occ} with no transport errors, "
                    f"got {result['status']} / {result['transport_errors']}"
                )
            response = await client.get(doc_url)
            response.raise_for_status()
            winning_body = response.json()["content"]
            if not winning_body.startswith("# Winning body\n\ncomplete-candidate-"):
                failures.append("OCC final body is not one complete candidate")

            response = await client.post(
                f"/api/v1/assets/{vault}",
                params={"filename": "same-discard.png"},
                headers={"Content-Type": "image/png"},
                content=png,
            )
            response.raise_for_status()
            discard_asset = response.json()["id"]
            result = await run_phase(
                name="same_asset_discard_300",
                count=concurrency,
                request=lambda _index: client.delete(
                    f"/api/v1/assets/{vault}/{discard_asset}"
                ),
                probe_url=probe_url,
                extract=lambda item: f"discarded:{str(item.json().get('discarded')).lower()}",
            )
            if error := assert_all(result, status=200):
                failures.append(error)
            expected_discard = {"discarded:false": concurrency - 1, "discarded:true": 1}
            if result["tags"] != expected_discard:
                failures.append(
                    f"same-asset discard expected {expected_discard}, got {result['tags']}"
                )

        long_limits = httpx.Limits(
            max_connections=concurrency + 20,
            max_keepalive_connections=concurrency + 20,
            keepalive_expiry=30.0,
        )
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            limits=long_limits,
            timeout=timeout,
        ) as long_client:
            for cycle in range(1, 4):
                result = await run_phase(
                    name=f"idle_resume_cycle_{cycle}_300",
                    count=concurrency,
                    request=lambda _index: long_client.get(doc_url),
                    probe_url=probe_url,
                )
                if error := assert_all(result, status=200):
                    failures.append(error)
                if cycle < 3:
                    await asyncio.sleep(6.0)

    finally:
        if token:
            cleanup_headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(
                base_url=base_url, headers=cleanup_headers, timeout=setup_timeout,
            ) as cleanup:
                await cleanup.delete(f"/api/v1/vaults/{vault}")
                await cleanup.delete("/api/v1/my/account")

    summary = {
        "concurrency": concurrency,
        "failures": failures,
        "result": "PASS" if not failures else "FAIL",
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if not failures else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=300)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be positive")
    sys.exit(asyncio.run(main(args.base_url.rstrip("/"), args.concurrency)))
