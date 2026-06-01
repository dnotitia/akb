#!/usr/bin/env bash
# Reproduction probes for the P0 publication-authorization findings and the
# P1 collection-scoped search crash. Prints a VULNERABLE / SAFE verdict per
# bug so the same script documents the BEFORE (vulnerable) and AFTER (fixed)
# states. Targets the audit Docker stack on :8001 by default.
#
#   bash backend/tests/concurrency/repro_pub_security.sh
#
# Exits non-zero if any probe is still VULNERABLE (so AFTER runs are a gate).

set -uo pipefail
AKB_URL="${AKB_URL:-http://localhost:8001}"
JSON=(-H "Content-Type: application/json")

VULN=0
verdict() { # name, state(VULNERABLE|SAFE), detail
  if [ "$2" = "VULNERABLE" ]; then echo "  ✗ $1: VULNERABLE — $3"; VULN=$((VULN+1));
  else echo "  ✓ $1: SAFE — $3"; fi
}

reg() { # username -> prints PAT
  local u="$1" pw="testtest1234"
  curl -s --max-time 20 -X POST "$AKB_URL/api/v1/auth/register" "${JSON[@]}" \
    -d "{\"username\":\"$u\",\"email\":\"$u@t.local\",\"password\":\"$pw\"}" >/dev/null
  local jwt
  jwt=$(curl -s --max-time 20 -X POST "$AKB_URL/api/v1/auth/login" "${JSON[@]}" \
    -d "{\"username\":\"$u\",\"password\":\"$pw\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
  curl -s --max-time 20 -X POST "$AKB_URL/api/v1/auth/tokens" -H "Authorization: Bearer $jwt" "${JSON[@]}" \
    -d '{"name":"repro"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'
}

TS=$(date +%s)
U1="reprou1-$TS"; U2="reprou2-$TS"
VA="repro-a-$TS"; VB="repro-b-$TS"

echo "▸ Setup"
PAT1=$(reg "$U1"); PAT2=$(reg "$U2")
[ -z "$PAT1" ] || [ -z "$PAT2" ] && { echo "auth setup failed"; exit 2; }
AUTH1=(-H "Authorization: Bearer $PAT1"); AUTH2=(-H "Authorization: Bearer $PAT2")
curl -s --max-time 20 -X POST "$AKB_URL/api/v1/vaults?name=$VA" "${AUTH1[@]}" >/dev/null   # user1 owns A
curl -s --max-time 20 -X POST "$AKB_URL/api/v1/vaults?name=$VB" "${AUTH2[@]}" >/dev/null   # user2 owns B
# a table in A so build_table_name_map(A) resolves
curl -s --max-time 20 -X POST "$AKB_URL/tables/$VA" "${AUTH1[@]}" "${JSON[@]}" \
  -d '{"name":"items","columns":[{"name":"label","type":"text"}]}' >/dev/null
echo "  user1=$U1 vaultA=$VA · user2=$U2 vaultB=$VB"
echo ""

create_pub() { # vault, body, authvar... -> prints "slug publication_id"; auth passed via global AUTHx
  local vault="$1" body="$2"; shift 2
  curl -s --max-time 20 -X POST "$AKB_URL/api/v1/publications/$vault/create" "$@" "${JSON[@]}" -d "$body" \
    | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    print("",""); raise SystemExit
print(d.get("slug","") or "", d.get("publication_id","") or "")' 2>/dev/null
}

# ── P0-1: public table_query leaks a system table ─────────────────────
echo "▸ P0-1: public table_query runs against system tables (data exfiltration)"
read -r SLUG1 PID1 < <(create_pub "$VA" "{\"resource_type\":\"table_query\",\"query_sql\":\"SELECT count(*) AS n FROM users\",\"title\":\"leak\"}" "${AUTH1[@]}")
if [ -z "$SLUG1" ]; then
  verdict "P0-1" "SAFE" "publication creation rejected the system-table query (slug empty)"
else
  BODY=$(curl -s --max-time 20 "$AKB_URL/api/v1/public/$SLUG1")
  WHO=$(create_pub "$VA" "{\"resource_type\":\"table_query\",\"query_sql\":\"SELECT current_user AS who\",\"title\":\"who\"}" "${AUTH1[@]}")
  WHOSLUG=$(echo "$WHO" | awk '{print $1}')
  WHOBODY=$(curl -s --max-time 20 "$AKB_URL/api/v1/public/$WHOSLUG")
  ROLE=$(echo "$WHOBODY" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); r=d.get("rows") or [{}]
    print(r[0].get("who",""))
except Exception:
    print("")' 2>/dev/null)
  # VULNERABLE if the public (unauthenticated) view actually read the users
  # table, i.e. returned a numeric count, OR runs as a non per-user role.
  if echo "$BODY" | grep -qE '"n":[0-9]+'; then
    verdict "P0-1" "VULNERABLE" "public view read users table → $(echo "$BODY" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("rows"))' 2>/dev/null); runs as role=${ROLE:-?}"
  elif [ -n "$ROLE" ] && [[ "$ROLE" != akb_user_* ]]; then
    verdict "P0-1" "VULNERABLE" "public query runs as privileged role '$ROLE' (not a per-user akb_user_ role)"
  else
    verdict "P0-1" "SAFE" "users-table read denied; query runs as role='${ROLE:-?}'"
  fi
fi
echo ""

# ── P0-3: delete_publication IDOR (writer on B deletes A's publication) ─
echo "▸ P0-3: delete_publication ignores vault binding (IDOR)"
read -r SLUG3 PID3 < <(create_pub "$VA" "{\"resource_type\":\"table_query\",\"query_sql\":\"SELECT label FROM items\",\"title\":\"idor-target\"}" "${AUTH1[@]}")
if [ -z "$PID3" ]; then
  echo "  (could not create target publication; skipping)"
else
  # user2 has zero access to vault A; deletes A's pub via the vault-B route.
  DEL=$(curl -s --max-time 20 -o /dev/null -w "%{http_code}" -X DELETE \
    "$AKB_URL/api/v1/publications/$VB/$PID3" "${AUTH2[@]}")
  DELBODY=$(curl -s --max-time 20 "$AKB_URL/api/v1/public/$SLUG3" -o /dev/null -w "%{http_code}")
  # If the pub is now gone (public view 404) AND the delete returned 200, it was deleted cross-vault.
  STILL=$(curl -s --max-time 20 -X GET "$AKB_URL/api/v1/publications/$VA" "${AUTH1[@]}" \
    | python3 -c "import sys,json;print(sum(1 for p in json.load(sys.stdin).get('publications',[]) if p.get('publication_id')=='$PID3'))" 2>/dev/null)
  if [ "$STILL" = "0" ]; then
    verdict "P0-3" "VULNERABLE" "user2 (no access to A) deleted A's publication via /publications/$VB/ (http=$DEL)"
  else
    verdict "P0-3" "SAFE" "cross-vault delete rejected; publication still present (http=$DEL)"
  fi
fi
echo ""

# ── P0-4: create_snapshot cross-vault ─────────────────────────────────
echo "▸ P0-4: create_snapshot ignores vault binding (cross-vault execution)"
read -r SLUG4 PID4 < <(create_pub "$VA" "{\"resource_type\":\"table_query\",\"query_sql\":\"SELECT label FROM items\",\"title\":\"snap-target\"}" "${AUTH1[@]}")
if [ -z "$PID4" ]; then
  echo "  (could not create target publication; skipping)"
else
  SNAP=$(curl -s --max-time 30 -o /dev/null -w "%{http_code}" -X POST \
    "$AKB_URL/api/v1/publications/$VB/$PID4/snapshot" "${AUTH2[@]}")
  # A vault-bound impl rejects the cross-vault publication with 404 BEFORE
  # ever touching the query/S3. Anything else (200 success, or 400/500 from
  # reaching the S3 step) means the vault binding was not enforced. Note the
  # audit stack has no S3, so BEFORE this surfaces as a non-404 error.
  if [ "$SNAP" = "404" ]; then
    verdict "P0-4" "SAFE" "cross-vault snapshot rejected with 404 before execution"
  else
    verdict "P0-4" "VULNERABLE" "snapshot of A's publication via vault-B route was not vault-rejected (http=$SNAP; reached execution)"
  fi
fi
echo ""

# ── P1-3: collection-scoped search crashes on dropped f.collection ────
echo "▸ P1-3: collection-scoped search references dropped vault_files.collection column"
# a doc under a collection so the search has a corpus + a collection filter
curl -s --max-time 20 -X POST "$AKB_URL/documents" "${AUTH1[@]}" "${JSON[@]}" \
  -d "{\"vault\":\"$VA\",\"collection\":\"specs\",\"title\":\"sdoc\",\"content\":\"# searchable marker xyzzy\",\"type\":\"note\"}" >/dev/null
SC=$(curl -s --max-time 20 -o /dev/null -w "%{http_code}" \
  "$AKB_URL/api/v1/search?q=searchable&vault=$VA&collection=specs" "${AUTH1[@]}")
if [ "$SC" = "500" ]; then
  verdict "P1-3" "VULNERABLE" "search?collection= returned HTTP 500 (UndefinedColumn f.collection)"
elif [ "$SC" = "200" ]; then
  verdict "P1-3" "SAFE" "collection-scoped search returned HTTP 200"
else
  verdict "P1-3" "VULNERABLE" "unexpected http=$SC (expected 200 after fix)"
fi
echo ""

# ── Cleanup ───────────────────────────────────────────────────────────
curl -s --max-time 15 -X DELETE "$AKB_URL/api/v1/vaults/$VA" "${AUTH1[@]}" >/dev/null 2>&1 || true
curl -s --max-time 15 -X DELETE "$AKB_URL/api/v1/vaults/$VB" "${AUTH2[@]}" >/dev/null 2>&1 || true

echo "════════════════════════════════════════════"
if [ "$VULN" -gt 0 ]; then
  echo "  $VULN probe(s) VULNERABLE"
else
  echo "  All probes SAFE"
fi
echo "════════════════════════════════════════════"
exit "$VULN"
