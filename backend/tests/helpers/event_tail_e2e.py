"""Endpoint-side parsing and assertions for the Event Tail shell E2E."""
from __future__ import annotations

import json
import os
from pathlib import Path
import ssl
import sys
import threading
import time
import urllib.request
from uuid import UUID


MAX_CAPTURE_BYTES = 1024 * 1024
MAX_FRAMES = 1000


def read_frames(path: str) -> list[dict]:
    """Parse complete SSE records, not substrings of an arbitrary response."""
    with open(path, "rb") as source:
        raw = source.read(MAX_CAPTURE_BYTES + 1)
    assert len(raw) <= MAX_CAPTURE_BYTES, "event capture exceeds the test budget"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    frames: list[dict] = []
    fields: dict[str, str] = {}
    data: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if line.startswith(":"):
            continue
        if not line:
            if not fields and not data:
                continue
            assert data, "SSE record is missing data"
            frame = {**fields, "data": json.loads("\n".join(data))}
            value = frame["data"]
            assert isinstance(value, dict), "event data must be an object"
            assert frame.get("event") in {"change", "checkpoint"}, "unexpected SSE event type"
            cursor = frame.get("id")
            assert isinstance(cursor, str) and 0 < len(cursor) <= 4096, "missing event cursor"
            assert cursor == value.get("cursor"), "SSE id differs from its JSON cursor"
            assert cursor not in seen, "duplicate event cursor"
            assert type(value.get("version")) is int and value["version"] == 1, "unsupported event version"
            assert not {"id", "vault_id", "redis"} & value.keys(), "internal event envelope leaked"
            if frame["event"] == "checkpoint":
                assert set(value) == {"version", "cursor"}, "unexpected checkpoint envelope"
            else:
                assert isinstance(value.get("payload"), dict), "event payload must be an object"
            seen.add(cursor)
            frames.append(frame)
            assert len(frames) <= MAX_FRAMES, "too many captured event frames"
            fields, data = {}, []
            continue
        field, separator, value = line.partition(":")
        assert separator, "invalid SSE field"
        value = value.removeprefix(" ")
        if field == "data":
            data.append(value)
        elif field in {"event", "id"}:
            assert field not in fields, "duplicate SSE field"
            fields[field] = value
        elif field != "retry":
            raise AssertionError("unexpected SSE field")
    assert not fields and not data, "incomplete SSE record"
    return frames


def read_write(path: str) -> dict:
    with open(path, "rb") as source:
        raw = source.read(MAX_CAPTURE_BYTES + 1)
    assert len(raw) <= MAX_CAPTURE_BYTES, "write receipt exceeds the test budget"
    value = json.loads(raw)
    assert isinstance(value, dict) and value.get("kind") == "document_write", "document write failed"
    assert all(isinstance(value.get(key), str) and value[key] for key in ("uri", "vault", "path", "commit_hash")), "incomplete write receipt"
    assert value.get("current_commit") == value["commit_hash"], "write receipt revisions differ"
    return value


def assert_document_change(frame: dict, write: dict, vault: str, *, native: bool,
                           resource_id: str | None = None, kind: str = "document.put",
                           previous_commit: str | None = None) -> None:
    event = frame["data"]
    payload = event["payload"]
    assert event["vault"] == write["vault"] == vault, "wrong event Vault"
    assert event["kind"] == kind, "wrong event kind"
    assert event["resource_uri"] == write["uri"], "wrong event resource URI"
    assert payload.get("vault") == vault and payload.get("path") == write["path"], "wrong event resource path"
    assert payload.get("collection") == write["path"].rpartition("/")[0], "wrong event collection"
    assert payload.get("commit_hash") == write["commit_hash"], "wrong event revision"
    if native:
        assert payload.get("revision_id") == write["commit_hash"], "wrong Native revision"
        assert isinstance(payload.get("resource_id"), str), "missing Native resource identity"
        assert str(UUID(payload["resource_id"])) == payload["resource_id"], "invalid Native resource identity"
        assert UUID(payload["resource_id"]).int != 0, "empty Native resource identity"
        assert "previous_commit" in payload, "missing Native parent receipt"
        assert payload.get("previous_commit") == previous_commit, "wrong Native parent revision"
        assert not {"title", "content", "body", "frontmatter"} & payload.keys(), "Native event copied document data"
        if resource_id is not None:
            assert payload["resource_id"] == resource_id, "Native resource identity changed"


def assert_filtered_events(path: str, vault: str) -> None:
    frames = read_frames(path)

    changes = [frame for frame in frames if frame.get("event") == "change"]
    checkpoints = [frame for frame in frames if frame.get("event") == "checkpoint"]
    assert len(changes) == 1, "expected exactly one selected change"
    assert checkpoints, "excluded producer events must advance by checkpoint"
    change = changes[0]
    assert change["id"] == change["data"]["cursor"]
    assert change["data"]["version"] == 1
    assert change["data"]["vault"] == vault, "wrong filtered Vault"
    assert change["data"]["kind"] == "table.rows_changed"
    assert "vault_id" not in change["data"]


def assert_all_events(path: str, receipt: str, vault: str, *, native: bool) -> None:
    frames = [frame for frame in read_frames(path) if frame["event"] == "change"]
    events = [frame["data"] for frame in frames]
    assert len(events) >= 3, "missing retained producer events"
    assert {item["kind"] for item in events} >= {"document.put", "table.create", "table.rows_changed"}
    assert all(item["cursor"] for item in events)
    assert all(item["vault"] == vault for item in events), "cross-Vault retained event"
    write = read_write(receipt)
    selected = [frame for frame in frames if frame["data"].get("resource_uri") == write["uri"]]
    assert len(selected) == 1, "missing or duplicated initial document change"
    assert_document_change(selected[0], write, vault, native=native)
    first_index = frames.index(selected[0])
    create_index = next(i for i, event in enumerate(events) if event["kind"] == "table.create")
    rows_index = next(i for i, event in enumerate(events) if event["kind"] == "table.rows_changed")
    assert first_index < create_index < rows_index, "producer events are out of order"


def assert_resumed_events(path: str, receipt: str, vault: str, after: str, *, native: bool) -> None:
    frames = read_frames(path)
    assert all(frame["id"] != after for frame in frames), "resume replayed its starting cursor"
    changes = [frame for frame in frames if frame["event"] == "change"]
    assert len(changes) == 1, "expected exactly one post-cursor document change"
    assert_document_change(changes[0], read_write(receipt), vault, native=native)


def failure_artifact(directory: str) -> dict:
    """Bounded structural evidence only: never echo values, paths, cursors or bodies."""
    captures = []
    for name in ("filtered", "all", "resumed", "query-resumed", "live"):
        path = Path(directory) / f"{name}.sse"
        if not path.exists():
            continue
        item = {"capture": name, "bytes": path.stat().st_size}
        try:
            frames = read_frames(str(path))
        except (AssertionError, ValueError, KeyError, UnicodeError, OSError):
            item["valid_records"] = False
        else:
            item["valid_records"] = True
            item["frame_count"] = len(frames)
            item["frames"] = [
                {"type": frame["event"], "cursor_matches": True,
                 "document_put": frame["data"].get("kind") == "document.put",
                 "has_resource_id": "resource_id" in frame["data"].get("payload", {}),
                 "has_revision_id": "revision_id" in frame["data"].get("payload", {})}
                for frame in frames[:20]
            ]
        captures.append(item)
    return {"event_tail_failure": {"schema_version": 1, "captures": captures}}


def read_frame(response) -> list[str]:
    lines = []
    size = 0
    while True:
        line = response.readline(MAX_CAPTURE_BYTES + 1)
        size += len(line)
        assert size <= MAX_CAPTURE_BYTES, "live frame exceeds the test budget"
        assert line, "SSE stream closed before the expected frame"
        if line in (b"\n", b"\r\n"):
            if lines:
                return lines
            continue
        lines.append(line.rstrip(b"\r\n").decode("utf-8"))


def assert_live_change(path: str, *, native: bool) -> None:
    """Observe a heartbeat before writing, instead of guessing when curl connected."""
    base = os.environ["AKB_URL"].rstrip("/")
    vault = os.environ["AKB_QUIET_VAULT"]
    headers = {"Authorization": f"Bearer {os.environ['AKB_TOKEN']}"}
    context = ssl._create_unverified_context() if base.startswith("https:") else None
    request = urllib.request.Request(f"{base}/api/v1/events/{vault}", headers=headers)
    started = time.monotonic()
    with urllib.request.urlopen(request, context=context, timeout=22) as response:
        assert response.headers.get("Content-Type", "").startswith("text/event-stream")
        assert read_frame(response) == [": heartbeat"], "cursor-free tail replayed an old event"
        assert time.monotonic() - started <= 20, "initial live heartbeat missed its deadline"
        write_request = urllib.request.Request(
            f"{base}/api/v1/documents", method="POST",
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps({"vault": vault, "collection": "tail", "slug": "live",
                             "title": "Live document", "content": "live", "status": "active"}).encode(),
        )
        with urllib.request.urlopen(write_request, context=context, timeout=12) as written:
            write = json.load(written)
        assert write.get("kind") == "document_write", "live document write failed"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            frame = read_frame(response)
            if frame == [": heartbeat"]:
                continue
            Path(path).write_text("\n".join(frame) + "\n\n", encoding="utf-8")
            records = read_frames(path)
            assert len(records) == 1 and records[0]["event"] == "change", "expected one live change"
            assert_document_change(records[0], write, vault, native=native)
            return
    raise AssertionError("live document event missed its deadline")


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
        assert result.get("kind") == "document_write", "heartbeat document write failed"
        return result

    # No writer is running while the first heartbeat establishes the quiet baseline.
    baseline_stream, baseline_started = open_tail(quiet_vault)
    try:
        baseline_frame = read_frame(baseline_stream)
        baseline_received = time.monotonic()
    finally:
        baseline_stream.close()
    baseline_gap = baseline_received - baseline_started
    assert baseline_frame == [": heartbeat"], "baseline returned a non-heartbeat"
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
                        f"quiet Vault received a non-heartbeat before its own write; {timeline()}"
                    )
                opened = heartbeats[-1] if heartbeats else stream_started
                heartbeats.append(received)
                count, worst_gap = busy_window(opened, received)
                if count >= 2 and worst_gap < 15:
                    busy_windows += 1
        except OSError as exc:  # a starved Tail sends nothing, not even headers, until timeout
            raise AssertionError(
                f"busy-phase stream failed ({type(exc).__name__}); a starved Tail times out here; {timeline()}"
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
        write = write_document(quiet_vault, "Target after heartbeat", "target-after-heartbeat")
        target_deadline = time.monotonic() + 30
        while True:
            assert time.monotonic() < target_deadline, "target change did not arrive within 30s"
            frame = read_frame(busy_stream)
            if frame == [": heartbeat"]:
                continue
            fields = dict(line.split(": ", 1) for line in frame if ": " in line)
            assert fields.get("event") == "change", "expected target change after heartbeat"
            event = json.loads(fields["data"])
            assert fields.get("id") == event["cursor"], "heartbeat event cursor mismatch"
            assert_document_change({"data": event}, write, quiet_vault,
                                   native=os.environ.get("AKB_EVENT_TAIL_EXPECT_NATIVE") == "1")
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
    elif operation == "assert-write" and len(sys.argv) == 5:
        write = read_write(sys.argv[2])
        assert write["vault"] == sys.argv[3] and write["path"] == sys.argv[4], "wrong write target"
    elif operation == "last-cursor" and len(sys.argv) == 3:
        frames = read_frames(sys.argv[2])
        assert frames, "missing cursor frames"
        print(frames[-1]["id"])
    elif operation == "assert-filtered" and len(sys.argv) == 4:
        assert_filtered_events(sys.argv[2], sys.argv[3])
    elif operation == "assert-all-events" and len(sys.argv) == 5:
        assert_all_events(sys.argv[2], sys.argv[3], sys.argv[4], native=os.environ.get("AKB_EVENT_TAIL_EXPECT_NATIVE") == "1")
    elif operation == "assert-resumed" and len(sys.argv) == 6:
        assert_resumed_events(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], native=os.environ.get("AKB_EVENT_TAIL_EXPECT_NATIVE") == "1")
    elif operation == "live" and len(sys.argv) == 3:
        assert_live_change(sys.argv[2], native=os.environ.get("AKB_EVENT_TAIL_EXPECT_NATIVE") == "1")
    elif operation == "failure-artifact" and len(sys.argv) == 3:
        print(json.dumps(failure_artifact(sys.argv[2]), separators=(",", ":")))
    elif operation == "heartbeat" and len(sys.argv) == 2:
        assert_heartbeat_timing()
    else:
        print("invalid Event Tail E2E operation or arguments", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, ValueError, KeyError, UnicodeError, OSError) as error:
        # Do not let an assertion/driver traceback print raw event or write data.
        print(f"Event Tail check failed ({type(error).__name__}); see structural failure artifact", file=sys.stderr)
        raise SystemExit(1) from None
