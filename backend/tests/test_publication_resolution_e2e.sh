#!/bin/bash
#
# AKB Publication Resolution Regression E2E
#
# A publication stores `slug` + `resource_uri` and resolves its target
# document by path (documents is UNIQUE(vault_id, path), so paths are
# reusable). Publication cleanup on document delete therefore has to be
# explicit and complete on every delete path, or a publication can outlive
# its document and a later document at the same path inherits the slug.
#
# This suite asserts the corrected behaviour: after each delete path, the
# slug must not resolve to a different document than the one it was
# published for. See the design item at
# docs/design/proposal/2026-08-04-publication-path-binding/ for background.
#
# Covered here:
#   T1 — recursive collection delete
#   T2 — move onto a path a publication still claims
#   T3 — file publications under a recursively deleted collection
#   T4 — publications when confirm_upload discards a file, both paths
#   T5 — external-git delete: covered as a pytest test instead; see the
#        note in section 5 below.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
TS=$(date +%s)
VAULT="pubres-e2e-$TS"
USER="pubres-user-$TS"
PASS=0
FAIL=0
SKIP=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }
skip() { SKIP=$((SKIP+1)); echo "  ⊘ SKIP $1 — $2"; }
info() { echo "    · $1"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB Publication Resolution Regression  ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup: register user + login + vault ───────────────
echo "▸ 0. Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

TOKEN=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])" 2>/dev/null)
[ -n "$TOKEN" ] && pass "Login as $USER" || { fail "Login" "no token"; exit 1; }

acurl() { curl -sk -H "Authorization: Bearer $TOKEN" "$@"; }

# MCP is an automation surface: a local browser/session JWT must not cross
# that capability boundary. Mint a dedicated PAT for the one MCP-only move
# below and keep TOKEN exclusively on the REST user-session path.
MCP_PAT=$(acurl -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H 'Content-Type: application/json' \
  -d '{"name":"publication-resolution-e2e-mcp"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])" 2>/dev/null)
[ -n "$MCP_PAT" ] && pass "MCP PAT acquired" || { fail "MCP PAT" "no token"; exit 1; }

R=$(acurl -X POST "$BASE_URL/api/v1/vaults?name=$VAULT&description=resolution%20regression")
[ "$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name",""))' 2>/dev/null)" = "$VAULT" ] \
  && pass "Vault created ($VAULT)" || { fail "Vault create" "$R"; exit 1; }

jfield() { python3 -c "import json,sys
try: print(json.load(sys.stdin).get('$1',''))
except Exception: print('')" 2>/dev/null; }

# Create a document. Returns "<uri>|<path>" so callers get both without a
# second round-trip; `slug` is passed explicitly so the resulting path is
# deterministic and a later recreate lands on exactly the same path.
mkdoc() {  # vault-collection, slug, title, content
  acurl -X POST "$BASE_URL/api/v1/documents" -H "Content-Type: application/json" \
    -d "{\"vault\":\"$VAULT\",\"collection\":\"$1\",\"slug\":\"$2\",\"title\":\"$3\",\"content\":\"$4\",\"type\":\"note\"}" \
  | python3 -c "import json,sys
try:
    d = json.load(sys.stdin); print(d.get('uri',''), d.get('path',''), sep='|')
except Exception: print('|')" 2>/dev/null
}

pubdoc() {  # doc uri -> slug
  acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
    -d "{\"resource_type\":\"document\",\"uri\":\"$1\"}" | jfield slug
}

code_of() { curl -sk -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/public/$1"; }

# Recursive collection delete. The envelope is
# {"kind":"collection_delete","ok":true,"deleted_docs":N,"deleted_files":N,...}
delcoll() { acurl -X DELETE "$BASE_URL/api/v1/collections/$VAULT/$1?recursive=true"; }
del_ok() { echo "$1" | grep -q '"ok":true'; }

echo ""

# ══════════════════════════════════════════════════════════
# 1. Recursive collection delete
# ══════════════════════════════════════════════════════════
# A recursive collection delete removes each `documents` row. Its
# publication cleanup has to run on that same path; the corrected code
# routes every document delete through one method that cleans publications
# under the row lock. This asserts a publication does not outlive its
# document here, and that a document recreated at the same path does not
# inherit the old slug.
echo "▸ 1. Recursive collection delete"

T1_COLL="t1-reports"
T1_OPEN="T1-FIRST-BODY"
T1_PRIVATE="T1-SECOND-BODY"

OUT=$(mkdoc "$T1_COLL" "q3" "T1 first document" "$T1_OPEN")
T1_URI="${OUT%%|*}"; T1_PATH="${OUT##*|}"
[ -n "$T1_URI" ] && pass "T1 setup: original doc created ($T1_PATH)" \
  || { fail "T1 setup" "doc create returned no uri"; }

T1_SLUG=$(pubdoc "$T1_URI")
[ -n "$T1_SLUG" ] && pass "T1 setup: published (slug=$T1_SLUG)" \
  || fail "T1 setup" "publish returned no slug"

# Sanity gate — if the slug does not serve the ORIGINAL here, every
# assertion below is meaningless and the suite is lying to you.
BEFORE=$(curl -sk "$BASE_URL/api/v1/public/$T1_SLUG")
echo "$BEFORE" | grep -q "$T1_OPEN" \
  && pass "T1 sanity: slug serves the original document" \
  || fail "T1 sanity" "slug does not serve the original: $(echo "$BEFORE" | head -c 200)"

D=$(delcoll "$T1_COLL")
del_ok "$D" \
  && pass "T1: collection deleted recursively" \
  || fail "T1 collection delete" "$D"

# Informational: this is the state that made the bug invisible for so
# long. The slug 404s while the path is vacant, so a spot-check right
# after the delete looks correct.
info "GET /public/$T1_SLUG immediately after delete -> HTTP $(code_of "$T1_SLUG") (expected 404; the bug is not visible yet)"

OUT=$(mkdoc "$T1_COLL" "q3" "T1 second document" "$T1_PRIVATE")
T1_NEW_URI="${OUT%%|*}"; T1_NEW_PATH="${OUT##*|}"
[ "$T1_NEW_PATH" = "$T1_PATH" ] \
  && pass "T1: new doc occupies the same path ($T1_NEW_PATH)" \
  || fail "T1 path reuse" "new path '$T1_NEW_PATH' != original '$T1_PATH' — collision suffix applied, test is not exercising the bug"

# ── THE ASSERTION ────────────────────────────────────────
AFTER=$(curl -sk "$BASE_URL/api/v1/public/$T1_SLUG")
AFTER_CODE=$(code_of "$T1_SLUG")
if echo "$AFTER" | grep -q "$T1_PRIVATE"; then
  fail "T1 resolution" "slug $T1_SLUG resolved to the recreated document, not the one it was published for (HTTP $AFTER_CODE)"
else
  pass "T1: old slug does NOT serve the recreated document"
fi
[ "$AFTER_CODE" != "200" ] \
  && pass "T1: old slug no longer resolves (HTTP $AFTER_CODE)" \
  || fail "T1 resolves" "publication for a deleted document still returns HTTP 200"

echo ""

# ══════════════════════════════════════════════════════════
# 2. Move onto a path a publication still claims
# ══════════════════════════════════════════════════════════
# A move rewrites publications at the source path but must not let an
# unrelated document arrive at a destination path that a publication still
# claims. This reaches the same resolution question as T1 without creating
# anything new — the corrected code refuses the move at the destination.
echo "▸ 2. Move onto a path a publication still claims"

T2_COLL="t2-board"
T2_OPEN="T2-FIRST-BODY"
T2_PRIVATE="T2-SECOND-BODY"

OUT=$(mkdoc "$T2_COLL" "minutes" "T2 first document" "$T2_OPEN")
T2_URI="${OUT%%|*}"; T2_PATH="${OUT##*|}"
[ -n "$T2_URI" ] && pass "T2 setup: original doc created ($T2_PATH)" || fail "T2 setup" "no uri"

T2_SLUG=$(pubdoc "$T2_URI")
[ -n "$T2_SLUG" ] && pass "T2 setup: published (slug=$T2_SLUG)" || fail "T2 setup" "no slug"

curl -sk "$BASE_URL/api/v1/public/$T2_SLUG" | grep -q "$T2_OPEN" \
  && pass "T2 sanity: slug serves the original document" \
  || fail "T2 sanity" "slug does not serve the original"

D=$(delcoll "$T2_COLL")
del_ok "$D" \
  && pass "T2: collection deleted recursively (publication now orphaned)" \
  || fail "T2 collection delete" "$D"

# The document that gets moved onto the claimed path. It lives somewhere
# else entirely and carries different content.
OUT=$(mkdoc "t2-other" "second" "T2 second document" "$T2_PRIVATE")
T2_MOVER_URI="${OUT%%|*}"; T2_MOVER_PATH="${OUT##*|}"
[ -n "$T2_MOVER_URI" ] && pass "T2 setup: unpublished doc created ($T2_MOVER_PATH)" || fail "T2 setup" "no mover uri"

# There is no REST move endpoint — move is MCP-only (akb_move).
SESS=$(curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $MCP_PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"pubres-e2e","version":"0.1"}}}' \
  -i 2>&1 | grep -i "mcp-session-id:" | tr -d '\r' | awk '{print $2}')
[ -n "$SESS" ] && pass "T2: MCP session initialized" || fail "T2 MCP init" "no session id"

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $MCP_PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "Mcp-Session-Id: $SESS" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null

mcp() {
  local id=$1; shift
  local name=$1; shift
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $MCP_PAT" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: $SESS" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$id,\"method\":\"tools/call\",\"params\":{\"name\":\"$name\",\"arguments\":$1}}" 2>&1
}
mcp_text() {
  python3 -c "
import json, sys, re
text = sys.stdin.read()
m = re.search(r'(\{.*\})', text, re.DOTALL)
if m:
    data = json.loads(m.group(1))
    if 'result' in data and 'content' in data['result']:
        print(data['result']['content'][0]['text'])
"
}

MV=$(mcp 20 akb_move "{\"uri\":\"$T2_MOVER_URI\",\"collection\":\"$T2_COLL\",\"slug\":\"minutes\"}" | mcp_text)
T2_MOVED_PATH=$(echo "$MV" | jfield path)
[ "$T2_MOVED_PATH" = "$T2_PATH" ] \
  && pass "T2: unpublished doc moved onto the orphaned path ($T2_MOVED_PATH)" \
  || fail "T2 move" "moved to '$T2_MOVED_PATH', expected '$T2_PATH' — response: $(echo "$MV" | head -c 240)"

# ── THE ASSERTION ────────────────────────────────────────
AFTER=$(curl -sk "$BASE_URL/api/v1/public/$T2_SLUG")
AFTER_CODE=$(code_of "$T2_SLUG")
if echo "$AFTER" | grep -q "$T2_PRIVATE"; then
  fail "T2 resolution" "slug $T2_SLUG resolved to the moved document, not the one it was published for (HTTP $AFTER_CODE)"
else
  pass "T2: old slug does NOT serve the moved document"
fi
[ "$AFTER_CODE" != "200" ] \
  && pass "T2: old slug no longer resolves (HTTP $AFTER_CODE)" \
  || fail "T2 resolves" "publication for a deleted document still returns HTTP 200"

echo ""

# ══════════════════════════════════════════════════════════
# 3. File publications under a recursively deleted collection
# ══════════════════════════════════════════════════════════
# collection_service.py:334 tears down each file's edges, chunks, S3
# object and `vault_files` row — but not its publications. A file URI
# carries a UUID (akb://V/coll/C/file/<uuid>), so unlike a document path
# it CANNOT be reoccupied. The row just outlives its
# resource. So the assertion here is that the row is GONE, not that it
# fails to be reoccupied.
#
# Needs S3 (MinIO), which both docker-compose and the CI e2e workflow
# now bring up — so this section RUNS in CI. The skip below is a
# fallback for an S3-less stack, not the expected CI path.
echo "▸ 3. File publication orphaned by recursive collection delete"

T3_COLL="t3-assets"
# --max-time: with no S3 configured the backend still round-trips boto
# (bounded by settings.s3_*_timeout_secs) before erroring. Cap it here too
# so a stalled endpoint degrades to a SKIP instead of hanging the suite.
INIT=$(acurl --max-time 60 -X POST "$BASE_URL/api/v1/files/$VAULT/upload?filename=quarterly.json&collection=$T3_COLL&mime_type=application/json")
T3_FILE_URI=$(echo "$INIT" | jfield uri)
T3_UPLOAD_URL=$(echo "$INIT" | jfield upload_url)

if [ -z "$T3_FILE_URI" ] || [ -z "$T3_UPLOAD_URL" ]; then
  skip "T3 (file publication cleanup)" "no S3 backend reachable — upload init returned: $(echo "$INIT" | head -c 160)"
else
  T3_FID="${T3_FILE_URI##*/}"
  printf '%s' '{"quarterly":"numbers"}' \
    | curl -sk --max-time 60 -X PUT "$T3_UPLOAD_URL" -H "Content-Type: application/json" --data-binary @- >/dev/null
  CONF=$(acurl -X POST "$BASE_URL/api/v1/files/$VAULT/$T3_FID/confirm")

  T3_SLUG=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
    -d "{\"resource_type\":\"file\",\"uri\":\"$T3_FILE_URI\"}" | jfield slug)

  if [ -z "$T3_SLUG" ]; then
    skip "T3 (file publication cleanup)" "could not publish the file — confirm: $(echo "$CONF" | head -c 160)"
  else
    pass "T3 setup: file uploaded + published (slug=$T3_SLUG)"

    BEFORE_CODE=$(code_of "$T3_SLUG")
    [ "$BEFORE_CODE" = "200" ] \
      && pass "T3 sanity: file slug resolves before the delete (HTTP 200)" \
      || fail "T3 sanity" "file slug returned HTTP $BEFORE_CODE before any delete"

    D=$(delcoll "$T3_COLL")
    del_ok "$D" \
      && pass "T3: collection deleted recursively" \
      || fail "T3 collection delete" "$D"

    # ── THE ASSERTION ────────────────────────────────────
    # The row must be gone, not merely unresolvable. A dangling row is
    # still a live slug in the owner's publication list pointing at a
    # resource that no longer exists.
    LIST=$(acurl "$BASE_URL/api/v1/publications/$VAULT")
    if echo "$LIST" | grep -q "\"$T3_SLUG\""; then
      fail "T3 ORPHAN ROW" "publication $T3_SLUG survived the recursive delete of its file's collection"
    else
      pass "T3: publication row removed with the file"
    fi

    AFTER_CODE=$(code_of "$T3_SLUG")
    [ "$AFTER_CODE" != "200" ] \
      && pass "T3: file slug no longer resolves (HTTP $AFTER_CODE)" \
      || fail "T3 resolves" "file publication still returns HTTP 200 after its file was deleted"
  fi
fi

echo ""

# ══════════════════════════════════════════════════════════
# 4. Publications orphaned by a FAILED confirm_upload
# ══════════════════════════════════════════════════════════
# A file is publishable the moment `initiate_upload` writes its
# `vault_files` row: `create_publication`'s existence check is
# `SELECT 1 FROM vault_files WHERE id=$1 AND vault_id=$2` — no confirmed
# state, no `hash_verified_at` predicate. `confirm_upload` then has TWO
# paths that discard the row, and neither used to delete its publication:
#
#   file_service.py:338  S3 object missing  -> 404 (client abandoned the PUT)
#   file_service.py:364  bytes disowned     -> 409 (declared hash mismatch)
#
# Like T3 this is a stale row, not a reoccupied path — a file URI carries a UUID, so
# the row cannot be reoccupied. The assertion is that the row is GONE.
#
# The CONTROL at the end is what keeps this section honest: an
# implementation that deleted publications unconditionally on every
# confirm would pass both discard cases and fail the control.
#
# Needs S3 (MinIO), which CI now brings up — this RUNS in CI. The
# skip below is a fallback for an S3-less stack, not the CI path.
echo "▸ 4. Publications orphaned by a failed confirm_upload"

# Publish a file that has NOT been confirmed yet. Echoes "<file_id>|<slug>",
# or "" when S3 is unreachable so the caller can skip.
mkunconfirmed() {  # collection, filename -> "<fid>|<slug>" (+ optional upload)
  local init uri fid slug upload
  init=$(acurl --max-time 60 -X POST \
    "$BASE_URL/api/v1/files/$VAULT/upload?filename=$2&collection=$1&mime_type=application/json")
  uri=$(echo "$init" | jfield uri)
  upload=$(echo "$init" | jfield upload_url)
  [ -z "$uri" ] && { echo ""; return; }
  fid="${uri##*/}"
  # Optionally park real bytes at the presigned URL (path B needs an object
  # that EXISTS so the failure is the hash check, not a missing object).
  if [ "${3:-}" = "upload" ]; then
    printf '%s' '{"real":"bytes"}' \
      | curl -sk --max-time 60 -X PUT "$upload" -H "Content-Type: application/json" --data-binary @- >/dev/null
  fi
  slug=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" \
    -H "Content-Type: application/json" \
    -d "{\"resource_type\":\"file\",\"uri\":\"$uri\"}" | jfield slug)
  [ -z "$slug" ] && { echo ""; return; }
  echo "$fid|$slug"
}

pub_listed() {  # slug -> 0 if the owner's publication list still carries it
  acurl "$BASE_URL/api/v1/publications/$VAULT" | grep -q "\"$1\""
}

OUT=$(mkunconfirmed "t4-abandoned" "abandoned.json")
if [ -z "$OUT" ]; then
  skip "T4 (failed-confirm cleanup)" "no S3 backend reachable"
else
  # ── Path A: the client never completed the presigned PUT ──
  T4A_FID="${OUT%%|*}"; T4A_SLUG="${OUT##*|}"
  pass "T4a setup: unconfirmed file published (slug=$T4A_SLUG)"

  # Sanity: the slug must genuinely serve the file BEFORE the failed
  # confirm, or the "it's gone afterwards" assertion proves nothing.
  [ "$(code_of "$T4A_SLUG")" = "200" ] \
    && pass "T4a sanity: slug resolves before the failed confirm (HTTP 200)" \
    || fail "T4a sanity" "slug returned HTTP $(code_of "$T4A_SLUG") before any confirm"

  C=$(acurl -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/api/v1/files/$VAULT/$T4A_FID/confirm")
  [ "$C" = "404" ] \
    && pass "T4a: confirm rejected the abandoned upload (HTTP 404)" \
    || fail "T4a confirm" "expected HTTP 404 for a missing S3 object, got $C"

  pub_listed "$T4A_SLUG" \
    && fail "T4a ORPHAN ROW" "publication $T4A_SLUG survived the discard of its unconfirmed file" \
    || pass "T4a: publication row removed with the discarded file"

  A=$(code_of "$T4A_SLUG")
  [ "$A" != "200" ] \
    && pass "T4a: slug no longer resolves (HTTP $A)" \
    || fail "T4a resolves" "publication still returns HTTP 200 after its file was discarded"

  # ── Path B: bytes present, but not the bytes that were declared ──
  OUT=$(mkunconfirmed "t4-mismatch" "mismatch.json" upload)
  if [ -z "$OUT" ]; then
    skip "T4b (hash-mismatch cleanup)" "could not stage the uploaded file"
  else
    T4B_FID="${OUT%%|*}"; T4B_SLUG="${OUT##*|}"
    pass "T4b setup: uploaded-but-unconfirmed file published (slug=$T4B_SLUG)"

    [ "$(code_of "$T4B_SLUG")" = "200" ] \
      && pass "T4b sanity: slug resolves before the failed confirm (HTTP 200)" \
      || fail "T4b sanity" "slug returned HTTP $(code_of "$T4B_SLUG") before any confirm"

    # `content_hash` is a QUERY parameter, not a body field — passing it in
    # the body silently confirms SUCCESSFULLY and the test would prove nothing.
    WRONG_HASH=$(printf 'a%.0s' $(seq 1 64))
    C=$(acurl -o /dev/null -w '%{http_code}' -X POST \
      "$BASE_URL/api/v1/files/$VAULT/$T4B_FID/confirm?content_hash=$WRONG_HASH")
    [ "$C" = "409" ] \
      && pass "T4b: confirm rejected the disowned bytes (HTTP 409)" \
      || fail "T4b confirm" "expected HTTP 409 for a hash mismatch, got $C"

    pub_listed "$T4B_SLUG" \
      && fail "T4b ORPHAN ROW" "publication $T4B_SLUG survived the hash-mismatch discard" \
      || pass "T4b: publication row removed with the discarded file"

    B=$(code_of "$T4B_SLUG")
    [ "$B" != "200" ] \
      && pass "T4b: slug no longer resolves (HTTP $B)" \
      || fail "T4b resolves" "publication still returns HTTP 200 after its file was discarded"
  fi

  # ── CONTROL: a SUCCESSFUL confirm must KEEP its publication ──
  # Without this, "delete publications on every confirm" passes T4a+T4b.
  OUT=$(mkunconfirmed "t4-control" "control.json" upload)
  if [ -z "$OUT" ]; then
    skip "T4 control" "could not stage the control file"
  else
    T4C_FID="${OUT%%|*}"; T4C_SLUG="${OUT##*|}"
    C=$(acurl -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/api/v1/files/$VAULT/$T4C_FID/confirm")
    [ "$C" = "200" ] \
      && pass "T4 control: a normal confirm still succeeds (HTTP 200)" \
      || fail "T4 control confirm" "expected HTTP 200 for a valid confirm, got $C"

    pub_listed "$T4C_SLUG" \
      && pass "T4 control: successful confirm KEEPS its publication" \
      || fail "T4 control ORPHAN" "a successful confirm deleted the publication — cleanup is firing unconditionally"

    C=$(code_of "$T4C_SLUG")
    [ "$C" = "200" ] \
      && pass "T4 control: slug still resolves after a successful confirm (HTTP 200)" \
      || fail "T4 control resolves" "confirmed file's publication returned HTTP $C"
  fi
fi

echo ""

# ══════════════════════════════════════════════════════════
# 5. External-git delete variant — INTENTIONALLY ABSENT
# ══════════════════════════════════════════════════════════
# external_git_service.py:616 deletes an external_git-sourced `documents`
# row with no publication cascade either, so the same applies when
# an upstream commit deletes a mirrored file and a later commit re-adds
# one at the same path.
#
# It is NOT testable from a shell e2e and is deliberately not faked here:
#
#   * The delete only fires from the reconcile loop, driven by a real
#     upstream commit that removes the path. That needs a repo we can
#     push to — the existing shell suite (test_external_git_e2e.sh)
#     mirrors a read-only public GitHub repo precisely because it cannot
#     mutate an upstream.
#   * A local bare repo is not a substitute: the mirror-URL validator is
#     fail-closed on globally-routable unicast addresses, so a
#     127.0.0.1 / file:// fixture is rejected outright by a live backend.
#     The in-process fixture that gets around this
#     (`backend/tests/extgit_http.py`) works by injecting a fake resolver
#     plus a matching (host, CIDR, port) allowlist into `Settings` — it
#     is a pytest fixture and cannot reach a backend over HTTP.
#
# Cover it as a pytest test built on `tests/extgit_http.py` instead.

# ── 99. Cleanup ───────────────────────────────────────────
echo "▸ 99. Cleanup"
mcp 99 akb_delete_vault "{\"vault\":\"$VAULT\"}" >/dev/null 2>&1
pass "Cleanup done"
curl -sk -X DELETE "$BASE_URL/mcp/" -H "Authorization: Bearer $MCP_PAT" -H "Mcp-Session-Id: ${SESS:-}" >/dev/null 2>&1

echo ""
echo "╔══════════════════════════════════════════╗"
printf "║   Results: %d passed, %d failed%s║\n" "$PASS" "$FAIL" "$(printf '%*s' $((22-${#PASS}-${#FAIL})) '')"
echo "╚══════════════════════════════════════════╝"
[ "$SKIP" -gt 0 ] && echo "   ($SKIP skipped)"

if [ $FAIL -gt 0 ]; then
  echo ""
  echo "Errors:"
  for e in "${ERRORS[@]}"; do
    echo "  • $e"
  done
  exit 1
fi
exit 0
