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

# Seed rows. Every later table_query assertion counts on exactly these 4 rows,
# so a silent seed failure must fail here rather than skew a row count downstream.
SEED=$(acurl -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" -H "Content-Type: application/json" \
  -d "{\"sql\":\"INSERT INTO products (name, category, price) VALUES ('Apple', 'food', 1), ('Bagel', 'food', 2), ('Chair', 'furniture', 50), ('Desk', 'furniture', 200)\"}")
SEEDED=$(echo "$SEED" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("affected_rows"))' 2>/dev/null)
[ "$SEEDED" = "4" ] && pass "Table seeded (4 rows)" || fail "Table seed" "affected_rows=$SEEDED: $SEED"

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
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\"}")
DOC_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
[ -n "$DOC_SLUG" ] && pass "Create document publication" || fail "Create doc pub" "$R"

# Response shape: canonical public dict only — no internal/legacy fields.
# The "PARSED:" marker is what makes this an absence assertion rather than a
# silence assertion: an unparsable body prints nothing and an error body has no
# `slug`, so neither can read as "no internal fields present".
LEAKS=$(echo "$R" | python3 -c '
import json, sys
d = json.load(sys.stdin)
forbidden = ["id", "publication_id", "public_url", "public_url_full",
             "public_base", "snapshot_s3_key", "password_hash", "vault_id"]
found = [k for k in forbidden if k in d]
if "slug" not in d:
    found.append("<not-a-publication-response>")
print("PARSED:" + ",".join(found))' 2>/dev/null)
[ "$LEAKS" = "PARSED:" ] && pass "Response excludes legacy/internal fields" || fail "Response shape" "${LEAKS:-unparsable body}: $R"

# share_url is always absolute (AKB_PUBLIC_BASE_URL is startup-required).
SU=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("share_url",""))' 2>/dev/null)
case "$SU" in
  http://*|https://*) pass "share_url is absolute ($SU)" ;;
  *) fail "share_url" "not absolute: $SU" ;;
esac

# vault name is on the publication
PV=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("vault",""))' 2>/dev/null)
[ "$PV" = "$VAULT" ] && pass "vault name surfaced on response" || fail "vault field" "$PV"

# Resolve via /public/{slug} (no auth)
R=$(curl -sk "$BASE_URL/api/v1/public/$DOC_SLUG")
TITLE=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("title",""))' 2>/dev/null)
[ "$TITLE" = "Pub Doc" ] && pass "Resolve doc publication (no auth)" || fail "Resolve doc" "title=$TITLE"

# content_unavailable = false
CU=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("content_unavailable"))' 2>/dev/null)
[ "$CU" = "False" ] && pass "content_unavailable = false" || fail "content_unavailable" "$CU"

# F8: anonymous doc response must not leak the raw creator id, internal workflow
# status, or created_at — only the resolved author display name is exposed.
echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); leaked=[k for k in ("created_by","status","created_at") if k in d]; sys.exit(1 if leaked else 0)' \
  && pass "F8: doc response omits created_by/status/created_at" || fail "F8 metadata leak" "$R"

# F6: owner-capability probe — anonymous can't edit; the authenticated owner can.
CE=$(curl -sk "$BASE_URL/api/v1/public/$DOC_SLUG/capabilities" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("can_edit"))' 2>/dev/null)
[ "$CE" = "False" ] && pass "F6: anonymous capabilities can_edit=false" || fail "F6 anon caps" "$CE"
CE=$(acurl "$BASE_URL/api/v1/public/$DOC_SLUG/capabilities" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("can_edit"))' 2>/dev/null)
[ "$CE" = "True" ] && pass "F6: owner capabilities can_edit=true" || fail "F6 owner caps" "$CE"

# Tags returned
TAGS=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(",".join(d.get("tags",[])))' 2>/dev/null)
[ "$TAGS" = "pub,test" ] && pass "Tags preserved" || fail "Tags" "$TAGS"

echo ""

# ── 2. Section Filter ─────────────────────────────────────
echo "▸ 2. Section filter"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"section_filter\":\"Alpha\"}")
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
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"section_filter\":\"Nonexistent\"}")
FAKE_SEC_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
R=$(curl -sk "$BASE_URL/api/v1/public/$FAKE_SEC_SLUG")
CONTENT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("content",""))' 2>/dev/null)
echo "$CONTENT" | grep -q "## Beta" && pass "Non-existent section falls back to full doc" || fail "Section fallback" "missing Beta"

echo ""

# ── 3. Expiration ─────────────────────────────────────────
echo "▸ 3. Expiration"

# Create with 1h expiry
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"expires_in\":\"1h\"}")
EXP_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
EXP_AT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("expires_at",""))' 2>/dev/null)
[ -n "$EXP_AT" ] && pass "expires_at populated" || fail "expires_at" "missing"

# Should be accessible now
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$EXP_SLUG")
[ "$CODE" = "200" ] && pass "Not yet expired (200)" || fail "Pre-expiry" "HTTP $CODE"

# Invalid expires_in format
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"expires_in\":\"banana\"}")
ERR=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("detail") or d.get("error",""))' 2>/dev/null)
echo "$ERR" | grep -qi "invalid expires_in" && pass "Invalid expires_in rejected" || fail "Invalid expires_in" "$R"

# 'never' produces no expiration
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"expires_in\":\"never\"}")
EAT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("expires_at"))' 2>/dev/null)
[ "$EAT" = "None" ] && pass "expires_in='never' → no expiry" || fail "Never expiry" "$EAT"

echo ""

# ── 4. Password Protection ────────────────────────────────
echo "▸ 4. Password protection"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"password\":\"secret123\"}")
PW_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
PWPROT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("password_protected"))' 2>/dev/null)
[ "$PWPROT" = "True" ] && pass "password_protected flag" || fail "password_protected" "$PWPROT"

# Without password → 401
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG")
[ "$CODE" = "401" ] && pass "No password → 401" || fail "No pw" "HTTP $CODE"

# Wrong password → 401 (password via header — query-string ?password= was
# removed as a leak vector in publish-hardening M1)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" -H "x-publication-password: wrong" "$BASE_URL/api/v1/public/$PW_SLUG")
[ "$CODE" = "401" ] && pass "Wrong password → 401" || fail "Wrong pw" "HTTP $CODE"

# Correct password → 200 (via header)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" -H "x-publication-password: secret123" "$BASE_URL/api/v1/public/$PW_SLUG")
[ "$CODE" = "200" ] && pass "Correct password → 200" || fail "Correct pw" "HTTP $CODE"

# Query-string password must NOT authenticate (M1 hardening) → still 401
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG?password=secret123")
[ "$CODE" = "401" ] && pass "Query-string password rejected → 401" || fail "QS pw should be ignored" "HTTP $CODE"

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

# Throttle backstop + owner bypass (F2 + G5): an anonymous wrong-password flood
# from ROTATING IPs trips the per-slug backstop and 429s further anonymous
# attempts (even the correct password), but the authenticated OWNER bypasses the
# throttle so a flood can never lock them out of their own publication.
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"password\":\"realpass\"}")
TH_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
th_auth() { curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/public/$TH_SLUG/auth" \
  -H "Content-Type: application/json" -H "X-Forwarded-For: $2" -d "{\"password\":\"$1\"}"; }
LOCKED=0
for i in $(seq 1 35); do [ "$(th_auth wrongpw "66.66.66.$i")" = "429" ] && LOCKED=1; done
[ "$LOCKED" = "1" ] && pass "Anon wrong-pw flood trips backstop (429)" || fail "Backstop 429" "not locked"
CODE=$(th_auth realpass "1.2.3.4")
[ "$CODE" = "429" ] && pass "Anon correct pw is ALSO throttled during flood (no probe)" || fail "Anon throttled" "HTTP $CODE"
CODE=$(acurl -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/public/$TH_SLUG/auth" \
  -H "Content-Type: application/json" -H "X-Forwarded-For: 1.2.3.4" -d '{"password":"realpass"}')
[ "$CODE" = "200" ] && pass "Owner session bypasses the throttle (200, not locked out)" || fail "Owner bypass" "HTTP $CODE"

echo ""

# ── 5. View Limits ────────────────────────────────────────
echo "▸ 5. Max views"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"max_views\":2}")
MV_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)

curl -sk -o /dev/null "$BASE_URL/api/v1/public/$MV_SLUG"
curl -sk -o /dev/null "$BASE_URL/api/v1/public/$MV_SLUG"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV_SLUG")
[ "$CODE" = "410" ] && pass "View 3 → 410 (limit reached)" || fail "View limit" "HTTP $CODE"

# max_views is a HARD cap on EVERY path via the view-grant. A page open mints a
# grant; /raw & /download re-serve THAT view free WITH the grant, but a direct
# fetch WITHOUT a grant spends its own view and 410s past the cap (bypass closed).
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV_SLUG/download")
[ "$CODE" = "410" ] && pass "doc /download WITHOUT grant after cap → 410 (hard cap, bypass closed)" || fail "doc download hard cap" "HTTP $CODE"

# a grant minted by a live page open re-serves even at the last allowed view
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"max_views\":1}")
G1_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
GRANT=$(curl -sk "$BASE_URL/api/v1/public/$G1_SLUG" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("view_grant",""))' 2>/dev/null)
[ -n "$GRANT" ] && pass "doc page open returns a view_grant" || fail "view_grant present" "empty"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$G1_SLUG/download?grant=$GRANT")
[ "$CODE" = "200" ] && pass "doc /download WITH grant re-serves the counted view (200)" || fail "doc download grant re-serve" "HTTP $CODE"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$G1_SLUG/download")
[ "$CODE" = "410" ] && pass "doc /download WITHOUT grant on exhausted pub → 410" || fail "doc download no grant" "HTTP $CODE"

# No rolling renewal: re-opening WITH the grant re-serves (no re-count) but must
# ECHO THE SAME grant string — re-minting a fresh timestamp on every GET would let
# a viewer refresh before expiry into an unlimited renewable capability from one
# counted view. (The identical string proves the embedded 600s TTL doesn't roll.)
GRANT2=$(curl -sk "$BASE_URL/api/v1/public/$G1_SLUG?grant=$GRANT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("view_grant",""))' 2>/dev/null)
[ "$GRANT2" = "$GRANT" ] && pass "grant-carried GET echoes the SAME grant (no rolling renewal)" || fail "rolling renewal" "$GRANT -> $GRANT2"
# and renewal did not buy extra views: a grantless open is still capped
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$G1_SLUG")
[ "$CODE" = "410" ] && pass "grantless open still 410 after grant re-serve (cap holds)" || fail "cap after renewal" "HTTP $CODE"

# /meta does NOT increment view count
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"max_views\":1}")
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

# Delete by slug (the only external identifier)
acurl -X DELETE "$BASE_URL/api/v1/publications/$VAULT/$DOC_SLUG" >/dev/null
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$DOC_SLUG")
[ "$CODE" = "404" ] && pass "Deleted publication → 404" || fail "Delete" "HTTP $CODE"

# Unknown slug → 404
CODE=$(acurl -X DELETE "$BASE_URL/api/v1/publications/$VAULT/totally-bogus-slug" -o /dev/null -w "%{http_code}")
[ "$CODE" = "404" ] && pass "Unknown slug delete → 404" || fail "Unknown slug delete" "HTTP $CODE"

echo ""

# ── 7. Table Query Publication ────────────────────────────
echo "▸ 7. Table query publication"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"SELECT name, category, price FROM products WHERE category = :cat AND price >= :min ORDER BY price DESC","query_params":{"cat":{"type":"text","default":"food"},"min":{"type":"number","default":0}},"title":"Products"}')
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

# Control-param reservation: a table declaring a `:grant` bind param must NOT
# receive our HMAC view-grant as data — `grant` is stripped, so the default is
# used (same for the page GET, CSV, and the /download path).
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"SELECT name FROM products WHERE category = :grant ORDER BY name","query_params":{"grant":{"type":"text","default":"food"}},"title":"GrantParam"}')
GP_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
# ?grant=furniture is stripped → the :grant param falls back to its default "food",
# so the rows are the food ones (Apple/Bagel), NOT furniture (Chair/Desk).
GPJ=$(curl -sk "$BASE_URL/api/v1/public/$GP_SLUG?grant=furniture")
echo "$GPJ" | grep -q "Apple" && ! echo "$GPJ" | grep -q "Chair" && pass "table :grant param ignores injected ?grant= (default food rows, page GET)" || fail "grant param inject (GET)" "$GPJ"
CSVG=$(curl -sk "$BASE_URL/api/v1/public/$GP_SLUG/download?grant=furniture")
echo "$CSVG" | grep -qi "furniture\|Chair\|Desk" && fail "grant param inject (/download)" "furniture rows leaked: $CSVG" || pass "table :grant ignores injected ?grant= on /download (food rows only)"

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

# Create snapshot from existing publication (slug-based, no UUID)
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/$TQ_SLUG/snapshot")
SS_MODE=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mode",""))' 2>/dev/null)
SS_AT=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("snapshot_at",""))' 2>/dev/null)
[ "$SS_MODE" = "snapshot" ] && pass "Snapshot response mode=snapshot" || fail "Snapshot mode" "$SS_MODE"
[ -n "$SS_AT" ] && pass "Snapshot response carries snapshot_at" || fail "Snapshot at" "$SS_AT"

# Snapshot response must not leak the internal s3 key. Establish the body is a
# real snapshot response first — a failed snapshot call carries no s3 key either,
# and that must not read as "the key is correctly hidden".
HAS_S3=$(echo "$R" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if d.get("mode") != "snapshot":
    print("not-a-snapshot-response")
else:
    print("yes" if "snapshot_s3_key" in d else "no")' 2>/dev/null)
[ "$HAS_S3" = "no" ] && pass "Snapshot response hides snapshot_s3_key" || fail "Snapshot leak" "${HAS_S3:-unparsable body}: $R"

# After snapshot, /public returns mode=snapshot
R=$(curl -sk "$BASE_URL/api/v1/public/$TQ_SLUG")
MODE=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mode",""))' 2>/dev/null)
[ "$MODE" = "snapshot" ] && pass "Mode flipped to snapshot" || fail "Snapshot mode" "$MODE"

# Insert new data — snapshot must NOT reflect it
acurl -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" -H "Content-Type: application/json" \
  -d "{\"sql\":\"INSERT INTO products (name, category, price) VALUES ('SnapTest', 'food', 999)\"}" >/dev/null
R=$(curl -sk "$BASE_URL/api/v1/public/$TQ_SLUG")
# The frozen result set must still BE a result set. An error body or an empty
# `rows` also contains no "SnapTest", so require the served snapshot rows first.
ROWS=$(echo "$R" | python3 -c '
import json, sys
d = json.load(sys.stdin)
rows = d.get("rows")
if d.get("mode") != "snapshot" or not rows:
    print("BAD")
else:
    print("ROWS:" + ",".join(r["name"] for r in rows))' 2>/dev/null)
case "$ROWS" in
  ROWS:*SnapTest*) fail "Snapshot freeze" "serves post-snapshot INSERT: $ROWS" ;;
  ROWS:*)          pass "Snapshot freezes data (no SnapTest)" ;;
  *)               fail "Snapshot freeze" "no usable snapshot rows to check: $R" ;;
esac

# snapshot only supported for table_query
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\"}")
DOC_SLUG_FOR_SNAP=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
CODE=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/$DOC_SLUG_FOR_SNAP/snapshot" -o /dev/null -w "%{http_code}")
[ "$CODE" = "400" ] && pass "Snapshot rejected for document publication" || fail "Snapshot doc" "HTTP $CODE"

echo ""

# ── 9. File Publication ───────────────────────────────────
echo "▸ 9. File publication"

R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"uri\":\"$FILE_URI\"}")
FILE_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
[ -n "$FILE_SLUG" ] && pass "Create file publication" || fail "File pub" "$R"

# /meta returns file metadata without presigned URL
R=$(curl -sk "$BASE_URL/api/v1/public/$FILE_SLUG/meta")
MIME=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mime_type",""))' 2>/dev/null)
[ "$MIME" = "application/json" ] && pass "/meta mime_type" || fail "/meta" "$R"

# F4: /public no longer hands out a presigned download_url — inline render is
# proxied same-origin via /raw, so the vault name embedded in the S3 key can't
# leak and the view stays counted + revocable.
R=$(curl -sk "$BASE_URL/api/v1/public/$FILE_SLUG")
# "OK:" only prints for a body that really is a resolved file publication, so a
# 404/410 or an unparsable body cannot read as "download_url correctly absent".
DL=$(echo "$R" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if d.get("resource_type") != "file" or not d.get("mime_type"):
    print("BAD:not-a-file-publication-response")
else:
    print("OK:" + str(d.get("download_url", "")))' 2>/dev/null)
[ "$DL" = "OK:" ] && pass "F4: no presigned download_url in file response" || fail "F4 download_url absent" "${DL:-unparsable body}: $R"

# /raw proxies content (CORS-safe for browser)
RAW=$(curl -sk "$BASE_URL/api/v1/public/$FILE_SLUG/raw")
echo "$RAW" | grep -q '"hello":"world"' && pass "/raw streams JSON content" || fail "/raw" "$RAW"
# F4: proxied inline content is served with inline disposition + nosniff
RH=$(curl -sk -D - -o /dev/null "$BASE_URL/api/v1/public/$FILE_SLUG/raw")
echo "$RH" | grep -qi '^x-content-type-options: nosniff' && pass "F4: /raw sets nosniff" || fail "F4 nosniff" "$RH"

# /download streams the bytes through the backend (mixed-content safe);
# 0.5.x flipped from "302 to S3 presigned" to a same-origin stream.
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$FILE_SLUG/download")
[ "$CODE" = "200" ] && pass "/download streams (200)" || fail "/download" "HTTP $CODE"

# Cross-vault file IDOR: publishing ANOTHER vault's file UUID through my own vault
# must be REJECTED at create time (else an anonymous viewer could read that vault's
# file bytes through my publication).
# The setup is asserted step by step: if no file row is ever created in the
# second vault, the publish below is refused for "no such file" and that would
# read as the vault binding working. The name must fit the vault grammar
# (`document_service._VAULT_NAME_RE`: lowercase alnum with single hyphens) —
# an underscore here 422s and leaves the whole section with nothing to publish.
IDOR_V="pubidor-$(date +%s)-$RANDOM"
CODE=$(acurl -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/vaults?name=$IDOR_V&description=idor")
[ "$CODE" = "200" ] && pass "second vault created" || fail "second vault create" "HTTP $CODE"
IINIT=$(acurl -X POST "$BASE_URL/api/v1/files/$IDOR_V/upload?filename=secret.txt&collection=s&mime_type=text/plain")
IURI=$(echo "$IINIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["uri"])' 2>/dev/null)
IFID=$(printf '%s' "$IURI" | uri_file_id)
IURL=$(echo "$IINIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upload_url"])' 2>/dev/null)
[ -n "$IFID" ] && [ -n "$IURL" ] && pass "second-vault upload initialized" || fail "second-vault upload init" "$IINIT"
CODE=$(printf 'bytes-that-live-in-the-second-vault' | curl -sk -o /dev/null -w "%{http_code}" \
  -X PUT "$IURL" -H "Content-Type: text/plain" --data-binary @-)
[ "$CODE" = "200" ] && pass "second-vault file bytes uploaded (200)" || fail "second-vault upload PUT" "HTTP $CODE"
CODE=$(acurl -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/files/$IDOR_V/$IFID/confirm")
[ "$CODE" = "200" ] && pass "second-vault file confirmed (200)" || fail "second-vault confirm" "HTTP $CODE"
# attacker (owner of $VAULT) tries to publish vault-B's file UUID through $VAULT
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"uri\":\"akb://$VAULT/file/$IFID\"}")
IDOR_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
[ -z "$IDOR_SLUG" ] && pass "cross-vault file publish REJECTED at create (IDOR closed)" || fail "cross-vault file IDOR" "created slug=$IDOR_SLUG"
acurl -X DELETE "$BASE_URL/api/v1/vaults/$IDOR_V" >/dev/null 2>&1

# File view-count model (hard cap via view-grant): a page open counts one view
# and mints a grant; /raw + /download re-serve THAT view free WITH the grant, but
# a direct fetch WITHOUT a grant spends its own view and is capped.
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"uri\":\"$FILE_URI\",\"max_views\":2}")
MV_FILE=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
GF=$(curl -sk "$BASE_URL/api/v1/public/$MV_FILE" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("view_grant",""))' 2>/dev/null)  # page open #1 → view 1/2, grant
# /raw + /download WITH the grant re-serve the SAME open without spending more views
C1=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV_FILE/raw?grant=$GF")
C2=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV_FILE/raw?grant=$GF")
C3=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV_FILE/download?grant=$GF")
[ "$C1" = "200" ] && [ "$C2" = "200" ] && [ "$C3" = "200" ] && pass "view model: /raw+/download WITH grant re-serve one open without re-counting" || fail "re-serve" "$C1/$C2/$C3"
# a direct /raw WITHOUT the grant spends view 2/2 → 200, then the cap is reached
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV_FILE/raw")
[ "$CODE" = "200" ] && pass "direct /raw WITHOUT grant spends its own view (200)" || fail "direct raw counts" "HTTP $CODE"
# now exhausted (2/2): a further direct /download WITHOUT grant → 410 (hard cap, bypass closed)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV_FILE/download")
[ "$CODE" = "410" ] && pass "direct /download WITHOUT grant past cap → 410 (bypass closed)" || fail "file download hard cap" "HTTP $CODE"
# and a page open past the cap is also 410
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV_FILE")
[ "$CODE" = "410" ] && pass "page open past cap → 410" || fail "cap page-open" "HTTP $CODE"

# Invalid file_id format
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"uri\":\"akb://$VAULT/file/not-a-uuid\"}")
ERR=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("detail","") or json.load(sys.stdin).get("error",""))' 2>/dev/null)
[ -n "$ERR" ] && pass "Invalid file_id rejected" || fail "Invalid file_id" "$R"

echo ""

# ── 10. Embed + oEmbed ────────────────────────────────────
echo "▸ 10. Embed + oEmbed"

# Document /embed: same payload as the page GET, plus embed: true.
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$DOC_SLUG_FOR_SNAP/embed")
[ "$CODE" = "200" ] && pass "document /embed returns 200" || fail "document /embed" "HTTP $CODE"
R=$(curl -sk "$BASE_URL/api/v1/public/$DOC_SLUG_FOR_SNAP/embed")
DEMB=$(echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("%s|%s" % (d.get("embed"), d.get("title","")))' 2>/dev/null)
[ "$DEMB" = "True|Pub Doc" ] && pass "document /embed carries embed=true + doc title" || fail "document /embed body" "${DEMB:-unparsable body}: $R"

# embed returns the same shape with embed: true
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$TQ_SLUG/embed")
[ "$CODE" = "200" ] && pass "/embed returns 200 for allowed publication" || fail "/embed" "HTTP $CODE"

# oEmbed
R=$(curl -sk "$BASE_URL/api/v1/oembed?url=/p/$TQ_SLUG")
TYPE=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("type",""))' 2>/dev/null)
[ "$TYPE" = "rich" ] && pass "oEmbed type=rich for table_query" || fail "oEmbed type" "$TYPE"

# F1: oEmbed must NOT leak a password-protected publication's title
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"password\":\"oe-secret\"}")
OE_PW_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
OT=$(curl -sk "$BASE_URL/api/v1/oembed?url=/p/$OE_PW_SLUG" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("title",""))' 2>/dev/null)
[ "$OT" = "Protected AKB publication" ] && pass "F1: oEmbed masks password-protected title" || fail "F1 oembed title" "$OT"

# allow_embed=false → /embed → 403
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"allow_embed\":false}")
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
  -H "Content-Type: application/json" -d "{\"resource_type\":\"document\",\"uri\":\"akb://$VAULT/doc/nonexistent-foo.md\"}")
[ "$CODE" = "401" ] && pass "Create without auth → 401" || fail "Create no auth" "HTTP $CODE"

# Non-existent doc_id
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"akb://$VAULT/doc/missing/nonexistent.md\"}")
CODE=$?
echo "$R" | grep -qi "not found\|404" && pass "Non-existent doc_id rejected" || fail "Bad doc_id" "$R"

# Snapshot of non-existent publication
CODE=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/totally-bogus-slug/snapshot" \
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

# akb_publication_snapshot — slug alone (vault inferred from row)
TQ_SLUG_MCP=$(mcp 12 akb_publish "{\"vault\":\"$VAULT\",\"resource_type\":\"table_query\",\"query_sql\":\"SELECT name FROM products\"}" | mcp_text | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
R=$(mcp 13 akb_publication_snapshot "{\"slug\":\"$TQ_SLUG_MCP\"}" | mcp_text)
SS_MODE_MCP=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mode"))' 2>/dev/null)
[ "$SS_MODE_MCP" = "snapshot" ] && pass "MCP akb_publication_snapshot returns publication dict" || fail "MCP snapshot" "$R"

# akb_unpublish by slug — returns {deleted: N}
R=$(mcp 14 akb_publications "{\"vault\":\"$VAULT\",\"resource_type\":\"document\"}" | mcp_text)
ANY_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["publications"][0]["slug"])' 2>/dev/null)
R=$(mcp 15 akb_unpublish "{\"slug\":\"$ANY_SLUG\"}" | mcp_text)
DEL=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("deleted"))' 2>/dev/null)
[ "$DEL" = "1" ] && pass "MCP akb_unpublish by slug → deleted=1" || fail "MCP unpublish" "$R"

# akb_unpublish rejects unknown args (e.g. legacy `mode`)
R=$(mcp 16 akb_publish "{\"uri\":\"$DOC_URI\",\"mode\":\"snapshot\"}" | mcp_text)
echo "$R" | grep -qi "unknown argument" && pass "MCP akb_publish: legacy 'mode' rejected" || fail "MCP legacy mode" "$R"

# akb_publish response has share_url (absolute), no public_url/publication_id
R=$(mcp 17 akb_publish "{\"uri\":\"$DOC_URI\"}" | mcp_text)
MCP_SHARE=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("share_url",""))' 2>/dev/null)
case "$MCP_SHARE" in
  http://*|https://*) pass "MCP akb_publish: share_url absolute" ;;
  *) fail "MCP share_url" "$MCP_SHARE" ;;
esac
LEAKS=$(echo "$R" | python3 -c '
import json, sys
d = json.load(sys.stdin)
forbidden = ["publication_id","public_url","public_url_full","public_base"]
print(",".join(k for k in forbidden if k in d))' 2>/dev/null)
[ -z "$LEAKS" ] && pass "MCP akb_publish: no legacy fields" || fail "MCP legacy leak" "$LEAKS"

# akb_unpublish by FILE uri — the bug case that 0.5.x silently rejected.
FU_RES=$(mcp 18 akb_publish "{\"uri\":\"$FILE_URI\",\"resource_type\":\"file\"}" | mcp_text)
FU_SLUG=$(echo "$FU_RES" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
[ -n "$FU_SLUG" ] && pass "MCP akb_publish(file uri) creates slug" || fail "File pub via MCP" "$FU_RES"
R=$(mcp 19 akb_unpublish "{\"uri\":\"$FILE_URI\"}" | mcp_text)
DEL=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("deleted"))' 2>/dev/null)
[ "$DEL" -ge 1 ] 2>/dev/null && pass "MCP akb_unpublish(file uri) deletes ≥1" || fail "MCP file uri unpublish" "$R"
# And the share now 404s
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$FU_SLUG")
[ "$CODE" = "404" ] && pass "Unpublished file share → 404" || fail "File unpub 404" "HTTP $CODE"

echo ""

# ── 13. Additional edge cases ─────────────────────────────
echo "▸ 13. Additional edge cases"

# `mode` is no longer a publish-time option (snapshot is reached via the
# separate snapshot endpoint). FastAPI rejects extra fields with 422.
CODE=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"mode\":\"snapshot\"}" \
  -o /dev/null -w "%{http_code}")
[ "$CODE" = "422" ] && pass "publish-time mode option removed (422)" || fail "publish mode" "HTTP $CODE"

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
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"password\":\"abc\"}")
PW_SLUG_T=$(echo "$PWR" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
TR=$(curl -sk -X POST "$BASE_URL/api/v1/public/$PW_SLUG_T/auth" -H "Content-Type: application/json" -d '{"password":"abc"}')
GOOD_TOKEN=$(echo "$TR" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)
TAMPERED="${GOOD_TOKEN%?}X"
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG_T?token=$TAMPERED")
[ "$CODE" = "401" ] && pass "Tampered HMAC token → 401" || fail "Tampered token" "HTTP $CODE"

# Token from one slug doesn't work on another (slug binding)
PWR2=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"password\":\"xyz\"}")
PW_SLUG_2=$(echo "$PWR2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PW_SLUG_2?token=$GOOD_TOKEN")
[ "$CODE" = "401" ] && pass "Token bound to original slug (cross-slug → 401)" || fail "Cross-slug token" "HTTP $CODE"

# max_views=0 → immediately exhausted
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"max_views\":0}")
MV0_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
if [ -n "$MV0_SLUG" ]; then
  CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$MV0_SLUG")
  [ "$CODE" = "410" ] && pass "max_views=0 → immediately 410" || fail "max_views=0" "HTTP $CODE"
else
  fail "max_views=0" "publish returned no slug, nothing to request: $R"
fi

# F4: images render inline through the same-origin /raw proxy (200 + image/png),
# not a presigned S3 URL.
echo -n "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9eMyf2QAAAAASUVORK5CYII=" | base64 -d > /tmp/edge.png
INIT=$(acurl -X POST "$BASE_URL/api/v1/files/$VAULT/upload?filename=edge.png&collection=img&mime_type=image/png")
PFILE_URI=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["uri"])' 2>/dev/null)
PFID=$(printf '%s' "$PFILE_URI" | uri_file_id)
PURL=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upload_url"])' 2>/dev/null)
curl -sk -X PUT "$PURL" -H "Content-Type: image/png" --data-binary @/tmp/edge.png > /dev/null
acurl -X POST "$BASE_URL/api/v1/files/$VAULT/$PFID/confirm" > /dev/null
PNG_PUB=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"uri\":\"$PFILE_URI\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
PH=$(curl -sk -D - -o /dev/null "$BASE_URL/api/v1/public/$PNG_PUB/raw")
echo "$PH" | grep -qiE '^HTTP/[0-9.]+ 200' && pass "F4: /raw on image → 200 (inline proxy)" || fail "/raw image 200" "$PH"
echo "$PH" | grep -qi '^content-type: image/png' && pass "F4: /raw image Content-Type image/png" || fail "/raw image mime" "$PH"

# A truly non-previewable MIME (octet-stream) → 415 (use /download instead)
echo -n "random-bytes-not-previewable" > /tmp/edge.bin
INIT=$(acurl -X POST "$BASE_URL/api/v1/files/$VAULT/upload?filename=edge.bin&collection=img&mime_type=application/octet-stream")
BFILE_URI=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["uri"])' 2>/dev/null)
BFID=$(printf '%s' "$BFILE_URI" | uri_file_id)
BURL=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upload_url"])' 2>/dev/null)
curl -sk -X PUT "$BURL" -H "Content-Type: application/octet-stream" --data-binary @/tmp/edge.bin > /dev/null
acurl -X POST "$BASE_URL/api/v1/files/$VAULT/$BFID/confirm" > /dev/null
BIN_PUB=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"uri\":\"$BFILE_URI\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$BIN_PUB/raw")
[ "$CODE" = "415" ] && pass "/raw on octet-stream → 415" || fail "/raw octet 415" "HTTP $CODE"

# F4: active document types (uploaded HTML, declared with a charset param) are
# served with a CSP sandbox so any embedded script is inert — verifies the mime
# is normalized before the CSP decision.
printf '<h1>x</h1><script>alert(1)</script>' > /tmp/edge.html
INIT=$(acurl -X POST "$BASE_URL/api/v1/files/$VAULT/upload?filename=edge.html&collection=img&mime_type=text/html")
HFILE_URI=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["uri"])' 2>/dev/null)
HFID=$(printf '%s' "$HFILE_URI" | uri_file_id)
HURL=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upload_url"])' 2>/dev/null)
curl -sk -X PUT "$HURL" -H "Content-Type: text/html" --data-binary @/tmp/edge.html > /dev/null
acurl -X POST "$BASE_URL/api/v1/files/$VAULT/$HFID/confirm" > /dev/null
HTML_PUB=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"uri\":\"$HFILE_URI\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
HH=$(curl -sk -D - -o /dev/null "$BASE_URL/api/v1/public/$HTML_PUB/raw")
echo "$HH" | grep -qi '^content-security-policy: sandbox' && pass "F4: /raw on HTML sets CSP sandbox" || fail "F4 html CSP" "$HH"

# SVG is an image/* MIME but scriptable — it must still be sandboxed (the CSP
# gate fails closed and doesn't let it ride the generic image/ exemption).
printf '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>' > /tmp/edge.svg
INIT=$(acurl -X POST "$BASE_URL/api/v1/files/$VAULT/upload?filename=edge.svg&collection=img&mime_type=image/svg%2Bxml")
SVG_URI=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["uri"])' 2>/dev/null)
SVG_FID=$(printf '%s' "$SVG_URI" | uri_file_id)
SVG_UURL=$(echo "$INIT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["upload_url"])' 2>/dev/null)
curl -sk -X PUT "$SVG_UURL" -H "Content-Type: image/svg+xml" --data-binary @/tmp/edge.svg > /dev/null
acurl -X POST "$BASE_URL/api/v1/files/$VAULT/$SVG_FID/confirm" > /dev/null
SVG_PUB=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"file\",\"uri\":\"$SVG_URI\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
SH=$(curl -sk -D - -o /dev/null "$BASE_URL/api/v1/public/$SVG_PUB/raw")
echo "$SH" | grep -qi '^content-security-policy: sandbox' && pass "CSP: /raw on SVG is sandboxed (image MIME but scriptable)" || fail "SVG CSP" "$SH"

# F3: a table_query returning non-finite floats (NaN/±Inf) must not 500 the JSON
# path (Starlette renders with allow_nan=False) — they coerce to null.
NANQ="SELECT float8 'NaN' AS nanv, float8 'Infinity' AS infv, float8 '2.5' AS okv"
NANP=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"table_query\",\"query_sql\":\"$NANQ\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$NANP")
[ "$CODE" = "200" ] && pass "F3: NaN/Inf table_query resolves 200 (not 500)" || fail "F3 nan 200" "HTTP $CODE"
NR=$(curl -sk "$BASE_URL/api/v1/public/$NANP")
echo "$NR" | python3 -c 'import json,sys; r=json.load(sys.stdin)["rows"][0]; sys.exit(0 if (r["nanv"] is None and r["infv"] is None and r["okv"]==2.5) else 1)' 2>/dev/null \
  && pass "F3: NaN/Inf → null, finite float preserved" || fail "F3 nan coercion" "$NR"

# M3: an issued auth token is revoked immediately by unpublish — it bypasses only
# the password check, never existence, so a deleted publication 404s even with a
# still-live token.
M3SLUG=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"password\":\"m3pass\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
M3TOK=$(curl -sk -X POST "$BASE_URL/api/v1/public/$M3SLUG/auth" -H "Content-Type: application/json" \
  -d '{"password":"m3pass"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null)
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$M3SLUG?token=$M3TOK")
[ "$CODE" = "200" ] && pass "M3: token authorizes before unpublish" || fail "M3 token pre" "HTTP $CODE"
acurl -X DELETE "$BASE_URL/api/v1/publications/$VAULT/$M3SLUG" > /dev/null
CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$M3SLUG?token=$M3TOK")
[ "$CODE" = "404" ] && pass "M3: same token → 404 after unpublish (immediate revocation)" || fail "M3 token revoke" "HTTP $CODE"

# Re-snapshot (should overwrite, not error)
RS_TQ=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d '{"resource_type":"table_query","query_sql":"SELECT name FROM products LIMIT 1"}')
RS_SLUG=$(echo "$RS_TQ" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/$RS_SLUG/snapshot" > /dev/null
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/$RS_SLUG/snapshot")
SS2=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("snapshot_at",""))' 2>/dev/null)
[ -n "$SS2" ] && pass "Re-snapshot is idempotent" || fail "Re-snapshot" "$R"

# Snapshot with 0 rows still flips mode and reports snapshot_at
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"table_query\",\"query_sql\":\"SELECT name FROM products WHERE name = 'NeverExists'\"}")
EMP_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/$EMP_SLUG/snapshot")
EMP_MODE=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mode"))' 2>/dev/null)
[ "$EMP_MODE" = "snapshot" ] && pass "Snapshot 0 rows flips mode" || fail "Empty snapshot" "$R"
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
  -d "{\"resource_type\":\"file\",\"uri\":\"$FILE_URI\"}")
CSC_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
acurl -X DELETE "$BASE_URL/api/v1/files/$VAULT/$FID" > /dev/null
# An empty slug would request /public/ — 404 by routing, which is exactly what
# "the publication was cascaded away" asserts. Require a real slug to check.
if [ -n "$CSC_SLUG" ]; then
  CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$CSC_SLUG")
  [ "$CODE" = "404" ] && pass "File delete cascades publications (404)" || fail "File cascade" "HTTP $CODE"
else
  fail "File cascade" "no publication slug to check the cascade against: $R"
fi

# Cascade: document delete → publications cascade
R=$(acurl -X POST "$BASE_URL/api/v1/documents" -H "Content-Type: application/json" \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"docs\",\"title\":\"To Delete\",\"content\":\"# tmp\",\"type\":\"note\"}")
CASCADE_DOC=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])' 2>/dev/null)
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"akb://$VAULT/doc/$CASCADE_DOC\"}")
DOC_CSC_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
acurl -X DELETE "$BASE_URL/api/v1/documents/$VAULT/$CASCADE_DOC" > /dev/null
if [ -n "$DOC_CSC_SLUG" ]; then
  CODE=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$DOC_CSC_SLUG")
  [ "$CODE" = "404" ] && pass "Document delete cascades publications (404)" || fail "Doc cascade" "HTTP $CODE"
else
  fail "Doc cascade" "no publication slug to check the cascade against: $R"
fi

# Cascade: empty vault delete
DELV=$(mcp 90 akb_delete_vault "{\"vault\":\"${VAULT}-empty\"}" | mcp_text)
DELOK=$(echo "$DELV" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("deleted"))' 2>/dev/null)
[ "$DELOK" = "True" ] && pass "Empty vault deleted" || fail "Empty vault delete" "$DELV"

# section_not_found field is True when filter missing
R=$(acurl -X POST "$BASE_URL/api/v1/publications/$VAULT/create" -H "Content-Type: application/json" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOC_URI\",\"section_filter\":\"NoSuchHeading\"}")
SNF_SLUG=$(echo "$R" | python3 -c 'import json,sys; print(json.load(sys.stdin)["slug"])' 2>/dev/null)
SNF=$(curl -sk "$BASE_URL/api/v1/public/$SNF_SLUG" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("section_not_found"))' 2>/dev/null)
[ "$SNF" = "True" ] && pass "section_not_found=true when filter missing" || fail "section_not_found" "$SNF"

echo ""

# ── 99. Cleanup ───────────────────────────────────────────
echo "▸ 99. Cleanup"
# Deleting the loaded vault is also the cascade check for a vault that still owns
# publications, tables and files — so inspect the result instead of assuming it.
DELV=$(mcp 99 akb_delete_vault "{\"vault\":\"$VAULT\"}" | mcp_text)
DELOK=$(echo "$DELV" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("deleted"))' 2>/dev/null)
[ "$DELOK" = "True" ] && pass "Cleanup: test vault deleted" || fail "Cleanup vault delete" "$DELV"

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
