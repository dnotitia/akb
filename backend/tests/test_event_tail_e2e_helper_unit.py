"""The endpoint oracle accepts title-free Native events, not arbitrary frames."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.helpers import event_tail_e2e as helper


VAULT = "event-tail-fixture"
RESOURCE_ID = "11111111-1111-4111-8111-111111111111"
WRITE = {"kind": "document_write", "vault": VAULT, "path": "tail/second.md",
         "uri": f"akb://{VAULT}/coll/tail/doc/second.md", "commit_hash": "native-revision",
         "current_commit": "native-revision"}
EVENT = {"version": 1, "cursor": "opaque-next", "vault": VAULT,
         "kind": "document.put", "resource_uri": WRITE["uri"], "actor": "writer",
         "occurred_at": "2026-01-01T00:00:00Z",
         "payload": {"vault": VAULT, "path": WRITE["path"], "collection": "tail",
                     "resource_id": RESOURCE_ID, "revision_id": WRITE["commit_hash"],
                     "commit_hash": WRITE["commit_hash"], "previous_commit": None}}


def sse(value, *, cursor=None, event="change"):
    return f"event: {event}\nid: {cursor or value['cursor']}\ndata: {json.dumps(value)}\n\n"


def captures(tmp_path, text):
    capture = tmp_path / "resumed.sse"
    capture.write_text(text)
    receipt = tmp_path / "write.json"
    receipt.write_text(json.dumps(WRITE))
    return str(capture), str(receipt)


@pytest.mark.parametrize("slug", ["second", "third", "live"])
def test_title_free_event_correlates_with_checked_write(tmp_path, slug):
    event, write = copy.deepcopy(EVENT), copy.deepcopy(WRITE)
    event["resource_uri"] = write["uri"] = f"akb://{VAULT}/coll/tail/doc/{slug}.md"
    event["payload"]["path"] = write["path"] = f"tail/{slug}.md"
    capture, receipt = captures(tmp_path, sse(event))
    Path(receipt).write_text(json.dumps(write))
    helper.assert_resumed_events(capture, receipt, VAULT, "opaque-before", native=True)


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
def test_wrong_identity_revision_or_contract_fails(tmp_path, section, key, value):
    event = copy.deepcopy(EVENT)
    (event if section == "envelope" else event["payload"])[key] = value
    capture, receipt = captures(tmp_path, sse(event))
    with pytest.raises(AssertionError):
        helper.assert_resumed_events(capture, receipt, VAULT, "opaque-before", native=True)


@pytest.mark.parametrize("text", [
    "", ": heartbeat\n\n", "data: {}\n\n", "event: change\ndata: {}\n\n",
    sse(EVENT).rstrip("\n"), sse(EVENT, cursor="different-id"),
    sse(EVENT) + sse(EVENT),
    sse(EVENT) + sse({**EVENT, "cursor": "another-new-cursor"}),
    sse({"version": 1, "cursor": "checkpoint-only"}, event="checkpoint"),
])
def test_missing_duplicate_partial_or_unrelated_resume_fails(tmp_path, text):
    capture, receipt = captures(tmp_path, text)
    with pytest.raises((AssertionError, ValueError)):
        helper.assert_resumed_events(capture, receipt, VAULT, "opaque-before", native=True)


def test_resume_excludes_starting_cursor(tmp_path):
    capture, receipt = captures(tmp_path, sse(EVENT))
    with pytest.raises(AssertionError, match="starting cursor"):
        helper.assert_resumed_events(capture, receipt, VAULT, EVENT["cursor"], native=True)


def test_comments_crlf_and_multiline_data(tmp_path):
    encoded = json.dumps(EVENT, indent=2)
    text = ": heartbeat\n\nevent: change\nid: opaque-next\n" + "".join(
        f"data: {line}\n" for line in encoded.splitlines()
    ) + "\n"
    capture, receipt = captures(tmp_path, text.replace("\n", "\r\n"))
    helper.assert_resumed_events(capture, receipt, VAULT, "opaque-before", native=True)


def test_known_resource_identity_and_parent_survive_update():
    event = copy.deepcopy(EVENT)
    event["kind"] = "document.update"
    event["payload"]["previous_commit"] = "prior-revision"
    helper.assert_document_change({"data": event}, WRITE, VAULT, native=True,
                                  kind="document.update", resource_id=RESOURCE_ID,
                                  previous_commit="prior-revision")
    event["payload"]["resource_id"] = "22222222-2222-4222-8222-222222222222"
    with pytest.raises(AssertionError, match="identity changed"):
        helper.assert_document_change({"data": event}, WRITE, VAULT, native=True,
                                      kind="document.update", resource_id=RESOURCE_ID,
                                      previous_commit="prior-revision")


def test_explicit_native_mode_cannot_accept_missing_native_fields(tmp_path):
    event = copy.deepcopy(EVENT)
    event["payload"].pop("revision_id")
    event["payload"].pop("resource_id")
    capture, receipt = captures(tmp_path, sse(event))
    helper.assert_resumed_events(capture, receipt, VAULT, "opaque-before", native=False)
    with pytest.raises(AssertionError):
        helper.assert_resumed_events(capture, receipt, VAULT, "opaque-before", native=True)


@pytest.mark.parametrize("change", ["error", "missing_revision", "different_current"])
def test_write_receipt_must_establish_success(tmp_path, change):
    value = copy.deepcopy(WRITE)
    if change == "error":
        value = {"error": "private-error-value"}
    elif change == "missing_revision":
        value.pop("commit_hash")
    else:
        value["current_commit"] = "different"
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(value))
    with pytest.raises(AssertionError):
        helper.read_write(str(path))


def test_failure_artifact_is_bounded_and_contains_no_event_values(tmp_path):
    marker = "sensitive-marker-PRIVATE"
    event = copy.deepcopy(EVENT)
    event.update(vault=marker, resource_uri=marker, actor=marker, cursor=marker)
    event["payload"].update(title=marker, content=marker, secret=marker)
    (tmp_path / "all.sse").write_text(sse(event))
    (tmp_path / "resumed.sse").write_text("data: " + marker + "\n\n")
    (tmp_path / "live.sse").write_bytes(b"x" * (helper.MAX_CAPTURE_BYTES + 1))
    encoded = json.dumps(helper.failure_artifact(str(tmp_path)))
    assert len(encoded) < 16384
    assert marker not in encoded and "opaque" not in encoded
    assert json.loads(encoded)["event_tail_failure"]["captures"][1]["valid_records"] is False


def test_cli_failure_never_prints_raw_frames(tmp_path):
    marker = "do-not-print-this-secret"
    path = tmp_path / "all.sse"
    path.write_text("data: " + marker + "\n\n")
    result = subprocess.run([sys.executable, helper.__file__, "last-cursor", str(path)],
                            capture_output=True, text=True, check=False, env=os.environ.copy())
    assert result.returncode == 1
    assert marker not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("overrides", [
    {"AKB_EVENT_TAIL_TOKEN": "private-fixture-marker"},
    {"AKB_EVENT_TAIL_OTHER_TOKEN": "private-fixture-marker"},
    {"AKB_EVENT_TAIL_EXPECT_NATIVE": "private-fixture-marker"},
])
def test_invalid_fixture_selection_fails_before_authentication(overrides):
    env = os.environ.copy()
    for name in ("AKB_EVENT_TAIL_TOKEN", "AKB_EVENT_TAIL_OTHER_TOKEN", "AKB_EVENT_TAIL_EXPECT_NATIVE"):
        env.pop(name, None)
    env.update(overrides, AKB_URL="http://127.0.0.1:1")
    result = subprocess.run(["bash", str(Path(helper.__file__).parents[1] / "test_event_tail_e2e.sh")],
                            capture_output=True, text=True, check=False, env=env, timeout=5)
    assert result.returncode == 1
    assert "private-fixture-marker" not in result.stdout + result.stderr
    assert "curl:" not in result.stderr
    assert "event_tail_failure" in result.stdout
