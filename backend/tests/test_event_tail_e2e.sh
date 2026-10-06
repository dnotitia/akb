#!/bin/bash
#
# AKB REST/SSE Event Tail E2E.
# Exercises the public reader surface against PostgreSQL-backed events without
# knowing or invoking the internal Redis fan-out.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()
TMP_DIR=$(mktemp -d)
VAULT="event-tail-$(date +%s)-$RANDOM"
BUSY_VAULT="event-tail-busy-$(date +%s)-$RANDOM"
USER="event-tail-user-$(date +%s)-$RANDOM"
OTHER="event-tail-other-$(date +%s)-$RANDOM"

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }
cleanup() {
  if [ -n "${TOKEN:-}" ]; then
    for vault in "$VAULT" "$BUSY_VAULT"; do
      curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$vault" \
        -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1 || true
    done
  fi
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

json_field() { python3 -c "import json,sys; print(json.load(sys.stdin).get('$1',''))" 2>/dev/null; }

echo "╔══════════════════════════════════════════╗"
echo "║       REST/SSE Event Tail E2E             ║"
echo "║       Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ 0. Setup"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
TOKEN=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" | json_field token)

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$OTHER\",\"email\":\"$OTHER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
OTHER_TOKEN=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$OTHER\",\"password\":\"test1234\"}" | json_field token)

[ -n "$TOKEN" ] && [ -n "$OTHER_TOKEN" ] && pass "two authenticated users" || { fail "setup" "could not obtain both tokens"; exit 1; }

VAULT_RESPONSE=$(curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$VAULT" \
  -H "Authorization: Bearer $TOKEN")
VAULT_ID=$(echo "$VAULT_RESPONSE" | json_field vault_id)
[ -n "$VAULT_ID" ] && pass "private Vault created" || { fail "vault" "$VAULT_RESPONSE"; exit 1; }

BUSY_VAULT_RESPONSE=$(curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$BUSY_VAULT" \
  -H "Authorization: Bearer $TOKEN")
BUSY_VAULT_ID=$(echo "$BUSY_VAULT_RESPONSE" | json_field vault_id)
[ -n "$BUSY_VAULT_ID" ] && pass "second Vault created for heartbeat isolation" || { fail "busy vault" "$BUSY_VAULT_RESPONSE"; exit 1; }

DOC_RESPONSE=$(curl -sk -X POST "$BASE_URL/api/v1/documents" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"tail\",\"title\":\"First document\",\"slug\":\"first\",\"content\":\"first\",\"status\":\"active\"}")
[ "$(echo "$DOC_RESPONSE" | json_field kind)" = "document_write" ] && pass "document producer event" || fail "document" "$DOC_RESPONSE"

TABLE_RESPONSE=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"items","columns":[{"name":"sku","type":"text"}]}')
[ "$(echo "$TABLE_RESPONSE" | json_field kind)" = "table" ] && pass "table producer event" || fail "table" "$TABLE_RESPONSE"

ROW_RESPONSE=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/items/rows" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Prefer: return=representation' \
  -H 'Content-Type: application/json' \
  -d '[{"sku":"one"}]')
[ "$(echo "$ROW_RESPONSE" | json_field kind)" = "table_query" ] && pass "table.rows_changed producer event" || fail "rows_changed" "$ROW_RESPONSE"

echo ""
echo "▸ 1. start=earliest and exact kind checkpoint"
FILTERED="$TMP_DIR/filtered.sse"
curl -sk --no-buffer --max-time 2 \
  "$BASE_URL/api/v1/events/$VAULT?start=earliest&kind=table.rows_changed" \
  -H "Authorization: Bearer $TOKEN" >"$FILTERED" 2>/dev/null || true

python3 - "$FILTERED" <<'PY'
import json
import sys

text = open(sys.argv[1], encoding="utf-8").read()
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
PY
[ $? -eq 0 ] && pass "exact filter emits change plus checkpoint" || fail "filter/checkpoint" "unexpected SSE frames"

echo ""
echo "▸ 2. unfiltered retained order and cursor resume"
ALL_EVENTS="$TMP_DIR/all.sse"
curl -sk --no-buffer --max-time 2 \
  "$BASE_URL/api/v1/events/$VAULT?start=earliest" \
  -H "Authorization: Bearer $TOKEN" >"$ALL_EVENTS" 2>/dev/null || true

python3 - "$ALL_EVENTS" <<'PY'
import json
import sys

text = open(sys.argv[1], encoding="utf-8").read()
events = []
for block in text.split("\n\n"):
    rows = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
    if rows.get("event") == "change":
        events.append(json.loads(rows["data"]))
assert len(events) >= 3, events
assert {item["kind"] for item in events} >= {"document.put", "table.create", "table.rows_changed"}
assert all(item["cursor"] for item in events)
assert "redis" not in text.lower()
PY
[ $? -eq 0 ] && pass "all Vault kinds are ordered and Redis-free" || fail "all events" "retained event envelope mismatch"

LAST_CURSOR=$(awk '/^id: / {cursor=$2} END {print cursor}' "$ALL_EVENTS")
[ -n "$LAST_CURSOR" ] && pass "opaque cursor captured" || fail "cursor" "no SSE id"

curl -sk -X POST "$BASE_URL/api/v1/documents" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"tail\",\"title\":\"Second document\",\"slug\":\"second\",\"content\":\"second\",\"status\":\"active\"}" >/dev/null
RESUMED="$TMP_DIR/resumed.sse"
curl -sk --no-buffer --max-time 2 \
  "$BASE_URL/api/v1/events/$VAULT" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Last-Event-ID: $LAST_CURSOR" >"$RESUMED" 2>/dev/null || true

python3 - "$RESUMED" <<'PY'
import json
import sys

text = open(sys.argv[1], encoding="utf-8").read()
changes = []
for block in text.split("\n\n"):
    rows = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
    if rows.get("event") == "change":
        changes.append(json.loads(rows["data"]))
assert changes, text
assert all(item["kind"] == "document.put" for item in changes), changes
assert "Second document" in text
assert "redis" not in text.lower()
PY
[ $? -eq 0 ] && pass "Last-Event-ID resumes after the retained cursor" || fail "resume" "new event was not delivered"

RESUME_CURSOR=$(awk '/^id: / {cursor=$2} END {print cursor}' "$RESUMED")
curl -sk -X POST "$BASE_URL/api/v1/documents" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"tail\",\"title\":\"Third document\",\"slug\":\"third\",\"content\":\"third\",\"status\":\"active\"}" >/dev/null
QUERY_RESUMED="$TMP_DIR/query-resumed.sse"
curl -sk --no-buffer --max-time 2 \
  "$BASE_URL/api/v1/events/$VAULT?cursor=$RESUME_CURSOR" \
  -H "Authorization: Bearer $TOKEN" >"$QUERY_RESUMED" 2>/dev/null || true
grep -q 'Third document' "$QUERY_RESUMED" && pass "cursor query resumes the same tail" || fail "query resume" "new event was not delivered"

echo ""
echo "▸ 3. start-at-tail, invalid cursor, and access isolation"
LIVE="$TMP_DIR/live.sse"
curl -sk --no-buffer --max-time 4 \
  "$BASE_URL/api/v1/events/$VAULT" \
  -H "Authorization: Bearer $TOKEN" >"$LIVE" 2>/dev/null &
LIVE_PID=$!
sleep 0.3
curl -sk -X POST "$BASE_URL/api/v1/documents" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"tail\",\"title\":\"Live document\",\"slug\":\"live\",\"content\":\"live\",\"status\":\"active\"}" >/dev/null
wait "$LIVE_PID" 2>/dev/null || true
grep -q 'Live document' "$LIVE" && pass "cursor-free connection waits for new tail" || fail "live tail" "post-connect event missing"

INVALID_CODE=$(curl -sk -o "$TMP_DIR/invalid.json" -w '%{http_code}' \
  "$BASE_URL/api/v1/events/$VAULT?cursor=invalid" \
  -H "Authorization: Bearer $TOKEN")
INVALID_BODY=$(cat "$TMP_DIR/invalid.json")
[ "$INVALID_CODE" = "400" ] && echo "$INVALID_BODY" | grep -q 'invalid_event_cursor' \
  && pass "malformed cursor returns standard 400" || fail "invalid cursor" "code=$INVALID_CODE body=$INVALID_BODY"

UNAUTHORIZED_CODE=$(curl -sk -o "$TMP_DIR/unauthorized.json" -w '%{http_code}' \
  "$BASE_URL/api/v1/events/$VAULT?start=earliest" \
  -H "Authorization: Bearer $OTHER_TOKEN")
UNAUTHORIZED_BODY=$(cat "$TMP_DIR/unauthorized.json")
[ "$UNAUTHORIZED_CODE" = "403" ] && ! echo "$UNAUTHORIZED_BODY" | grep -q 'document.put' \
  && pass "non-member cannot observe the tail" || fail "access isolation" "code=$UNAUTHORIZED_CODE body=$UNAUTHORIZED_BODY"

echo ""
echo "▸ 4. quiet and cross-Vault heartbeat deadlines"
if AKB_URL="$BASE_URL" AKB_TOKEN="$TOKEN" AKB_QUIET_VAULT="$VAULT" AKB_BUSY_VAULT="$BUSY_VAULT" python3 <<'PY'
import json
import os
import ssl
import threading
import time
import urllib.request

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


busy_stream, stream_started = open_tail(quiet_vault)
busy_writes = []
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
            busy_writes.append(time.monotonic())
        except Exception as exc:  # the assertion below reports a sanitized class name
            writer_errors.append(type(exc).__name__)
            stop_writer.set()
            return
        next_write_at = write_started + 2.5


writer = threading.Thread(target=keep_busy_vault_changing, daemon=True)
writer.start()
heartbeats = []
try:
    while len(heartbeats) < 3:
        frame = read_frame(busy_stream)
        received = time.monotonic()
        if frame != [": heartbeat"]:
            raise AssertionError(
                f"quiet Vault received a non-heartbeat before its own write: {frame}"
            )
        heartbeats.append(received)
    stop_writer.set()
    writer.join(timeout=10)
    assert not writer.is_alive(), "busy writer did not stop"
    assert not writer_errors, f"busy writer failed: {writer_errors}"
    assert len(busy_writes) >= 15, f"only {len(busy_writes)} busy writes succeeded"
    assert busy_writes[-1] - stream_started >= 35, "busy writes did not span the observation"
    heartbeat_gaps = [heartbeats[0] - stream_started] + [
        later - earlier for earlier, later in zip(heartbeats, heartbeats[1:])
    ]
    assert all(gap <= 20 for gap in heartbeat_gaps), heartbeat_gaps
    observation_seconds = heartbeats[-1] - stream_started
    busy_write_offsets = [round(received - stream_started, 3) for received in busy_writes]
    print(
        "busy Vault: "
        f"successful_write_offsets={busy_write_offsets}, "
        f"observation={observation_seconds:.3f}s, "
        f"heartbeats=3, received_gaps={[round(gap, 3) for gap in heartbeat_gaps]}s"
    )

    # A target-Vault write after the comments must still arrive as a normal change.
    write_document(quiet_vault, "Target after heartbeat", "target-after-heartbeat")
    while True:
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
    busy_stream.close()
PY
then
  pass "quiet baseline and repeated 15s-contract heartbeats stay scoped to the target Vault"
else
  fail "heartbeat starvation" "baseline, busy cross-Vault cadence, comment shape, or target resume failed"
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo "  Failures:"
  printf '    - %s\n' "${ERRORS[@]}"
  exit 1
fi
exit 0
