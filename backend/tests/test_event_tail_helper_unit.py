"""The wire oracle correlates title-free Native events with checked receipts."""

from __future__ import annotations

import copy
import json

import pytest

from tests.runtime_e2e import event_tail as helper

VAULT = "event-tail-fixture"
RESOURCE_ID = "11111111-1111-4111-8111-111111111111"
WRITE = {
    "kind": "document_write",
    "vault": VAULT,
    "path": "tail/second.md",
    "uri": f"akb://{VAULT}/coll/tail/doc/second.md",
    "commit_hash": "native-revision",
    "current_commit": "native-revision",
}
EVENT = {
    "version": 1,
    "cursor": "opaque-next",
    "vault": VAULT,
    "kind": "document.put",
    "resource_uri": WRITE["uri"],
    "actor": "writer",
    "occurred_at": "2026-01-01T00:00:00Z",
    "payload": {
        "vault": VAULT,
        "path": WRITE["path"],
        "collection": "tail",
        "resource_id": RESOURCE_ID,
        "revision_id": WRITE["commit_hash"],
        "commit_hash": WRITE["commit_hash"],
        "previous_commit": None,
    },
}


def sse(value, *, cursor=None, event="change"):
    return f"event: {event}\nid: {cursor or value['cursor']}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"


async def receive(text):
    async def chunks():
        raw = text.encode()
        for offset in range(0, len(raw), 5):
            yield raw[offset:offset + 5]

    return [frame async for frame in helper.iter_frames(chunks())]


@pytest.mark.parametrize("slug", ["second", "third", "live"])
async def test_title_free_event_correlates_with_checked_write(slug):
    event, write = copy.deepcopy(EVENT), copy.deepcopy(WRITE)
    write["uri"] = f"akb://{VAULT}/coll/tail/doc/{slug}.md"
    write["path"] = f"tail/{slug}.md"
    event["resource_uri"] = write["uri"]
    event["payload"]["path"] = write["path"]
    helper.assert_document_write(write, VAULT, slug)
    helper.assert_resumed(await receive(sse(event)), write, VAULT, "opaque-before", native=True)


@pytest.mark.parametrize(("section", "key", "value"), [
    ("envelope", "vault", "other-vault"),
    ("envelope", "resource_uri", "akb://other/doc/other.md"),
    ("envelope", "kind", "document.update"),
    ("envelope", "version", True),
    ("payload", "vault", "other-vault"),
    ("payload", "path", "other.md"),
    ("payload", "collection", "other"),
    ("payload", "commit_hash", "wrong-revision"),
    ("payload", "revision_id", "wrong-revision"),
    ("payload", "previous_commit", "not-a-create"),
    ("payload", "resource_id", None),
    ("payload", "resource_id", "00000000-0000-0000-0000-000000000000"),
    ("payload", "body", "private-content-marker"),
])
async def test_wrong_identity_revision_or_contract_fails(section, key, value):
    event = copy.deepcopy(EVENT)
    (event if section == "envelope" else event["payload"])[key] = value
    with pytest.raises(AssertionError):
        helper.assert_resumed(await receive(sse(event)), WRITE, VAULT, "opaque-before", native=True)


@pytest.mark.parametrize("text", [
    "", ": heartbeat\n\n", "data: {}\n\n", "event: change\ndata: {}\n\n",
    sse(EVENT).rstrip("\n"), sse(EVENT, cursor="different-id"),
    sse(EVENT) + sse(EVENT),
    sse(EVENT) + sse({**EVENT, "cursor": "another-new-cursor"}),
    sse({"version": 1, "cursor": "checkpoint-only"}, event="checkpoint"),
])
async def test_missing_duplicate_partial_or_unrelated_resume_fails(text):
    with pytest.raises(AssertionError):
        helper.assert_resumed(await receive(text), WRITE, VAULT, "opaque-before", native=True)


async def test_resume_excludes_starting_cursor():
    with pytest.raises(AssertionError, match="starting cursor"):
        helper.assert_resumed(await receive(sse(EVENT)), WRITE, VAULT, EVENT["cursor"], native=True)


async def test_comments_crlf_and_multiline_data():
    encoded = json.dumps(EVENT, indent=2)
    text = ": heartbeat\n\nevent: change\nid: opaque-next\n" + "".join(
        f"data: {line}\n" for line in encoded.splitlines()
    ) + "\n"
    frames = await receive(text.replace("\n", "\r\n"))
    assert [frame.event for frame in frames] == ["heartbeat", "change"]
    helper.assert_resumed(frames, WRITE, VAULT, "opaque-before", native=True)


async def test_known_resource_identity_and_parent_survive_update():
    event = copy.deepcopy(EVENT)
    event["kind"] = "document.update"
    event["payload"]["previous_commit"] = "prior-revision"
    (frame,) = await receive(sse(event))
    expected = {
        "native": True, "kind": "document.update", "resource_id": RESOURCE_ID,
        "previous_commit": "prior-revision",
    }
    helper.assert_document_change(frame, WRITE, VAULT, **expected)
    frame.data["payload"]["resource_id"] = "22222222-2222-4222-8222-222222222222"
    with pytest.raises(AssertionError, match="identity changed"):
        helper.assert_document_change(frame, WRITE, VAULT, **expected)


async def test_explicit_native_mode_cannot_accept_missing_native_fields():
    event = copy.deepcopy(EVENT)
    event["payload"].pop("revision_id")
    event["payload"].pop("resource_id")
    frames = await receive(sse(event))
    helper.assert_resumed(frames, WRITE, VAULT, "opaque-before", native=False)
    with pytest.raises(AssertionError):
        helper.assert_resumed(frames, WRITE, VAULT, "opaque-before", native=True)


@pytest.mark.parametrize("change", ["error", "missing_revision", "different_current"])
def test_write_receipt_must_establish_success(change):
    value = copy.deepcopy(WRITE)
    if change == "error":
        value = {"error": "private-error-value"}
    elif change == "missing_revision":
        value.pop("commit_hash")
    else:
        value["current_commit"] = "different-current"
    with pytest.raises(AssertionError):
        helper.assert_document_write(value, VAULT, "second")


async def test_frame_and_parse_errors_keep_event_values_out_of_diagnostics():
    marker = "sensitive-marker-PRIVATE"
    event = copy.deepcopy(EVENT)
    event.update(vault=marker, resource_uri=marker, actor=marker, cursor=marker)
    event["payload"].update(title=marker, content=marker, secret=marker)
    (frame,) = await receive(sse(event))
    assert marker not in repr(frame)

    with pytest.raises(AssertionError, match="^invalid SSE JSON$") as error:
        await receive(f"event: change\ndata: {marker}\n\n")
    assert error.value.__suppress_context__


@pytest.mark.parametrize("overrides", [
    {"AKB_EVENT_TAIL_TOKEN": "private-fixture-marker"},
    {"AKB_EVENT_TAIL_OTHER_TOKEN": "private-fixture-marker"},
    {"AKB_EVENT_TAIL_EXPECT_NATIVE": "private-fixture-marker"},
])
def test_invalid_fixture_selection_fails_without_echoing_tokens(overrides):
    with pytest.raises(ValueError) as error:
        helper.event_tail_mode(overrides)
    assert "private-fixture-marker" not in str(error.value)


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
async def test_unicode_separators_in_event_values_are_not_sse_line_endings(separator):
    event = copy.deepcopy(EVENT)
    event["actor"] = f"writer{separator}name"
    frames = await receive(sse(event))
    helper.assert_resumed(frames, WRITE, VAULT, "opaque-before", native=True)
    assert frames[0].data["actor"] == event["actor"]


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
async def test_record_needs_a_blank_line_after_its_data(ending):
    encoded = sse(EVENT).replace("\n", ending)
    helper.assert_resumed(await receive(encoded), WRITE, VAULT, "opaque-before", native=True)
    with pytest.raises(AssertionError, match="incomplete"):
        await receive(encoded.removesuffix(ending))


async def test_frames_exceeding_the_capture_budget_are_rejected(monkeypatch):
    monkeypatch.setattr(helper, "MAX_FRAME_BYTES", 64)
    with pytest.raises(AssertionError, match="exceeds the test budget"):
        await receive(sse(EVENT))
