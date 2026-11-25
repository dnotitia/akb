"""Decode complete SSE frames and check document events against write receipts."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

MAX_FRAME_BYTES = 1024 * 1024
MAX_FRAMES = 1000


def event_tail_mode(env: Mapping[str, str]) -> tuple[bool, str, str]:
    native = env.get("AKB_EVENT_TAIL_EXPECT_NATIVE", "0")
    if native not in {"0", "1"}:
        raise ValueError("AKB_EVENT_TAIL_EXPECT_NATIVE must be 0 or 1")
    owner = env.get("AKB_EVENT_TAIL_TOKEN", "")
    outsider = env.get("AKB_EVENT_TAIL_OTHER_TOKEN", "")
    if bool(owner) != bool(outsider):
        raise ValueError("Supply both Event Tail fixture tokens or neither")
    return native == "1", owner, outsider


@dataclass(frozen=True, repr=False)
class Frame:
    event: str
    cursor: str
    data: dict[str, Any]


async def _sse_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    pending = b""
    async for chunk in chunks:
        # Byte splitting recognizes protocol CR/LF without splitting Unicode
        # separators within JSON strings. Hold a trailing CR for a split CRLF.
        lines = (pending + chunk).splitlines(keepends=True)
        pending = b""
        if lines and not lines[-1].endswith(b"\n"):
            pending = lines.pop()
        assert len(pending) <= MAX_FRAME_BYTES, "SSE line exceeds the test budget"
        for line in lines:
            yield line
    if pending.endswith(b"\r"):
        yield pending
    else:
        assert not pending, "incomplete SSE record"


def _parse_frame(lines: list[str]) -> Frame:
    fields: dict[str, str] = {}
    data: list[str] = []
    for line in lines:
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        assert separator, "invalid SSE field"
        value = value.removeprefix(" ")
        if field == "data":
            data.append(value)
        elif field in {"event", "id"}:
            assert field not in fields, "duplicate SSE field"
            fields[field] = value
        else:
            assert field == "retry", "unexpected SSE field"

    if not fields and not data:
        assert lines == [": heartbeat"], "unexpected SSE comment"
        return Frame("heartbeat", "", {})

    assert data, "SSE record is missing data"
    try:
        value = json.loads("\n".join(data))
    except ValueError:
        raise AssertionError("invalid SSE JSON") from None
    assert isinstance(value, dict), "event data must be an object"
    event, cursor = fields.get("event", ""), fields.get("id", "")
    assert event in {"change", "checkpoint"}, "unexpected SSE event type"
    assert 0 < len(cursor) <= 4096, "missing event cursor"
    assert cursor == value.get("cursor"), "SSE id differs from its JSON cursor"
    assert type(value.get("version")) is int and value["version"] == 1, "unsupported event version"
    assert not {"id", "vault_id", "redis"} & value.keys(), "internal event envelope leaked"
    if event == "checkpoint":
        assert set(value) == {"version", "cursor"}, "unexpected checkpoint envelope"
    else:
        assert isinstance(value.get("payload"), dict), "event payload must be an object"
    return Frame(event, cursor, value)


async def iter_frames(chunks: AsyncIterator[bytes]) -> AsyncIterator[Frame]:
    lines: list[str] = []
    size = 0
    seen: set[str] = set()
    async for raw in _sse_lines(chunks):
        size += len(raw)
        assert size <= MAX_FRAME_BYTES, "SSE frame exceeds the test budget"
        line = raw.rstrip(b"\r\n")
        if line:
            lines.append(line.decode("utf-8"))
        elif lines:
            frame = _parse_frame(lines)
            if frame.cursor:
                assert frame.cursor not in seen, "duplicate event cursor"
                seen.add(frame.cursor)
                assert len(seen) <= MAX_FRAMES, "too many captured event frames"
            yield frame
            lines = []
            size = 0
        else:
            size = 0
    assert not lines, "incomplete SSE record"


def assert_document_write(write: dict, vault: str, slug: str) -> None:
    assert write.get("kind") == "document_write", "document write failed"
    assert all(isinstance(write.get(key), str) and write[key] for key in ("uri", "commit_hash")), (
        "incomplete write receipt"
    )
    assert write.get("vault") == vault and write.get("path") == f"tail/{slug}.md", "wrong write target"
    assert write.get("current_commit") == write["commit_hash"], "write receipt revisions differ"


def assert_document_change(
    frame: Frame, write: dict, vault: str, *, native: bool,
    kind: str = "document.put", previous_commit: str | None = None, resource_id: str | None = None,
) -> None:
    assert frame.event == "change", "expected document change"
    event = frame.data
    payload = event["payload"]
    assert event["vault"] == write["vault"] == vault, "wrong event Vault"
    assert event["kind"] == kind, "wrong event kind"
    assert event["resource_uri"] == write["uri"], "wrong event resource URI"
    assert payload.get("vault") == vault and payload.get("path") == write["path"], "wrong event resource path"
    assert payload.get("collection") == write["path"].rpartition("/")[0], "wrong event collection"
    assert payload.get("commit_hash") == write["commit_hash"], "wrong event revision"
    if native:
        assert payload.get("revision_id") == write["commit_hash"], "wrong Native revision"
        identity = payload.get("resource_id")
        assert isinstance(identity, str), "missing Native resource identity"
        try:
            parsed = UUID(identity)
        except ValueError:
            raise AssertionError("invalid Native resource identity") from None
        assert str(parsed) == identity and parsed.int != 0, "invalid Native resource identity"
        assert "previous_commit" in payload, "missing Native parent receipt"
        assert payload["previous_commit"] == previous_commit, "wrong Native parent revision"
        assert not {"title", "content", "body", "frontmatter"} & payload.keys(), "Native event copied document data"
        if resource_id is not None:
            assert identity == resource_id, "Native resource identity changed"


def assert_filtered(frames: list[Frame], vault: str) -> None:
    changes = [frame for frame in frames if frame.event == "change"]
    assert len(changes) == 1, f"expected one filtered change, got {len(changes)}"
    assert changes[0].data["vault"] == vault, "filtered event came from another Vault"
    assert changes[0].data["kind"] == "table.rows_changed", "wrong event kind matched the filter"
    assert any(frame.event == "checkpoint" for frame in frames), "filtered events did not advance the cursor"


def retained_cursor(frames: list[Frame], write: dict, vault: str, *, native: bool) -> str:
    assert all(frame.event == "change" for frame in frames), "unexpected unfiltered frame type"
    kinds = [frame.data["kind"] for frame in frames]
    assert {"document.put", "table.create", "table.rows_changed"} <= set(kinds), (
        f"missing retained producer events in {len(frames)} frames"
    )
    assert all(frame.data["vault"] == vault for frame in frames), "retained event came from another Vault"
    matching_writes = [
        index for index, frame in enumerate(frames) if frame.data.get("resource_uri") == write["uri"]
    ]
    assert len(matching_writes) == 1, f"expected one initial document event, got {len(matching_writes)}"
    first_index = matching_writes[0]
    assert_document_change(frames[first_index], write, vault, native=native)
    assert first_index < kinds.index("table.create") < kinds.index("table.rows_changed"), (
        "producer events are out of order"
    )
    return frames[-1].cursor


def assert_resumed(frames: list[Frame], write: dict, vault: str, after: str, *, native: bool) -> None:
    assert all(frame.cursor != after for frame in frames), "resume replayed its starting cursor"
    changes = [frame for frame in frames if frame.event == "change"]
    assert len(changes) == 1, "expected exactly one post-cursor document change"
    assert_document_change(changes[0], write, vault, native=native)
