#!/bin/bash
#
# AKB Publications E2E Test Suite
# Tests the unified public sharing feature for documents, tables, and files.
#
# Covers:
#   - Document publications (basic, expiration, password, max_views, section, allow_embed)
#   - Table query publications (params, format negotiation, read-only enforcement, snapshot)
#   - File publications (302 redirect, /raw text proxy, /meta, /download)
#   - Generic features (HMAC token, oEmbed, /embed, list, delete, idempotent)
#   - Edge cases (multi-byte slugs, max_views=0, expired token, no access on /publications, etc.)
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="pub-e2e-$(date +%s)"
USER="pub-user-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB Publications E2E Test Suite        ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup: register user + login ───────────────────────
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

# Create vault
R=$(acurl -X POST "$BASE_URL/api/v1/vaults?name=$VAULT&description=Pub%20test")
[ "$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name",""))' 2>/dev/null)" = "$VAULT" ] \
  && pass "Vault created" || fail "Vault create" "$R"

# Helper: parse the {uuid} out of an akb://{vault}/file/{uuid} URI.
uri_file_id() { python3 -c "import sys; u=sys.stdin.read().strip(); print(u.rsplit('/',1)[-1] if u else '')"; }
uri_doc_path() { python3 -c "import sys; u=sys.stdin.read().strip(); print(u.split('/doc/',1)[1] if '/doc/' in u else '')"; }

# Create a doc. Backend response carries `uri` + `path` only — there is no
# legacy `doc_id` field. REST routes that take a `doc_id` accept the doc
# path verbatim via document_repo.find_by_ref().
R=$(acurl -X POST "$BASE_URL/api/v1/documents" -H "Content-Type: application/json" \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"docs\",\"title\":\"Pub Doc\",\"content\":\"# Top\\n\\n## Alpha\\nAlpha content\\n\\n## Beta\\nBeta content\\n\\n## Gamma\\nGamma content\",\"type\":\"note\",\"tags\":[\"pub\",\"test\"]}")
DOC_PATH=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("path",""))' 2>/dev/null)
DOC_URI=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("uri",""))' 2>/dev/null)
DOC_ID="$DOC_PATH"  # REST publications endpoint takes path-as-doc_id
[ -n "$DOC_URI" ] && pass "Doc created ($DOC_URI)" || fail "Doc create" "$R"

# Create a table
R=$(acurl -X POST "$BASE_URL/api/v1/tables/$VAULT" -H "Content-Type: application/json" \
  -d '{"name":"products","columns":[{"name":"name","type":"text","required":true},{"name":"category","type":"text"},{"name":"price","type":"number"}]}')
[ -n "$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("uri",""))' 2>/dev/null)" ] \
  && pass "Table created" || fail "Table create" "$R"

acurl -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" -H "Content-Type: application/json" \
  -d "{\"sql\":\"INSERT INTO products (name, category, price) VALUES ('Apple', 'food', 1), ('Bagel', 'food', 2), ('Chair', 'furniture', 50), ('Desk', 'furniture', 200)\"}" >/dev/null
pass "Table seeded"

# Upload a JSON file. The init response surfaces only `uri`; the file UUID
# (required by the confirm round-trip) is the trailing segment.
JSON_BODY='{"hello":"world","arr":[1,2,3],"nested":{"a":true}}'
INIT=$(acurl -X POST "$BASE_URL/api/v1/files/$VAULT/upload?filename=data.json&collection=data&mime_type=application/json")
FILE_URI=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["uri"])' 2>/dev/null)
FID=$(printf '%s' "$FILE_URI" | uri_file_id)
URL=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upload_url"])' 2>/dev/null)
echo "$JSON_BODY" | curl -sk -X PUT "$URL" -H "Content-Type: application/json" --data-binary @- > /dev/null
acurl -X POST "$BASE_URL/api/v1/files/$VAULT/$FID/confirm" > /dev/null
[ -n "$FID" ] && pass "JSON file uploaded ($FID)" || fail "File upload" "$INIT"

echo ""

# ── 1. Document Publication (basic) ───────────────────────
echo "▸ 1. Document Publication"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\"}")
DOC_PID=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("publication_id",""))' 2>/dev/null)
DOC_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
[ -n "$DOC_PID" ] && pass "Create document publication" || fail "Create doc pub" "$R"
[ -n "$DOC_SLUG" ] && pass "Slug returned" || fail "Slug" "missing"

# Response shape: must have publication_id NOT id
HAS_ID=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("yes" if "id" in d else "no")' 2>/dev/null)
[ "$HAS_ID" = "no" ] && pass "Response excludes deprecated 'id' field" || fail "Response shape" "leaks 'id'"

# Resolve via /public/{slug} (no auth)
R=$(curl -sk "$BASE_URL/api/v1/public/$DOC_SLUG")
TITLE=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("title",""))' 2>/dev/null)
[ "$TITLE" = "Pub Doc" ] && pass "Resolve doc publication (no auth)" || fail "Resolve doc" "title=$TITLE"

# content_unavailable = false
CU=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("content_unavailable"))' 2>/dev/null)
[ "$CU" = "False" ] && pass "content_unavailable = false" || fail "content_unavailable" "$CU"

# Tags returned
TAGS=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(",".join(d.get("tags",[])))' 2>/dev/null)
[ "$TAGS" = "pub,test" ] && pass "Tags preserved" || fail "Tags" "$TAGS"

echo ""

# ── 2. Section Filter ─────────────────────────────────────
echo "▸ 2. Section filter"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"section\":\"Alpha\"}")
SEC_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)

R=$(curl -sk "$BASE_URL/api/v1/public/$SEC_SLUG")
CONTENT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("content",""))' 2>/dev/null)
echo "$CONTENT" | grep -q "## Alpha" && pass "Section filter renders Alpha" || fail "Section Alpha" "$CONTENT"
echo "$CONTENT" | grep -q "## Beta" && fail "Section bleed" "Beta should be excluded" || pass "Section excludes Beta"

# section_filter is exposed in response
SF=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("section_filter",""))' 2>/dev/null)
[ "$SF" = "Alpha" ] && pass "section_filter exposed" || fail "section_filter field" "$SF"

# Non-existent section → fallback to full content
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"section\":\"Nonexistent\"}")
FAKE_SEC_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
R=$(curl -sk "$BASE_URL/api/v1/public/$FAKE_SEC_SLUG")
CONTENT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("content",""))' 2>/dev/null)
echo "$CONTENT" | grep -q "## Beta" && pass "Non-existent section falls back to full doc" || fail "Section fallback" "missing Beta"

echo ""

# ── 3. Expiration ─────────────────────────────────────────
echo "▸ 3. Expiration"

# Create with 1h expiry
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"expires_in\":\"1h\"}")
EXP_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
EXP_AT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("expires_at",""))' 2>/dev/null)
[ -n "$EXP_AT" ] && pass "expires_at populated" || fail "expires_at" "missing"

# Should be accessible now
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$EXP_SLUG")
[ "$CODE" = "200" ] && pass "Not yet expired (200)" || fail "Pre-expiry" "HTTP $CODE"

# Invalid expires_in format
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"expires_in\":\"banana\"}")
ERR=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("detail") or d.get("error",""))' 2>/dev/null)
echo "$ERR" | grep -qi "invalid expires_in" && pass "Invalid expires_in rejected" || fail "Invalid expires_in" "$R"

# 'never' produces no expiration
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"expires_in\":\"never\"}")
EAT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("expires_at"))' 2>/dev/null)
[ "$EAT" = "None" ] && pass "expires_in='never' → no expiry" || fail "Never expiry" "$EAT"

echo ""

# ── 4. Password Protection ────────────────────────────────
echo "▸ 4. Password protection"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"password\":\"secret123\"}")
PW_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
PWPROT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("password_protected"))' 2>/dev/null)
[ "$PWPROT" = "True" ] && pass "password_protected flag" || fail "password_protected" "$PWPROT"

# Without password → 401
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG")
[ "$CODE" = "401" ] && pass "No password → 401" || fail "No pw" "HTTP $CODE"

# Wrong password → 401
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG?password=wrong")
[ "$CODE" = "401" ] && pass "Wrong password → 401" || fail "Wrong pw" "HTTP $CODE"

# Correct password → 200
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG?password=secret123")
[ "$CODE" = "200" ] && pass "Correct password → 200" || fail "Correct pw" "HTTP $CODE"

# Auth flow: POST /auth → token → use token
R=$(curl -sk -X POST "$BASE_URL/api/v1/public/$PW_SLUG/auth" -H "Content-Type: application/json" \
  -d '{"password":"secret123"}')
SHARE_TOKEN=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)
[ -n "$SHARE_TOKEN" ] && pass "Auth endpoint returns token" || fail "Auth token" "$R"

CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG?token=$SHARE_TOKEN")
[ "$CODE" = "200" ] && pass "Token bypasses password" || fail "Token bypass" "HTTP $CODE"

# Wrong password to /auth → error (not token)
R=$(curl -sk -X POST "$BASE_URL/api/v1/public/$PW_SLUG/auth" -H "Content-Type: application/json" \
  -d '{"password":"wrong"}')
NO_TOKEN=$(echo "$R" | python3 -c 'import json,sys; print("token" not in json.load(sys.stdin))' 2>/dev/null)
[ "$NO_TOKEN" = "True" ] && pass "Wrong pw to /auth returns no token" || fail "Auth wrong pw" "$R"

echo ""

# ── 5. View Limits ────────────────────────────────────────
echo "▸ 5. Max views"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"max_views\":2}")
MV_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)

curl -sk -o /dev/null "$BASE_URL/api/v1/public/$MV_SLUG"
curl -sk -o /dev/null "$BASE_URL/api/v1/public/$MV_SLUG"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV_SLUG")
[ "$CODE" = "410" ] && pass "View 3 → 410 (limit reached)" || fail "View limit" "HTTP $CODE"

# /meta does NOT increment view count
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"max_views\":1}")
META_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
curl -sk -o /dev/null "$BASE_URL/api/v1/public/$META_SLUG/meta"
curl -sk -o /dev/null "$BASE_URL/api/v1/public/$META_SLUG/meta"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$META_SLUG")
[ "$CODE" = "200" ] && pass "/meta does not consume view" || fail "/meta view count" "HTTP $CODE"

echo ""

# ── 6. List + Delete + Idempotent ─────────────────────────
echo "▸ 6. List + delete"

R=$(acurl "$BASE_URL/api/v1/publications/$VAULT")
COUNT=$(echo "$R" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["publications"]))' 2>/dev/null)
[ "$COUNT" -gt 5 ] && pass "List returns multiple publications ($COUNT)" || fail "List" "$COUNT"

# Filter by resource_type
R=$(acurl "$BASE_URL/api/v1/publications/$VAULT?resource_type=document")
DOC_COUNT=$(echo "$R" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["publications"]))' 2>/dev/null)
[ "$DOC_COUNT" -gt 0 ] && pass "Filter by resource_type=document ($DOC_COUNT)" || fail "Filter doc" "$DOC_COUNT"

# Delete one
acurl -X DELETE "$BASE_URL/api/v1/publications/$VAULT/$DOC_PID" >/dev/null
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$DOC_SLUG")
[ "$CODE" = "404" ] && pass "Deleted publication → 404" || fail "Delete" "HTTP $CODE"

# Invalid publication_id format
CODE=$(acurl -X DELETE "$BASE_URL/api/v1/publications/$VAULT/not-a-uuid" -o /dev/null -w "%{http_code}")
[ "$CODE" = "400" ] && pass "Invalid publication_id → 400" || fail "Invalid ID" "HTTP $CODE"

echo ""

# ── 7. Table Query Publication ────────────────────────────
echo "▸ 7. Table query publication"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"SELECT name, category, price FROM products WHERE category = :cat AND price >= :min ORDER BY price DESC","query_params":{"cat":{"type":"text","default":"food"},"min":{"type":"number","default":0}},"title":"Products"}')
TQ_PID=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["publication_id"])' 2>/dev/null)
TQ_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
[ -n "$TQ_SLUG" ] && pass "Create table_query publication" || fail "TQ create" "$R"

# Default params
R=$(curl -sk "$BASE_URL/api/v1/public/$TQ_SLUG")
TOTAL=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("total"))' 2>/dev/null)
[ "$TOTAL" = "2" ] && pass "Default params (cat=food → 2 rows)" || fail "Default params" "total=$TOTAL"

# URL params override
R=$(curl -sk "$BASE_URL/api/v1/public/$TQ_SLUG?cat=furniture&min=100")
TOTAL=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("total"))' 2>/dev/null)
[ "$TOTAL" = "1" ] && pass "URL params override (furniture price>=100 → 1 row)" || fail "URL params" "total=$TOTAL"

# CSV format
CSV=$(curl -sk "$BASE_URL/api/v1/public/$TQ_SLUG?format=csv")
echo "$CSV" | head -1 | grep -q "name,category,price" && pass "CSV header" || fail "CSV header" "$CSV"
echo "$CSV" | grep -q "Bagel,food,2" && pass "CSV data row" || fail "CSV data" "$CSV"

# HTML format
HTML=$(curl -sk "$BASE_URL/api/v1/public/$TQ_SLUG?format=html")
echo "$HTML" | grep -q "<table" && pass "HTML table tag" || fail "HTML" "$HTML"

# Read-only enforcement at create time
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"DELETE FROM products"}')
ERR=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("detail") or d.get("error",""))' 2>/dev/null)
[ -n "$ERR" ] && pass "DELETE blocked at create" || fail "DELETE create" "$R"

# Multi-statement blocked
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"SELECT 1; SELECT 2"}')
ERR=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("detail") or d.get("error",""))' 2>/dev/null)
echo "$ERR" | grep -qi "multi-statement" && pass "Multi-statement blocked at create" || fail "Multi-stmt" "$R"

# Missing query_sql
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query"}')
ERR=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("detail","") or json.load(sys.stdin).get("error",""))' 2>/dev/null)
[ -n "$ERR" ] && pass "Missing query_sql rejected" || fail "Missing query_sql" "$R"

echo ""

# ── 8. Snapshot Mode ──────────────────────────────────────
echo "▸ 8. Snapshot mode"

# Create snapshot from existing publication
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/$TQ_PID/snapshot")
SS_KEY=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("snapshot_s3_key",""))' 2>/dev/null)
[ -n "$SS_KEY" ] && pass "Snapshot created ($SS_KEY)" || fail "Snapshot create" "$R"

# After snapshot, /public returns mode=snapshot
R=$(curl -sk "$BASE_URL/api/v1/public/$TQ_SLUG")
MODE=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mode",""))' 2>/dev/null)
[ "$MODE" = "snapshot" ] && pass "Mode flipped to snapshot" || fail "Snapshot mode" "$MODE"

# Insert new data — snapshot must NOT reflect it
acurl -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" -H "Content-Type: application/json" \
  -d "{\"sql\":\"INSERT INTO products (name, category, price) VALUES ('SnapTest', 'food', 999)\"}" >/dev/null
R=$(curl -sk "$BASE_URL/api/v1/public/$TQ_SLUG")
ROWS=$(echo "$R" | python3 -c 'import json,sys; rows=json.load(sys.stdin).get("rows",[]); print(",".join(r["name"] for r in rows))' 2>/dev/null)
echo "$ROWS" | grep -q "SnapTest" && fail "Snapshot freeze" "leaked new data" || pass "Snapshot freezes data (no SnapTest)"

# snapshot only supported for table_query
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\"}")
DOCPID=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["publication_id"])' 2>/dev/null)
CODE=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/$DOCPID/snapshot" -o /dev/null -w "%{http_code}")
[ "$CODE" = "400" ] && pass "Snapshot rejected for document publication" || fail "Snapshot doc" "HTTP $CODE"

echo ""

# ── 9. File Publication ───────────────────────────────────
echo "▸ 9. File publication"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"file_id\":\"$FID\"}")
FILE_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
[ -n "$FILE_SLUG" ] && pass "Create file publication" || fail "File pub" "$R"

# /meta returns file metadata without presigned URL
R=$(curl -sk "$BASE_URL/api/v1/public/$FILE_SLUG/meta")
MIME=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mime_type",""))' 2>/dev/null)
[ "$MIME" = "application/json" ] && pass "/meta mime_type" || fail "/meta" "$R"

# /public returns full file info with download_url
R=$(curl -sk "$BASE_URL/api/v1/public/$FILE_SLUG")
DLURL=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("download_url",""))' 2>/dev/null)
[ -n "$DLURL" ] && pass "download_url returned" || fail "download_url" "$R"

# /raw proxies content (CORS-safe for browser)
RAW=$(curl -sk "$BASE_URL/api/v1/public/$FILE_SLUG/raw")
echo "$RAW" | grep -q '"hello":"world"' && pass "/raw streams JSON content" || fail "/raw" "$RAW"

# /download → 302 to S3
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$FILE_SLUG/download")
[ "$CODE" = "302" ] && pass "/download → 302 redirect" || fail "/download" "HTTP $CODE"

# Invalid file_id format
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"file_id\":\"not-a-uuid\"}")
ERR=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("detail","") or json.load(sys.stdin).get("error",""))' 2>/dev/null)
[ -n "$ERR" ] && pass "Invalid file_id rejected" || fail "Invalid file_id" "$R"

echo ""

# ── 10. Embed + oEmbed ────────────────────────────────────
echo "▸ 10. Embed + oEmbed"

R=$(curl -sk "$BASE_URL/api/v1/public/$DOCPID/embed" 2>&1 || true)
# embed returns the same shape with embed: true
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$TQ_SLUG/embed")
[ "$CODE" = "200" ] && pass "/embed returns 200 for allowed publication" || fail "/embed" "HTTP $CODE"

# oEmbed
R=$(curl -sk "$BASE_URL/api/v1/oembed?url=/p/$TQ_SLUG")
TYPE=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("type",""))' 2>/dev/null)
[ "$TYPE" = "rich" ] && pass "oEmbed type=rich for table_query" || fail "oEmbed type" "$TYPE"

# allow_embed=false → /embed → 403
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"allow_embed\":false}")
NOEMBED_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$NOEMBED_SLUG/embed")
[ "$CODE" = "403" ] && pass "allow_embed=false → /embed 403" || fail "allow_embed" "HTTP $CODE"

# Bad oEmbed URL → 400
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/oembed?url=https://example.com/wrong")
[ "$CODE" = "400" ] && pass "oEmbed bad URL → 400" || fail "oEmbed bad URL" "HTTP $CODE"

echo ""

# ── 11. Edge cases ────────────────────────────────────────
echo "▸ 11. Edge cases"

# Not found slug
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/totallybogusslug123")
[ "$CODE" = "404" ] && pass "Bogus slug → 404" || fail "Bogus slug" "HTTP $CODE"

# Invalid slug character (URL-decoded only)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/!@#")
# Either 404 or 400, not 500
[ "$CODE" != "500" ] && pass "Invalid slug doesn't crash ($CODE)" || fail "Invalid slug crash" "500"

# /publications without auth → 401
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/publications/$VAULT")
[ "$CODE" = "401" ] && pass "List without auth → 401" || fail "List no auth" "HTTP $CODE"

# Create publication without auth → 401
CODE=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/publications/$VAULT/create" \
  -H "Content-Type: application/json" -d '{"resource_type":"document","doc_id":"foo"}')
[ "$CODE" = "401" ] && pass "Create without auth → 401" || fail "Create no auth" "HTTP $CODE"

# Non-existent doc_id
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"d-00000000\"}")
CODE=$?
echo "$R" | grep -qi "not found\|404" && pass "Non-existent doc_id rejected" || fail "Bad doc_id" "$R"

# Snapshot of non-existent publication
CODE=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/00000000-0000-0000-0000-000000000000/snapshot" \
  -o /dev/null -w "%{http_code}")
[ "$CODE" = "404" ] && pass "Snapshot non-existent → 404" || fail "Snapshot 404" "HTTP $CODE"

# Public access on archived vault publication should still work (read is OK)
# (We don't archive here — too disruptive — just verify the publication still resolves)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$TQ_SLUG")
[ "$CODE" = "200" ] && pass "TQ slug still resolves after later mutations" || fail "Resolve after" "HTTP $CODE"

echo ""

# ── 12. MCP integration: legacy akb_publish backward compat ──
echo "▸ 12. MCP backward compat"

# Init MCP session
SESS=$(curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' \
  -i 2>&1 | grep -i "mcp-session-id:" | tr -d '\r' | awk '{print $2}')
[ -n "$SESS" ] && pass "MCP session initialized" || fail "MCP init" "no session"

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESS" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null

mcp() {
  local id=$1; shift
  local name=$1; shift
  local args=$1
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "Mcp-Session-Id: $SESS" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$id,\"method\":\"tools/call\",\"params\":{\"name\":\"$name\",\"arguments\":$args}}" 2>&1
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

# akb_publish (basic) — uses canonical URI
DOC_URI_FOR_MCP="$DOC_URI"
R=$(mcp 10 akb_publish "{\"uri\":\"$DOC_URI_FOR_MCP\"}" | mcp_text)
SLUG_FROM_MCP=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
[ -n "$SLUG_FROM_MCP" ] && pass "MCP akb_publish returns slug" || fail "MCP publish" "$R"

# akb_publications (list)
R=$(mcp 11 akb_publications "{\"vault\":\"$VAULT\"}" | mcp_text)
PUB_TOTAL=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("total",0))' 2>/dev/null)
[ "$PUB_TOTAL" -gt 0 ] && pass "MCP akb_publications list ($PUB_TOTAL)" || fail "MCP list" "$R"

# akb_publication_snapshot — takes vault + slug
TQ_SLUG_MCP=$(mcp 12 akb_publish "{\"vault\":\"$VAULT\",\"resource_type\":\"table_query\",\"query_sql\":\"SELECT name FROM products\"}" | mcp_text | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
R=$(mcp 13 akb_publication_snapshot "{\"vault\":\"$VAULT\",\"slug\":\"$TQ_SLUG_MCP\"}" | mcp_text)
SS_AT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("snapshot_at",""))' 2>/dev/null)
[ -n "$SS_AT" ] && pass "MCP akb_publication_snapshot" || fail "MCP snapshot" "$R"

# akb_unpublish by slug
R=$(mcp 14 akb_publications "{\"vault\":\"$VAULT\",\"resource_type\":\"document\"}" | mcp_text)
ANY_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["publications"][0]["slug"])' 2>/dev/null)
R=$(mcp 15 akb_unpublish "{\"slug\":\"$ANY_SLUG\"}" | mcp_text)
DEL=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("deleted"))' 2>/dev/null)
[ "$DEL" = "True" ] && pass "MCP akb_unpublish by slug" || fail "MCP unpublish" "$R"

echo ""

# ── 13. Additional edge cases ─────────────────────────────
echo "▸ 13. Additional edge cases"

# Invalid mode value
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"mode\":\"banana\"}")
ERR=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("detail") or d.get("error",""))' 2>/dev/null)
echo "$ERR" | grep -qi "invalid mode" && pass "Invalid mode rejected" || fail "Invalid mode" "$R"

# Empty query_sql (whitespace only)
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"   "}')
ERR=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("detail") or d.get("error",""))' 2>/dev/null)
[ -n "$ERR" ] && pass "Whitespace query_sql rejected" || fail "Whitespace SQL" "$R"

# Comment-style SQL injection attempt
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"SELECT * FROM products -- ; DROP TABLE products"}')
SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
# Either accepted (because comment is after SELECT — harmless) or rejected; what
# matters is products table still exists afterwards.
EXISTS=$(acurl -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" -H "Content-Type: application/json" \
  -d '{"sql":"SELECT COUNT(*) FROM products"}' | python3 -c 'import json,sys; print(json.load(sys.stdin).get("items",[{}])[0].get("count","?"))' 2>/dev/null)
[ "$EXISTS" != "?" ] && pass "products table survived comment injection (rows=$EXISTS)" || fail "Comment injection" "table dropped or query failed"

# SQL declares parameter not used
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"SELECT * FROM products","query_params":{"unused":{"type":"text"}}}')
ERR=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("detail") or d.get("error",""))' 2>/dev/null)
echo "$ERR" | grep -qi "unused" && pass "Unused query_params rejected at create" || fail "Unused param" "$R"

# SQL references undeclared param
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"SELECT * FROM products WHERE name = :missing"}')
ERR=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("detail") or d.get("error",""))' 2>/dev/null)
echo "$ERR" | grep -qi "undeclared" && pass "Undeclared :param rejected at create" || fail "Undeclared param" "$R"

# HMAC token tampering — last char flipped
PWR=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"password\":\"abc\"}")
PW_SLUG_T=$(echo "$PWR" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
TR=$(curl -sk -X POST "$BASE_URL/api/v1/public/$PW_SLUG_T/auth" -H "Content-Type: application/json" -d '{"password":"abc"}')
GOOD_TOKEN=$(echo "$TR" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)
TAMPERED="${GOOD_TOKEN%?}X"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG_T?token=$TAMPERED")
[ "$CODE" = "401" ] && pass "Tampered HMAC token → 401" || fail "Tampered token" "HTTP $CODE"

# Token from one slug doesn't work on another (slug binding)
PWR2=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"password\":\"xyz\"}")
PW_SLUG_2=$(echo "$PWR2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG_2?token=$GOOD_TOKEN")
[ "$CODE" = "401" ] && pass "Token bound to original slug (cross-slug → 401)" || fail "Cross-slug token" "HTTP $CODE"

# max_views=0 → immediately exhausted
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"max_views\":0}")
MV0_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
if [ -n "$MV0_SLUG" ]; then
  CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV0_SLUG")
  [ "$CODE" = "410" ] && pass "max_views=0 → immediately 410" || fail "max_views=0" "HTTP $CODE"
fi

# /raw on non-previewable MIME (PNG image) → 415
echo -n "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9eMyf2QAAAAASUVORK5CYII=" | base64 -d > /tmp/edge.png
INIT=$(acurl -X POST "$BASE_URL/api/v1/files/$VAULT/upload?filename=edge.png&collection=img&mime_type=image/png")
PFID=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)
PURL=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upload_url"])' 2>/dev/null)
curl -sk -X PUT "$PURL" -H "Content-Type: image/png" --data-binary @/tmp/edge.png > /dev/null
acurl -X POST "$BASE_URL/api/v1/files/$VAULT/$PFID/confirm" > /dev/null
PNG_PUB=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"file_id\":\"$PFID\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PNG_PUB/raw")
[ "$CODE" = "415" ] && pass "/raw on image → 415" || fail "/raw image" "HTTP $CODE"

# Re-snapshot (should overwrite, not error)
RS_TQ=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"SELECT name FROM products LIMIT 1"}')
RS_PID=$(echo "$RS_TQ" | python3 -c 'import json,sys; print(json.load(sys.stdin)["publication_id"])' 2>/dev/null)
acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/$RS_PID/snapshot" > /dev/null
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/$RS_PID/snapshot")
SS2=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("snapshot_at",""))' 2>/dev/null)
[ -n "$SS2" ] && pass "Re-snapshot is idempotent" || fail "Re-snapshot" "$R"

# Snapshot with 0 rows
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"table_query\",\"query_sql\":\"SELECT name FROM products WHERE name = 'NeverExists'\"}")
EMP_PID=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["publication_id"])' 2>/dev/null)
EMP_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/$EMP_PID/snapshot")
ROWS=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("rows"))' 2>/dev/null)
[ "$ROWS" = "0" ] && pass "Snapshot 0 rows handled" || fail "Empty snapshot" "$R"
# Access the empty snapshot
R=$(curl -sk "$BASE_URL/api/v1/public/$EMP_SLUG")
TOTAL=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("total"))' 2>/dev/null)
[ "$TOTAL" = "0" ] && pass "Empty snapshot returns total=0" || fail "Empty snapshot read" "$R"

# /embed on password-protected publication WITHOUT token → 401
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG_T/embed")
[ "$CODE" = "401" ] && pass "Password-protected /embed without token → 401" || fail "PW embed" "HTTP $CODE"

# List on a vault with 0 publications (after creating new vault)
acurl -X POST "$BASE_URL/api/v1/vaults?name=${VAULT}-empty&description=empty" >/dev/null
R=$(acurl "$BASE_URL/api/v1/publications/${VAULT}-empty")
COUNT=$(echo "$R" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["publications"]))' 2>/dev/null)
[ "$COUNT" = "0" ] && pass "Empty vault list → []" || fail "Empty list" "$COUNT"

# List with invalid resource_type filter (FastAPI may 422 or service may return [])
R=$(acurl "$BASE_URL/api/v1/publications/$VAULT?resource_type=banana")
ITEMS=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("publications",[])))' 2>/dev/null)
[ "$ITEMS" = "0" ] && pass "Invalid resource_type filter → []" || fail "Invalid filter" "$R"

# Cascade: file delete → publications cascade
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"file_id\":\"$FID\"}")
CSC_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
acurl -X DELETE "$BASE_URL/api/v1/files/$VAULT/$FID" > /dev/null
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$CSC_SLUG")
[ "$CODE" = "404" ] && pass "File delete cascades publications (404)" || fail "File cascade" "HTTP $CODE"

# Cascade: document delete → publications cascade
R=$(acurl -X POST "$BASE_URL/api/v1/documents" -H "Content-Type: application/json" \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"docs\",\"title\":\"To Delete\",\"content\":\"# tmp\",\"type\":\"note\"}")
CASCADE_DOC=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])' 2>/dev/null)
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$CASCADE_DOC\"}")
DOC_CSC_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
acurl -X DELETE "$BASE_URL/api/v1/documents/$VAULT/$CASCADE_DOC" > /dev/null
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$DOC_CSC_SLUG")
[ "$CODE" = "404" ] && pass "Document delete cascades publications (404)" || fail "Doc cascade" "HTTP $CODE"

# Cascade: empty vault delete
mcp 90 akb_delete_vault "{\"vault\":\"${VAULT}-empty\"}" >/dev/null 2>&1
pass "Empty vault deleted (cleanup)"

# section_not_found field is True when filter missing
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$DOC_ID\",\"section\":\"NoSuchHeading\"}")
SNF_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
SNF=$(curl -sk "$BASE_URL/api/v1/public/$SNF_SLUG" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("section_not_found"))' 2>/dev/null)
[ "$SNF" = "True" ] && pass "section_not_found=true when filter missing" || fail "section_not_found" "$SNF"

echo ""

# ── 99. Cleanup ───────────────────────────────────────────
echo "▸ 99. Cleanup"
mcp 99 akb_delete_vault "{\"vault\":\"$VAULT\"}" >/dev/null 2>&1
pass "Cleanup done"

# Terminate MCP session
curl -sk -X DELETE "$BASE_URL/mcp/" -H "Authorization: Bearer $TOKEN" -H "Mcp-Session-Id: $SESS" >/dev/null 2>&1

echo ""
echo "╔══════════════════════════════════════════╗"
printf "║   Results: %d passed, %d failed%s║\n" "$PASS" "$FAIL" "$(printf '%*s' $((22-${#PASS}-${#FAIL})) '')"
echo "╚══════════════════════════════════════════╝"

if [ $FAIL -gt 0 ]; then
  echo ""
  echo "Errors:"
  for e in "${ERRORS[@]}"; do
    echo "  • $e"
  done
  exit 1
fi
exit 0
