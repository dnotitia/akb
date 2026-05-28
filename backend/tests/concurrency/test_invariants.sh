#!/usr/bin/env bash
# Concurrency invariant tests for AKB (audit-v2 Tier 0/1 verification).
#
# Hits prod-shaped state with N concurrent clients (curl) and asserts the
# post-condition in the response or directly in PG.
#
# Defaults to the audit Docker stack on :8001 with PG container
# akb-v2-postgres-1. Override AKB_URL / PG_CONTAINER if needed.

set -uo pipefail

AKB_URL="${AKB_URL:-http://localhost:8001}"
PG_CONTAINER="${PG_CONTAINER:-akb-v2-postgres-1}"
N_PARALLEL="${N_PARALLEL:-20}"
CURL_TIMEOUT="${CURL_TIMEOUT:-30}"
LABELS=("$@")

PASS=0
FAIL=0
FAILURES=()

pass() { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1: $2"; FAIL=$((FAIL+1)); FAILURES+=("$1: $2"); }

run_inv() {
  local id="$1"
  if [ ${#LABELS[@]} -gt 0 ]; then
    local found=0
    for w in "${LABELS[@]}"; do [ "$w" = "$id" ] && found=1 && break; done
    [ $found -eq 0 ] && return 1
  fi
  return 0
}

pg() {
  docker exec "$PG_CONTAINER" bash -c "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -tAc \"$1\""
}

# ── Setup: user + vault + PAT ─────────────────────────────────────────
TS=$(date +%s)
USER="inv-$TS"
EMAIL="inv-$TS@test.local"
PW="testtest1234"
VAULT="inv-vault-$TS"

echo "▸ Setup"
curl -sf --max-time $CURL_TIMEOUT -X POST "$AKB_URL/api/v1/auth/register" -H "Content-Type: application/json" \
  -d "{\"username\":\"$USER\",\"email\":\"$EMAIL\",\"password\":\"$PW\"}" > /dev/null \
  || { echo "register failed"; exit 1; }

JWT=$(curl -sf --max-time $CURL_TIMEOUT -X POST "$AKB_URL/api/v1/auth/login" -H "Content-Type: application/json" \
  -d "{\"username\":\"$USER\",\"password\":\"$PW\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
[ -z "$JWT" ] && { echo "login failed"; exit 1; }

PAT=$(curl -sf --max-time $CURL_TIMEOUT -X POST "$AKB_URL/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" -d '{"name":"inv-test"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
[ -z "$PAT" ] && { echo "PAT mint failed"; exit 1; }

curl -sf --max-time $CURL_TIMEOUT -X POST "$AKB_URL/api/v1/vaults?name=$VAULT&description=invariants" \
  -H "Authorization: Bearer $PAT" > /dev/null || { echo "vault create failed"; exit 1; }

cleanup() {
  curl -s --max-time 10 -X DELETE "$AKB_URL/api/v1/vaults/$VAULT" -H "Authorization: Bearer $PAT" > /dev/null 2>&1 || true
}
trap cleanup EXIT

VID=$(pg "SELECT id FROM vaults WHERE name='$VAULT'")
echo "  ✓ user=$USER vault=$VAULT (id=$VID)"
echo ""

# ── MCP helpers ───────────────────────────────────────────────────────
mcp_init() {
  local sid
  sid=$(curl -s --max-time $CURL_TIMEOUT -i -X POST "$AKB_URL/mcp/" \
    -H "Authorization: Bearer $PAT" \
    -H "Accept: application/json, text/event-stream" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"inv","version":"1"}}}' 2>&1 \
    | grep -i 'mcp-session-id' | head -1 | awk -F': ' '{print $2}' | tr -d '\r\n')
  curl -s --max-time $CURL_TIMEOUT -X POST "$AKB_URL/mcp/" \
    -H "Authorization: Bearer $PAT" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $sid" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' > /dev/null
  echo "$sid"
}

mcp_call() {
  local sid="$1" name="$2" args="$3"
  curl -s --max-time $CURL_TIMEOUT -X POST "$AKB_URL/mcp/" \
    -H "Authorization: Bearer $PAT" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $sid" \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":99,\"method\":\"tools/call\",\"params\":{\"name\":\"$name\",\"arguments\":$args}}"
}

mcp_result_text() {
  python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    txt = d["result"]["content"][0]["text"]
    print(txt)
except Exception as e:
    print(f"PARSE_ERR: {e}", file=sys.stderr)
    sys.exit(1)
'
}

# Classify an MCP tools/call response body: "OK" / "ERR:<msg>"
mcp_classify() {
python3 -c '
import sys, json
raw = sys.stdin.read()
if not raw.strip():
    print("ERR:empty-response"); sys.exit(0)
try:
    d = json.loads(raw)
except Exception:
    snippet = raw[:120]
    print("ERR:non-json:" + repr(snippet)); sys.exit(0)
err = d.get("error")
if err:
    msg = err.get("message", "jsonrpc-error") if isinstance(err, dict) else str(err)
    print("ERR:" + str(msg)); sys.exit(0)
res = d.get("result") or {}
if res.get("isError"):
    print("ERR:" + str(res)); sys.exit(0)
# tools/call wraps the actual return in result.content[0].text (json string)
try:
    txt = res["content"][0]["text"]
    obj = json.loads(txt)
    if isinstance(obj, dict) and "error" in obj:
        print("ERR:" + str(obj["error"])); sys.exit(0)
except Exception:
    pass
print("OK")
'
}

SID=$(mcp_init)
[ -z "$SID" ] && { echo "MCP init failed"; exit 1; }
echo "▸ MCP session: $SID"
echo ""

# ── INV-1: edges.kind — explicit edge survives concurrent akb_update ──
if run_inv "INV-1"; then
  echo "▸ INV-1: edges.kind — explicit edge survives $N_PARALLEL concurrent akb_update"
  R=$(mcp_call "$SID" "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"inv1\",\"title\":\"src\",\"content\":\"# src\",\"type\":\"note\"}" | mcp_result_text)
  SRC=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["uri"])')
  R=$(mcp_call "$SID" "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"inv1\",\"title\":\"tgt\",\"content\":\"# tgt\",\"type\":\"note\"}" | mcp_result_text)
  TGT=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["uri"])')
  LR=$(mcp_call "$SID" "akb_link" "{\"source\":\"$SRC\",\"target\":\"$TGT\",\"relation\":\"depends_on\"}")
  CLS=$(echo "$LR" | mcp_classify)
  [ "$CLS" != "OK" ] && fail "INV-1 link" "$CLS"

  PRE=$(pg "SELECT COUNT(*) FROM edges WHERE source_uri='$SRC' AND kind='explicit'")
  if [ "$PRE" != "1" ]; then
    fail "INV-1 setup" "explicit edge not created (got $PRE)"
  else
    for i in $(seq 1 $N_PARALLEL); do
      (curl -s --max-time $CURL_TIMEOUT -X POST "$AKB_URL/mcp/" \
        -H "Authorization: Bearer $PAT" \
        -H "Accept: application/json, text/event-stream" \
        -H "mcp-session-id: $SID" \
        -H "Content-Type: application/json" \
        -d "{\"jsonrpc\":\"2.0\",\"id\":1000$i,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_update\",\"arguments\":{\"uri\":\"$SRC\",\"content\":\"# src v$i\"}}}" > /dev/null) &
    done
    wait
    POST=$(pg "SELECT COUNT(*) FROM edges WHERE source_uri='$SRC' AND kind='explicit'")
    if [ "$POST" = "1" ]; then
      pass "INV-1 explicit edge survived $N_PARALLEL concurrent updates"
    else
      fail "INV-1" "explicit edge count after updates = $POST (expected 1)"
    fi
  fi
fi

# ── INV-2: concurrent create_table — exactly 1 success ────────────────
if run_inv "INV-2"; then
  echo "▸ INV-2: concurrent akb_create_table same name — exactly 1 wins"
  NAME="inv2_t"
  RESULTS=$(mktemp -d)
  for i in $(seq 1 10); do
    (curl -s --max-time $CURL_TIMEOUT -X POST "$AKB_URL/mcp/" \
      -H "Authorization: Bearer $PAT" \
      -H "Accept: application/json, text/event-stream" \
      -H "mcp-session-id: $SID" \
      -H "Content-Type: application/json" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":2000$i,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_create_table\",\"arguments\":{\"vault\":\"$VAULT\",\"name\":\"$NAME\",\"columns\":[{\"name\":\"v\",\"type\":\"text\"}]}}}" \
      > "$RESULTS/$i.json") &
  done
  wait
  OK=0; CONFLICT=0; OTHER=0
  for f in "$RESULTS"/*.json; do
    cls=$(cat "$f" | mcp_classify)
    case "$cls" in
      OK) OK=$((OK+1)) ;;
      ERR:*already*exist*|ERR:*Conflict*|ERR:*already*) CONFLICT=$((CONFLICT+1)) ;;
      *) OTHER=$((OTHER+1)); echo "    other: $cls" ;;
    esac
  done
  rm -rf "$RESULTS"
  PG_CNT=$(pg "SELECT COUNT(*) FROM vault_tables WHERE vault_id='$VID' AND name='$NAME'")
  if [ "$OK" = "1" ] && [ "$CONFLICT" = "9" ] && [ "$PG_CNT" = "1" ]; then
    pass "INV-2 1 OK / 9 conflict / pg_rows=1"
  else
    fail "INV-2" "OK=$OK conflict=$CONFLICT other=$OTHER pg_rows=$PG_CNT"
  fi
fi

# ── INV-4: publication view_count + expires atomic ────────────────────
if run_inv "INV-4"; then
  echo "▸ INV-4: max_views=5 publication, N=$N_PARALLEL concurrent resolve → view_count ≤ 5"
  R=$(mcp_call "$SID" "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"inv4\",\"title\":\"pub\",\"content\":\"# pub\",\"type\":\"note\"}" | mcp_result_text)
  PUB_URI=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["uri"])')
  R=$(mcp_call "$SID" "akb_publish" "{\"uri\":\"$PUB_URI\",\"max_views\":5}" | mcp_result_text)
  SLUG=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["slug"])')
  for i in $(seq 1 $N_PARALLEL); do
    (curl -s --max-time $CURL_TIMEOUT "$AKB_URL/api/v1/public/$SLUG" > /dev/null) &
  done
  wait
  VC=$(pg "SELECT view_count FROM publications WHERE slug='$SLUG'")
  if [ "$VC" -le 5 ] && [ "$VC" -ge 1 ]; then
    pass "INV-4 view_count=$VC (≤5, no overshoot)"
  else
    fail "INV-4" "view_count=$VC (expected 1..5)"
  fi
fi

# ── INV-8: MCP version hex validation ─────────────────────────────────
if run_inv "INV-8"; then
  echo "▸ INV-8: MCP akb_get version=HEAD~1 rejected"
  R=$(mcp_call "$SID" "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"inv8\",\"title\":\"v\",\"content\":\"# a\",\"type\":\"note\"}" | mcp_result_text)
  URI8=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["uri"])')
  RAW=$(mcp_call "$SID" "akb_get" "{\"uri\":\"$URI8\",\"version\":\"HEAD~1\"}")
  if echo "$RAW" | grep -qE '7-64.*hex|version must be'; then
    pass "INV-8 HEAD~1 rejected with hex error"
  else
    fail "INV-8" "HEAD~1 was accepted: $(echo $RAW | head -c 200)"
  fi
fi

# ── INV-9: get_or_create concurrent same collection ───────────────────
if run_inv "INV-9"; then
  echo "▸ INV-9: N=$N_PARALLEL concurrent akb_put new collection → exactly 1 collection row"
  COLL="inv9-$(date +%s)"
  for i in $(seq 1 $N_PARALLEL); do
    (curl -s --max-time $CURL_TIMEOUT -X POST "$AKB_URL/mcp/" \
      -H "Authorization: Bearer $PAT" \
      -H "Accept: application/json, text/event-stream" \
      -H "mcp-session-id: $SID" \
      -H "Content-Type: application/json" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":9000$i,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_put\",\"arguments\":{\"vault\":\"$VAULT\",\"collection\":\"$COLL\",\"title\":\"doc$i\",\"content\":\"# d$i\",\"type\":\"note\"}}}" > /dev/null) &
  done
  wait
  CNT=$(pg "SELECT COUNT(*) FROM collections WHERE vault_id='$VID' AND path='$COLL'")
  DOC_CNT=$(pg "SELECT COUNT(*) FROM documents WHERE vault_id='$VID' AND path LIKE '$COLL/%'")
  if [ "$CNT" = "1" ] && [ "$DOC_CNT" = "$N_PARALLEL" ]; then
    pass "INV-9 1 collection / $DOC_CNT docs (no duplicate collection rows)"
  else
    fail "INV-9" "collections=$CNT docs=$DOC_CNT (expected 1/$N_PARALLEL)"
  fi
fi

# ── INV-10: unlink_resources is same-vault only at MCP surface ────────
if run_inv "INV-10"; then
  echo "▸ INV-10: MCP akb_unlink validates source/target same vault (URI-derived)"
  R=$(mcp_call "$SID" "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"inv10\",\"title\":\"a\",\"content\":\"# a\",\"type\":\"note\"}" | mcp_result_text)
  A=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["uri"])')
  R=$(mcp_call "$SID" "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"inv10\",\"title\":\"b\",\"content\":\"# b\",\"type\":\"note\"}" | mcp_result_text)
  B=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["uri"])')
  LR=$(mcp_call "$SID" "akb_link" "{\"source\":\"$A\",\"target\":\"$B\",\"relation\":\"related_to\"}")
  CLS=$(echo "$LR" | mcp_classify)
  [ "$CLS" != "OK" ] && { fail "INV-10 link" "$CLS"; }

  PRE=$(pg "SELECT COUNT(*) FROM edges WHERE source_uri='$A' AND target_uri='$B' AND kind='explicit'")
  if [ "$PRE" != "1" ]; then
    fail "INV-10 setup" "edge missing (got $PRE)"
  else
    # Cross-vault unlink: forge target URI in a DIFFERENT vault — MCP should reject
    B_FAKE="akb://different-vault/coll/inv10/doc/b.md"
    UR=$(mcp_call "$SID" "akb_unlink" "{\"source\":\"$A\",\"target\":\"$B_FAKE\",\"relation\":\"related_to\"}")
    CLS=$(echo "$UR" | mcp_classify)
    POST=$(pg "SELECT COUNT(*) FROM edges WHERE source_uri='$A' AND target_uri='$B' AND kind='explicit'")
    if [ "$POST" = "1" ] && case "$CLS" in ERR:*same\ vault*|ERR:*belong*to*the*same*) true ;; *) false ;; esac; then
      pass "INV-10 cross-vault unlink rejected; edge preserved"
    else
      fail "INV-10" "cls=$CLS post_edge=$POST"
    fi
    # Same-vault unlink works
    mcp_call "$SID" "akb_unlink" "{\"source\":\"$A\",\"target\":\"$B\",\"relation\":\"related_to\"}" > /dev/null
    FINAL=$(pg "SELECT COUNT(*) FROM edges WHERE source_uri='$A' AND target_uri='$B' AND kind='explicit'")
    [ "$FINAL" = "0" ] && pass "INV-10b same-vault unlink deletes edge" || fail "INV-10b" "edge remains: $FINAL"
  fi
fi

# ── INV-11: link + delete race — no dangling edge (separate sessions) ─
if run_inv "INV-11"; then
  echo "▸ INV-11: concurrent akb_link / akb_delete on same source (separate MCP sessions) → no dangling edge"
  SID_B=$(mcp_init)
  R=$(mcp_call "$SID" "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"inv11\",\"title\":\"src\",\"content\":\"# s\",\"type\":\"note\"}" | mcp_result_text)
  S11=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["uri"])')
  R=$(mcp_call "$SID" "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"inv11\",\"title\":\"tgt\",\"content\":\"# t\",\"type\":\"note\"}" | mcp_result_text)
  T11=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["uri"])')
  # Use separate sessions so MCP streamable HTTP doesn't serialize them
  (mcp_call "$SID"   "akb_link"   "{\"source\":\"$S11\",\"target\":\"$T11\",\"relation\":\"depends_on\"}" > /dev/null) &
  (mcp_call "$SID_B" "akb_delete" "{\"uri\":\"$S11\"}" > /dev/null) &
  wait
  # Invariant: if doc is gone, no edges from it should remain.
  DOC_PATH="inv11/src.md"
  DOC_EXIST=$(pg "SELECT COUNT(*) FROM documents WHERE vault_id='$VID' AND path='$DOC_PATH'")
  EDGE_CNT=$(pg "SELECT COUNT(*) FROM edges WHERE source_uri='$S11'")
  if [ "$DOC_EXIST" = "0" ] && [ "$EDGE_CNT" = "0" ]; then
    pass "INV-11 delete won race: no doc, no edges"
  elif [ "$DOC_EXIST" = "1" ]; then
    if [ "$EDGE_CNT" -ge 0 ]; then
      pass "INV-11 link won race: doc kept (edge_cnt=$EDGE_CNT)"
    else
      fail "INV-11" "unexpected: doc=$DOC_EXIST edge=$EDGE_CNT"
    fi
  else
    fail "INV-11" "DANGLING: doc=$DOC_EXIST edges=$EDGE_CNT"
  fi
  curl -s --max-time 5 -X DELETE "$AKB_URL/mcp/" -H "Authorization: Bearer $PAT" -H "mcp-session-id: $SID_B" > /dev/null 2>&1 || true
fi

# ── INV-12: SQL token rewriter — string literal preserved ─────────────
if run_inv "INV-12"; then
  echo "▸ INV-12: column-name substring inside string literal not rewritten"
  mcp_call "$SID" "akb_create_table" "{\"vault\":\"$VAULT\",\"name\":\"inv12_t\",\"columns\":[{\"name\":\"label\",\"type\":\"text\"},{\"name\":\"qty\",\"type\":\"number\"}]}" > /dev/null
  mcp_call "$SID" "akb_sql" "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO inv12_t (label, qty) VALUES ('label_contains_qty_word', 42)\"}" > /dev/null
  R=$(mcp_call "$SID" "akb_sql" "{\"vault\":\"$VAULT\",\"sql\":\"SELECT label FROM inv12_t WHERE qty = 42\"}" | mcp_result_text)
  LABEL=$(echo "$R" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["items"][0]["label"] if d.get("items") else "MISSING")')
  if [ "$LABEL" = "label_contains_qty_word" ]; then
    pass "INV-12 string literal preserved through token-aware rewriter"
  else
    fail "INV-12" "label corrupted: '$LABEL' (expected 'label_contains_qty_word')"
  fi
fi

# ── Cleanup MCP session ───────────────────────────────────────────────
curl -s --max-time 5 -X DELETE "$AKB_URL/mcp/" -H "Authorization: Bearer $PAT" -H "mcp-session-id: $SID" > /dev/null 2>&1 || true

echo ""
echo "════════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
if [ $FAIL -gt 0 ]; then
  echo "  Failures:"
  for f in "${FAILURES[@]}"; do echo "    - $f"; done
fi
echo "════════════════════════════════════════════"
exit $FAIL
