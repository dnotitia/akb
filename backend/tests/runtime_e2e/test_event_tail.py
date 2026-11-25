"""Event Tail contracts over open HTTP streams backed by real PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from time import monotonic

import pytest

from .event_tail import (
    assert_document_change,
    assert_document_write,
    assert_filtered,
    assert_resumed,
    event_tail_mode,
    iter_frames,
    retained_cursor,
)


@pytest.fixture
async def tail_auth(create_actor):
    try:
        native, owner_token, outsider_token = event_tail_mode(os.environ)
    except ValueError as exc:
        pytest.fail(str(exc))
    if not owner_token:
        owner_token = (await create_actor("tail-owner")).jwt
        outsider_token = (await create_actor("tail-outsider")).jwt
    return (
        native,
        {"Authorization": f"Bearer {owner_token}"},
        {"Authorization": f"Bearer {outsider_token}"},
    )


@asynccontextmanager
async def open_tail(api, vault, headers, *, params=None):
    async with api.stream("GET", f"/api/v1/events/{vault}", headers=headers, params=params) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        yield iter_frames(response.aiter_bytes())


async def collect_replay(api, vault, headers, *, params=None):
    frames = []
    async with open_tail(api, vault, headers, params=params) as stream:
        # The replay request remains an open tail after the backlog.
        try:
            async with asyncio.timeout(2):
                async for frame in stream:
                    assert frame.event != "heartbeat", "heartbeat arrived before the replay window ended"
                    frames.append(frame)
        except TimeoutError:
            pass
        else:
            pytest.fail("the tail closed before the replay window ended")
    return frames


async def write_document(api, headers, vault, slug):
    response = await api.post(
        "/api/v1/documents", headers=headers,
        json={
            "vault": vault, "collection": "tail", "slug": slug, "title": slug,
            "content": f"Event Tail fixture for {slug}", "status": "active",
        },
    )
    assert response.status_code in (200, 201)
    write = response.json()
    assert_document_write(write, vault, slug)
    return write


async def test_retained_events_filter_checkpoint_and_resume(api, create_vault, tail_auth):
    native, owner, _ = tail_auth
    vault = await create_vault(owner, "tail-replay")
    first = await write_document(api, owner, vault, "first")
    table = await api.post(
        f"/api/v1/tables/{vault}", headers=owner,
        json={"name": "items", "columns": [{"name": "sku", "type": "text"}]},
    )
    assert table.status_code == 200
    assert table.json()["kind"] == "table"
    rows = await api.post(
        f"/api/v1/tables/{vault}/items/rows",
        headers={**owner, "Prefer": "return=representation"}, json=[{"sku": "one"}],
    )
    assert rows.status_code == 201
    assert rows.json()["kind"] == "table_query"

    filtered = await collect_replay(
        api, vault, owner, params={"start": "earliest", "kind": "table.rows_changed"},
    )
    assert_filtered(filtered, vault)

    retained = await collect_replay(api, vault, owner, params={"start": "earliest"})
    last_cursor = retained_cursor(retained, first, vault, native=native)
    second = await write_document(api, owner, vault, "second")
    resumed = await collect_replay(api, vault, {**owner, "Last-Event-ID": last_cursor})
    assert_resumed(resumed, second, vault, last_cursor, native=native)

    resume_cursor = resumed[-1].cursor
    third = await write_document(api, owner, vault, "third")
    query_resumed = await collect_replay(api, vault, owner, params={"cursor": resume_cursor})
    assert_resumed(query_resumed, third, vault, resume_cursor, native=native)


async def test_tail_rejects_invalid_cursors_and_non_members(api, create_vault, tail_auth):
    _, owner, outsider = tail_auth
    vault = await create_vault(owner, "tail-access")
    await write_document(api, owner, vault, "private")
    async with api.stream(
        "GET", f"/api/v1/events/{vault}", headers=owner, params={"cursor": "invalid"},
    ) as invalid:
        assert invalid.status_code == 400
        await invalid.aread()
        assert invalid.json()["detail"]["code"] == "invalid_event_cursor"

    async with api.stream(
        "GET", f"/api/v1/events/{vault}", headers=outsider, params={"start": "earliest"},
    ) as denied:
        assert denied.status_code == 403
        if b"document.put" in await denied.aread():
            pytest.fail("denied response revealed a private event")


async def next_change(stream):
    async with asyncio.timeout(30):
        async for frame in stream:
            if frame.event != "heartbeat":
                return frame
    pytest.fail("SSE stream closed before the expected change")


async def test_cursor_free_tail_waits_for_new_writes(api, create_vault, tail_auth):
    native, owner, _ = tail_auth
    vault = await create_vault(owner, "tail-live")
    await write_document(api, owner, vault, "already-retained")
    started = monotonic()
    async with open_tail(api, vault, owner) as stream:
        async with asyncio.timeout(20):
            assert (await anext(stream)).event == "heartbeat"
        assert monotonic() - started <= 20
        write = await write_document(api, owner, vault, "live")
        assert_document_change(await next_change(stream), write, vault, native=native)


def busiest_gap(opened, closed, writes):
    # NOTIFY landed somewhere between the start and finish of each fully
    # enclosed write. These gaps are the longest it could have gone quiet.
    inside = [(start, finish) for start, finish in writes if opened <= start and finish <= closed]
    if not inside:
        return 0, closed - opened
    gaps = [inside[0][1] - opened, closed - inside[-1][0]]
    gaps.extend(later[1] - earlier[0] for earlier, later in zip(inside, inside[1:]))
    return len(inside), max(gaps)


async def test_other_vault_writes_cannot_starve_heartbeat(api, create_vault, tail_auth):
    native, owner, _ = tail_auth
    quiet = await create_vault(owner, "tail-quiet")
    busy = await create_vault(owner, "tail-busy")
    writes = []
    stop = asyncio.Event()

    async def keep_writing():
        sequence = 0
        while not stop.is_set():
            sequence += 1
            started = monotonic()
            await write_document(api, owner, busy, f"heartbeat-busy-{sequence}")
            writes.append((started, monotonic()))
            delay = max(0, 2.5 - (monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except TimeoutError:
                continue

    windows = []
    async with asyncio.TaskGroup() as tasks:
        # Begin writes before the connection, including when headers are
        # buffered until the first frame by an intermediary.
        writer = tasks.create_task(keep_writing())
        try:
            opened = monotonic()
            async with open_tail(api, quiet, owner) as stream:
                while len(windows) < 8:
                    try:
                        async with asyncio.timeout(max(0, opened + 20 - monotonic())):
                            frame = await anext(stream)
                    except TimeoutError:
                        pytest.fail(f"SSE heartbeat exceeded 20s; earlier windows={windows}, writes={len(writes)}")
                    received = monotonic()
                    assert frame.event == "heartbeat", "quiet Vault received a non-heartbeat"
                    count, worst_gap = busiest_gap(opened, received, writes)
                    windows.append((received - opened, count, worst_gap))
                    if sum(count >= 2 and gap < 15 for _, count, gap in windows) >= 3:
                        break
                    opened = received
                else:
                    pytest.fail(f"fewer than three heartbeat windows stayed busy; windows={windows}")

                stop.set()
                await writer
                assert all(elapsed <= 20 for elapsed, _, _ in windows)
                target = await write_document(api, owner, quiet, "target-after-heartbeat")
                assert_document_change(await next_change(stream), target, quiet, native=native)
        finally:
            stop.set()
