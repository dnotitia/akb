"""Endpoint-side parsing and assertions for the Event Tail shell E2E."""
from __future__ import annotations

import json
import os
import ssl
import sys
import threading
import time
import urllib.request


def assert_filtered_events(path: str) -> None:
    with open(path, encoding="utf-8") as source:
        text = source.read()
    frames = []
    current = {}
    for line in text.splitlines():
        if not line:
            if current:
                frames.append(current)
                current = {}
            continue
        if line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("id: "):
            current["id"] = line[4:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
    if current:
        frames.append(current)

    changes = [frame for frame in frames if frame.get("event") == "change"]
    checkpoints = [frame for frame in frames if frame.get("event") == "checkpoint"]
    assert len(changes) == 1, (frames, "expected exactly one selected change")
    assert checkpoints, (frames, "excluded producer events must advance by checkpoint")
    change = changes[0]
    assert change["id"] == change["data"]["cursor"]
    assert change["data"]["version"] == 1
    assert change["data"]["vault"].startswith("event-tail-")
    assert change["data"]["kind"] == "table.rows_changed"
    assert "vault_id" not in change["data"]
    assert "redis" not in text.lower()


def assert_all_events(path: str) -> None:
    with open(path, encoding="utf-8") as source:
        text = source.read()
    events = []
    for block in text.split("\n\n"):
        rows = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if rows.get("event") == "change":
            events.append(json.loads(rows["data"]))
    assert len(events) >= 3, events
    assert {item["kind"] for item in events} >= {"document.put", "table.create", "table.rows_changed"}
    assert all(item["cursor"] for item in events)
    assert "redis" not in text.lower()


def assert_resumed_events(path: str) -> None:
    with open(path, encoding="utf-8") as source:
        text = source.read()
    changes = []
    for block in text.split("\n\n"):
        rows = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if rows.get("event") == "change":
            changes.append(json.loads(rows["data"]))
    assert changes, text
    assert all(item["kind"] == "document.put" for item in changes), changes
    assert "Second document" in text
    assert "redis" not in text.lower()


def assert_heartbeat_timing() -> None:
    base_url = os.environ["AKB_URL"].rstrip("/")
    token = os.environ["AKB_TOKEN"]
    quiet_vault = os.environ["AKB_QUIET_VAULT"]
    busy_vault = os.environ["AKB_BUSY_VAULT"]
    context = ssl._create_unverified_context() if base_url.startswith("https:") else None

    def open_response(url, *, method="GET", payload=None, timeout=12):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Authorization": f"Bearer {token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        return urllib.request.urlopen(request, context=context, timeout=timeout)

    def read_frame(response):
        lines = []
        while True:
            line = response.readline()
            if not line:
                raise AssertionError("SSE stream closed before the expected frame")
            if line in (b"\n", b"\r\n"):
                if lines:
                    return [item.decode("utf-8") for item in lines]
                continue
            lines.append(line.rstrip(b"\r\n"))

    def open_tail(vault, timeout=22):
        started = time.monotonic()
        response = open_response(
            f"{base_url}/api/v1/events/{vault}", timeout=timeout
        )
        assert response.headers.get("Content-Type", "").startswith("text/event-stream")
        return response, started

    def write_document(vault, title, slug):
        with open_response(
            f"{base_url}/api/v1/documents",
            method="POST",
            payload={
                "vault": vault,
                "collection": "tail",
                "title": title,
                "slug": slug,
                "content": title,
                "status": "active",
            },
        ) as response:
            result = json.loads(response.read())
        assert result.get("kind") == "document_write", result
        return result

    # No writer is running while the first heartbeat establishes the quiet baseline.
    baseline_stream, baseline_started = open_tail(quiet_vault)
    try:
        baseline_frame = read_frame(baseline_stream)
        baseline_received = time.monotonic()
    finally:
        baseline_stream.close()
    baseline_gap = baseline_received - baseline_started
    assert baseline_frame == [": heartbeat"], baseline_frame
    assert baseline_gap <= 20, f"quiet baseline heartbeat arrived after {baseline_gap:.3f}s"
    print(f"quiet baseline: first comment at {baseline_gap:.3f}s with no writes")

    busy_writes = []  # (started, finished) monotonic pairs; the NOTIFY lands in between
    writer_errors = []
    stop_writer = threading.Event()

    def keep_busy_vault_changing():
        sequence = 0
        next_write_at = time.monotonic()
        while not stop_writer.is_set():
            delay = next_write_at - time.monotonic()
            if delay > 0 and stop_writer.wait(delay):
                return
            sequence += 1
            write_started = time.monotonic()
            try:
                write_document(
                    busy_vault,
                    f"Busy write {sequence}",
                    f"heartbeat-busy-{sequence}",
                )
                busy_writes.append((write_started, time.monotonic()))
            except Exception as exc:  # the assertion below reports a sanitized class name
                writer_errors.append(type(exc).__name__)
                stop_writer.set()
                return
            next_write_at = write_started + 2.5

    def busy_window(opened, closed):
        """Return (write count, worst-case wakeup gap) for writes fully inside the window.

        A pre-#717 stream restarted its 15s wait on every cross-Vault wakeup, so a
        heartbeat window whose worst-case wakeup gap stays under the interval could
        not have produced that heartbeat. Bounds are conservative: each NOTIFY is
        only known to fall between its write's start and finish.
        """
        inside = [w for w in list(busy_writes) if w[0] >= opened and w[1] <= closed]
        if not inside:
            return 0, closed - opened
        gaps = [inside[0][1] - opened, closed - inside[-1][0]]
        gaps += [later[1] - earlier[0] for earlier, later in zip(inside, inside[1:])]
        return len(inside), max(gaps)

    def timeline():
        writes = [
            (round(s - stream_started, 3), round(f - stream_started, 3))
            for s, f in list(busy_writes)
        ]
        windows = [
            (count, round(gap, 3))
            for count, gap in (
                busy_window(opened, closed)
                for opened, closed in zip([stream_started] + heartbeats, heartbeats)
            )
        ]
        return (
            f"busy_writes={len(writes)} write_offsets={writes} "
            f"heartbeat_offsets={[round(h - stream_started, 3) for h in heartbeats]} "
            f"windows(count, worst_gap)={windows} writer_errors={writer_errors}"
        )

    # Start writing first: the Tail only flushes its response headers with the
    # first frame, so a writer started after open_tail() would miss window one.
    writer = threading.Thread(target=keep_busy_vault_changing, daemon=True)
    writer.start()
    heartbeats = []
    busy_windows = 0
    busy_stream = None
    stream_started = time.monotonic()  # replaced by open_tail(); keeps timeline() valid
    try:
        try:
            busy_stream, stream_started = open_tail(quiet_vault)
            # Keep the Vault busy until three heartbeat windows were each provably
            # busy; a slow runner only lengthens the observation instead of failing.
            while busy_windows < 3 and len(heartbeats) < 8 and not writer_errors:
                frame = read_frame(busy_stream)
                received = time.monotonic()
                if frame != [": heartbeat"]:
                    raise AssertionError(
                        f"quiet Vault received a non-heartbeat before its own write: {frame}; {timeline()}"
                    )
                opened = heartbeats[-1] if heartbeats else stream_started
                heartbeats.append(received)
                count, worst_gap = busy_window(opened, received)
                if count >= 2 and worst_gap < 15:
                    busy_windows += 1
        except OSError as exc:  # a starved Tail sends nothing, not even headers, until timeout
            raise AssertionError(
                f"busy-phase stream failed ({exc!r}); a starved Tail times out here; {timeline()}"
            ) from exc
        stop_writer.set()
        writer.join(timeout=10)
        assert not writer.is_alive(), f"busy writer did not stop; {timeline()}"
        assert not writer_errors, f"busy writer failed; {timeline()}"
        assert busy_windows >= 3, f"only {busy_windows} heartbeat windows stayed busy; {timeline()}"
        heartbeat_gaps = [heartbeats[0] - stream_started] + [
            later - earlier for earlier, later in zip(heartbeats, heartbeats[1:])
        ]
        assert all(gap <= 20 for gap in heartbeat_gaps), f"gaps={heartbeat_gaps}; {timeline()}"
        print(
            f"busy Vault: {timeline()}, "
            f"received_gaps={[round(gap, 3) for gap in heartbeat_gaps]}s"
        )

        # A target-Vault write after the comments must still arrive as a normal change.
        write_document(quiet_vault, "Target after heartbeat", "target-after-heartbeat")
        target_deadline = time.monotonic() + 30
        while True:
            assert time.monotonic() < target_deadline, "target change did not arrive within 30s"
            frame = read_frame(busy_stream)
            if frame == [": heartbeat"]:
                continue
            fields = dict(line.split(": ", 1) for line in frame if ": " in line)
            assert fields.get("event") == "change", frame
            event = json.loads(fields["data"])
            assert event["vault"] == quiet_vault, event
            assert event["kind"] == "document.put", event
            assert fields.get("id") == event["cursor"], event
            target_received = time.monotonic()
            break
        assert target_received > heartbeats[-1]
        print(
            "target change after heartbeat: "
            f"event={event['kind']}, cursor present, receipt={target_received - heartbeats[-1]:.3f}s later"
        )
    finally:
        stop_writer.set()
        writer.join(timeout=10)
        if busy_stream is not None:
            busy_stream.close()


def main() -> int:
    if len(sys.argv) < 2:
        print("expected an Event Tail E2E operation", file=sys.stderr)
        return 2

    operation = sys.argv[1]
    if operation == "json-field" and len(sys.argv) == 3:
        print(json.load(sys.stdin).get(sys.argv[2], ""))
    elif operation == "assert-filtered" and len(sys.argv) == 3:
        assert_filtered_events(sys.argv[2])
    elif operation == "assert-all-events" and len(sys.argv) == 3:
        assert_all_events(sys.argv[2])
    elif operation == "assert-resumed" and len(sys.argv) == 3:
        assert_resumed_events(sys.argv[2])
    elif operation == "heartbeat" and len(sys.argv) == 2:
        assert_heartbeat_timing()
    else:
        print("invalid Event Tail E2E operation or arguments", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
