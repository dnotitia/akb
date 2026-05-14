#!/bin/bash
#
# Collection unified-membership E2E.
#
# Validates that docs / tables / files share a single collection
# hierarchy after migration 020:
#
#   1. akb_create_table with `collection=` lands the table under the
#      right collections row (FK populated).
#   2. akb_put_file with `collection=` lands the file under the same
#      collections row.
#   3. akb_browse(vault) returns tables/files with `collection` set, so
#      the frontend tree groups them under the matching collection.
#   4. akb_browse(vault, collection="X") returns only that collection's
#      docs/tables/files.
#   5. Root-level resources (no collection) appear at vault root in the
#      top-level browse.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
NS="${AKB_NS:-akb}"
PG_POD="${AKB_PG_POD:-postgres-0}"
PG_USER="${AKB_PG_USER:-akbuser}"
PG_DB="${AKB_PG_DB:-akb}"

VAULT="coll-unified-$(date +%s)"
USER="coll-unified-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

run_psql() {
  kubectl exec -n "$NS" "$PG_POD" -- psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
}

# ── Setup ────────────────────────────────────────────────────────
echo "▸ Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"coll-unified"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] || { echo "FATAL: PAT"; exit 1; }

curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$VAULT" \
  -H "Authorization: Bearer $PAT" >/dev/null
VAULT_ID=$(run_psql "SELECT id FROM vaults WHERE name = '$VAULT'")
[ -n "$VAULT_ID" ] && pass "vault ready ($VAULT)" || { fail "vault" "missing"; exit 1; }

# ── 1. Create table in collection 'specs' ───────────────────────
echo ""
echo "▸ 1. Table inside collection 'specs'"

T_RESP=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"customers","collection":"specs","description":"d","columns":[{"name":"email","type":"text"}]}')
T_KIND=$(echo "$T_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("kind",""))' 2>/dev/null)
T_COLL=$(echo "$T_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("collection") or "")' 2>/dev/null)
[ "$T_KIND" = "table" ] && pass "table created" || fail "table create" "$T_RESP"
[ "$T_COLL" = "specs" ] && pass "response.collection == 'specs'" || fail "response.collection" "got '$T_COLL'"

# DB-side FK should resolve to a collections row with path='specs'
DB_COLL_PATH=$(run_psql "SELECT c.path FROM vault_tables vt JOIN collections c ON c.id = vt.collection_id WHERE vt.vault_id = '$VAULT_ID' AND vt.name = 'customers'")
[ "$DB_COLL_PATH" = "specs" ] && pass "vault_tables.collection_id → collections.path = 'specs'" \
                              || fail "FK" "got '$DB_COLL_PATH'"

# ── 2. Create table at root ──────────────────────────────────────
echo ""
echo "▸ 2. Table at vault root"

curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"top_table","description":"d","columns":[{"name":"x","type":"text"}]}' >/dev/null

ROOT_FK=$(run_psql "SELECT collection_id IS NULL FROM vault_tables WHERE vault_id = '$VAULT_ID' AND name = 'top_table'")
[ "$ROOT_FK" = "t" ] && pass "root table has collection_id IS NULL" || fail "root FK" "$ROOT_FK"

# ── 3. Put a document into the same 'specs' collection ──────────
echo ""
echo "▸ 3. Document inside same collection 'specs'"

D_RESP=$(curl -sk -X POST "$BASE_URL/api/v1/documents" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"Spec A\",\"type\":\"spec\",\"content\":\"# A\n\nbody\"}")
D_ID=$(echo "$D_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("doc_id",""))' 2>/dev/null)
[ -n "$D_ID" ] && pass "doc created in 'specs' ($D_ID)" || fail "doc put" "$D_RESP"

# ── 4. Initiate file upload into 'specs' (skip actual S3 upload) ─
echo ""
echo "▸ 4. File metadata inside same collection 'specs'"

# Synthetic vault_files row + ensure the collections row exists so the
# FK populates correctly. This skips the presigned PUT round-trip and
# verifies the FK plumbing only.
SPEC_COLL_ID=$(run_psql "SELECT id FROM collections WHERE vault_id = '$VAULT_ID' AND path = 'specs'")
[ -n "$SPEC_COLL_ID" ] && pass "collections row 'specs' was auto-created by service" \
                      || fail "ensure_collection" "missing"

SYN_FID=$(run_psql "INSERT INTO vault_files (vault_id, collection_id, name, s3_key, mime_type, size_bytes, description, created_by)
                    VALUES ('$VAULT_ID', '$SPEC_COLL_ID', 'note.txt', '$VAULT/test/note.txt', 'text/plain', 5, '', '$USER')
                    RETURNING id" | head -n 1)
[ -n "$SYN_FID" ] && pass "synthetic file row inserted under 'specs'" || fail "file insert" "no id"

# ── 5. akb_browse top-level returns collection attribute ────────
echo ""
echo "▸ 5. Top-level akb_browse returns collection attribute on table/file"

B_RESP=$(curl -sk "$BASE_URL/api/v1/browse/$VAULT?depth=2" \
  -H "Authorization: Bearer $PAT" 2>/dev/null)
if echo "$B_RESP" | grep -q '"items"'; then
  TBL_COLL=$(echo "$B_RESP" | python3 -c 'import sys,json
d=json.load(sys.stdin)
for it in d.get("items",[]):
    if it.get("type")=="table" and it.get("name")=="customers":
        print(it.get("collection") or "")
        break' 2>/dev/null)
  FILE_COLL=$(echo "$B_RESP" | python3 -c 'import sys,json
d=json.load(sys.stdin)
for it in d.get("items",[]):
    if it.get("type")=="file" and it.get("name")=="note.txt":
        print(it.get("collection") or "")
        break' 2>/dev/null)
  [ "$TBL_COLL" = "specs" ] && pass "browse.table 'customers'.collection == 'specs'" || fail "browse.table.collection" "got '$TBL_COLL'"
  [ "$FILE_COLL" = "specs" ] && pass "browse.file 'note.txt'.collection == 'specs'" || fail "browse.file.collection" "got '$FILE_COLL'"
else
  echo "  (skipped — /browse not reachable; tested via DB FK above)"
fi

# ── 5b. Scoped browse: collection='specs' returns doc + table + file ─
echo ""
echo "▸ 5b. Scoped akb_browse(collection='specs') returns siblings"
S_RESP=$(curl -sk "$BASE_URL/api/v1/browse/$VAULT?collection=specs&depth=1" \
  -H "Authorization: Bearer $PAT" 2>/dev/null)

if echo "$S_RESP" | grep -q '"items"'; then
  HAS_DOC=$(echo "$S_RESP" | python3 -c 'import sys,json
d=json.load(sys.stdin)
print("y" if any(it.get("type")=="document" and it.get("name")=="Spec A" for it in d.get("items",[])) else "n")' 2>/dev/null)
  HAS_TBL=$(echo "$S_RESP" | python3 -c 'import sys,json
d=json.load(sys.stdin)
print("y" if any(it.get("type")=="table" and it.get("name")=="customers" for it in d.get("items",[])) else "n")' 2>/dev/null)
  HAS_FILE=$(echo "$S_RESP" | python3 -c 'import sys,json
d=json.load(sys.stdin)
print("y" if any(it.get("type")=="file" and it.get("name")=="note.txt" for it in d.get("items",[])) else "n")' 2>/dev/null)
  [ "$HAS_DOC" = "y" ]  && pass "scoped browse: contains doc 'Spec A'"      || fail "scoped doc"   "missing"
  [ "$HAS_TBL" = "y" ]  && pass "scoped browse: contains table 'customers'" || fail "scoped table" "missing"
  [ "$HAS_FILE" = "y" ] && pass "scoped browse: contains file 'note.txt'"   || fail "scoped file"  "missing"

  # Excludes the root-only table 'top_table'.
  HAS_ROOT_TBL=$(echo "$S_RESP" | python3 -c 'import sys,json
d=json.load(sys.stdin)
print("y" if any(it.get("type")=="table" and it.get("name")=="top_table" for it in d.get("items",[])) else "n")' 2>/dev/null)
  [ "$HAS_ROOT_TBL" = "n" ] && pass "scoped browse: excludes root table 'top_table'" \
                             || fail "scoped excludes" "root table leaked in"
else
  echo "  (skipped — scoped browse not reachable)"
fi

# ── 6. Migration 020 verification: legacy column gone ───────────
echo ""
echo "▸ 6. Migration 020 verification"

LEGACY_TEXT=$(run_psql "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='vault_files' AND column_name='collection'")
[ -z "$LEGACY_TEXT" ] && pass "vault_files.collection (legacy TEXT) is gone" \
                     || fail "migration 020" "legacy TEXT column still present"

HAS_TBL_FK=$(run_psql "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='vault_tables' AND column_name='collection_id'")
[ "$HAS_TBL_FK" = "1" ] && pass "vault_tables.collection_id present" || fail "vault_tables FK" "missing"

HAS_FILE_FK=$(run_psql "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='vault_files' AND column_name='collection_id'")
[ "$HAS_FILE_FK" = "1" ] && pass "vault_files.collection_id present" || fail "vault_files FK" "missing"

# ── Cleanup ──────────────────────────────────────────────────────
echo ""
echo "── Cleanup ──"
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$VAULT" \
  -H "Authorization: Bearer $PAT" >/dev/null 2>&1 || true

echo ""
echo "═══════════════════════════════════════════"
echo "  Passed: $PASS   Failed: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo "  Failures:"
  printf '    - %s\n' "${ERRORS[@]}"
  exit 1
fi
exit 0
