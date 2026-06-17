#!/bin/bash
#
# AKB OKF Export/Import E2E Test Suite
# Exercises the knowledge-bundle export/import surface end-to-end over both
# transports: MCP tools (akb_export / akb_import) and REST
# (GET /vaults/{v}/export, POST /vaults/{v}/import). No S3 / no search deps,
# so it runs under the CI live-stack (embed-stubbed, MinIO-less) job.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
SRC_VAULT="okf-src-$(date +%s)"
DST_VAULT="okf-dst-$(date +%s)"
REST_VAULT="okf-rest-$(date +%s)"
E2E_USER="okf-user-$(date +%s)"
READER_USER="okf-reader-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }
note() { echo "  • $1"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB OKF Export/Import E2E Test Suite   ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup ──────────────────────────────────────────────────
echo "▸ 0. Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"okf-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# Reader-only user (for access-control assertion)
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$READER_USER\",\"email\":\"$READER_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
READER_JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$READER_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
READER_PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $READER_JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"okf-reader"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

# MCP session (writer)
INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"okf-e2e","version":"1.0"}}}' 2>&1)
SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "MCP session ($SID)" || { fail "MCP" "no session"; exit 1; }
curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

# MCP session (reader)
READER_INIT=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $READER_PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"okf-reader","version":"1.0"}}}' 2>&1)
READER_SID=$(echo "$READER_INIT" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $READER_PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $READER_SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

MCP_ID=10
mcp_call() {
  local tool=$1 args=$2
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}
mcp_call_reader() {
  local tool=$1 args=$2
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $READER_PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $READER_SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}
mcp_result() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null; }

# ── 1. Source vault with documents (+ a table, soft) ──────────
echo ""
echo "▸ 1. Source vault content"
R=$(mcp_call akb_create_vault "{\"name\":\"$SRC_VAULT\",\"description\":\"OKF export source\"}" | mcp_result)
echo "$R" | grep -q '"vault_id"' && pass "Source vault created" || { fail "Vault" "no vault_id: $R"; exit 1; }

D1=$(mcp_call akb_put "{\"vault\":\"$SRC_VAULT\",\"collection\":\"specs\",\"title\":\"API v2\",\"content\":\"# API v2\\n\\nSpec body.\",\"type\":\"spec\",\"status\":\"active\",\"tags\":[\"api\"]}" | mcp_result)
echo "$D1" | grep -q '"uri"' && pass "Doc 1 created (specs/)" || fail "Doc1" "$D1"
D2=$(mcp_call akb_put "{\"vault\":\"$SRC_VAULT\",\"title\":\"Readme\",\"content\":\"# Readme\\n\\nRoot doc.\",\"type\":\"note\"}" | mcp_result)
echo "$D2" | grep -q '"uri"' && pass "Doc 2 created (root)" || fail "Doc2" "$D2"

# Table is best-effort: column type names are environment-dependent; the
# concept-doc transform itself is covered by the unit suite.
TBL=$(mcp_call akb_create_table "{\"vault\":\"$SRC_VAULT\",\"name\":\"metrics\",\"columns\":[{\"name\":\"region\",\"type\":\"text\"},{\"name\":\"hits\",\"type\":\"integer\"}]}" | mcp_result)
HAS_TABLE=0
if echo "$TBL" | grep -q '"uri"'; then HAS_TABLE=1; note "table 'metrics' created"; else note "table create skipped ($TBL)"; fi

# ── 2. MCP export ─────────────────────────────────────────────
echo ""
echo "▸ 2. Export via MCP (akb_export)"
EXPORT=$(mcp_call akb_export "{\"vault\":\"$SRC_VAULT\",\"format\":\"okf\"}" | mcp_result)
FILE_COUNT=$(echo "$EXPORT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("file_count",0))' 2>/dev/null)
[ "${FILE_COUNT:-0}" -gt 0 ] 2>/dev/null && pass "Export returned $FILE_COUNT files" || { fail "Export" "no files: $EXPORT"; }

# Bundle structure assertions
echo "$EXPORT" | python3 -c '
import sys,json
d=json.load(sys.stdin); f=d.get("files",{})
assert "index.md" in f, "no index.md"
assert "okf_version" in f["index.md"], "root index.md missing okf_version"
assert any(p.endswith("specs/api-v2.md") or p=="specs/api-v2.md" for p in f), "doc path missing: "+str(list(f))
assert "log.md" in f, "no log.md"
' 2>/tmp/okf_struct_err && pass "Bundle has index.md(+okf_version), log.md, doc paths" || fail "Structure" "$(cat /tmp/okf_struct_err)"

# Conformance: every concept doc has frontmatter + non-empty type
echo "$EXPORT" | python3 -c '
import sys,json,re
d=json.load(sys.stdin); f=d.get("files",{})
bad=[]
for path,txt in f.items():
    name=path.rsplit("/",1)[-1]
    if name in ("index.md","log.md"): continue
    m=re.match(r"^---\n(.*?)\n---\n", txt, re.DOTALL)
    if not m: bad.append(path+":no-frontmatter"); continue
    if not re.search(r"^type:\s*\S", m.group(1), re.MULTILINE): bad.append(path+":no-type")
assert not bad, "non-conformant: "+str(bad)
' 2>/tmp/okf_conf_err && pass "All concept docs conformant (frontmatter+type)" || fail "Conformance" "$(cat /tmp/okf_conf_err)"

if [ "$HAS_TABLE" = "1" ]; then
  echo "$EXPORT" | python3 -c '
import sys,json
d=json.load(sys.stdin); f=d.get("files",{})
assert any("type: table" in t for t in f.values()), "no table concept in bundle"
' 2>/dev/null && pass "Table exported as OKF concept doc" || fail "TableConcept" "table concept missing"
fi

# ── 3. MCP import round-trip ──────────────────────────────────
echo ""
echo "▸ 3. Import via MCP (akb_import)"
mcp_call akb_create_vault "{\"name\":\"$DST_VAULT\",\"description\":\"OKF import target\"}" >/dev/null 2>&1
IMPORT_ARGS=$(echo "$EXPORT" | DST="$DST_VAULT" python3 -c 'import sys,json,os; d=json.load(sys.stdin); print(json.dumps({"vault":os.environ["DST"],"files":d["files"],"status":"active"}))')
IMPORT=$(mcp_call akb_import "$IMPORT_ARGS" | mcp_result)
CREATED=$(echo "$IMPORT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("created",0))' 2>/dev/null)
FAILED_N=$(echo "$IMPORT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("failed",-1))' 2>/dev/null)
[ "${CREATED:-0}" -ge 2 ] 2>/dev/null && pass "Imported $CREATED docs" || fail "Import" "created=$CREATED: $IMPORT"
[ "${FAILED_N:-1}" = "0" ] && pass "No import failures" || fail "ImportFailures" "failed=$FAILED_N: $IMPORT"

# Verify the imported docs exist in the target vault
BROWSE=$(mcp_call akb_browse "{\"vault\":\"$DST_VAULT\",\"depth\":-1,\"content_type\":\"documents\"}" | mcp_result)
DOC_N=$(echo "$BROWSE" | python3 -c 'import sys,json; print(len([i for i in json.load(sys.stdin).get("items",[]) if i.get("type")=="document"]))' 2>/dev/null)
[ "${DOC_N:-0}" -ge 2 ] 2>/dev/null && pass "Target vault has $DOC_N documents" || fail "Verify" "doc count=$DOC_N"

# ── 4. REST export (json + zip) ───────────────────────────────
echo ""
echo "▸ 4. Export via REST"
REST_JSON=$(curl -sk "$BASE_URL/api/v1/vaults/$SRC_VAULT/export?format=okf&as=json" -H "Authorization: Bearer $JWT")
echo "$REST_JSON" | python3 -c 'import sys,json; assert json.load(sys.stdin)["file_count"]>0' 2>/dev/null && pass "REST export (as=json) ok" || fail "RESTjson" "$REST_JSON"

curl -sk "$BASE_URL/api/v1/vaults/$SRC_VAULT/export?format=okf" -H "Authorization: Bearer $JWT" -o /tmp/okf_bundle.zip
[ "$(head -c 2 /tmp/okf_bundle.zip)" = "PK" ] && pass "REST export (zip) returns PK archive" || fail "RESTzip" "not a zip"

# Format guard
GUARD=$(curl -sk "$BASE_URL/api/v1/vaults/$SRC_VAULT/export?format=rdf" -H "Authorization: Bearer $JWT")
echo "$GUARD" | grep -qi "unsupported format" && pass "Unsupported format rejected (400)" || fail "Guard" "$GUARD"

# ── 5. REST import (zip multipart) ────────────────────────────
echo ""
echo "▸ 5. Import via REST (zip upload)"
mcp_call akb_create_vault "{\"name\":\"$REST_VAULT\",\"description\":\"OKF REST import target\"}" >/dev/null 2>&1
REST_IMPORT=$(curl -sk -X POST "$BASE_URL/api/v1/vaults/$REST_VAULT/import?format=okf&status=active" \
  -H "Authorization: Bearer $JWT" -F "file=@/tmp/okf_bundle.zip")
RC_CREATED=$(echo "$REST_IMPORT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("created",0))' 2>/dev/null)
[ "${RC_CREATED:-0}" -ge 2 ] 2>/dev/null && pass "REST import created $RC_CREATED docs" || fail "RESTimport" "$REST_IMPORT"

# ── 6. Access control ─────────────────────────────────────────
echo ""
echo "▸ 6. Access control"
# Reader has no membership on DST_VAULT → import must be refused.
DENY=$(mcp_call_reader akb_import "{\"vault\":\"$DST_VAULT\",\"files\":{\"a.md\":\"---\\ntype: note\\n---\\nx\"}}" | mcp_result)
echo "$DENY" | grep -q '"error"' && pass "Reader import refused" || fail "ACL" "expected error: $DENY"

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "═════════════════════════════════════════"
echo "Results: $PASS passed, $FAIL failed"
echo "═════════════════════════════════════════"
if [ "$FAIL" -gt 0 ]; then
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
exit 0
