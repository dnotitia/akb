#!/bin/bash
#
# Upload vault/document templates to AKB _system vault
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "${AKB_PAT:-}" ]; then
  echo "Usage: AKB_PAT=akb_xxx ./upload.sh"
  echo "  Requires admin PAT."
  exit 1
fi

acurl() { curl -sk -H "Authorization: Bearer $AKB_PAT" "$@"; }

echo "=== Creating _system vault ==="
acurl -X POST "$BASE_URL/api/v1/vaults?name=_system&description=System+templates+and+configuration" 2>/dev/null
echo ""

echo "=== Uploading vault templates ==="
for f in "$SCRIPT_DIR"/vault-templates/*.yaml; do
  NAME=$(basename "$f" .yaml)
  CONTENT=$(cat "$f" | python3 -c "import sys,yaml,json; print(json.dumps(yaml.safe_load(sys.stdin.read()), indent=2, ensure_ascii=False))" 2>/dev/null)
  if [ -z "$CONTENT" ]; then
    # Fallback: upload as-is
    CONTENT=$(cat "$f")
  fi

  ESCAPED=$(echo "$CONTENT" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" | sed 's/^"//;s/"$//')

  R=$(acurl -X POST "$BASE_URL/api/v1/documents" \
    -H 'Content-Type: application/json' \
    -d "{\"vault\":\"_system\",\"collection\":\"vault-templates\",\"title\":\"Template: $NAME\",\"content\":\"$ESCAPED\",\"type\":\"reference\",\"tags\":[\"template\",\"vault\",\"$NAME\"]}" 2>/dev/null)
  DOC_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id','FAIL'))" 2>/dev/null)
  echo "  $NAME → $DOC_ID"
done

echo ""
echo "=== Uploading document templates ==="
for f in "$SCRIPT_DIR"/doc-templates/*.md; do
  NAME=$(basename "$f" .md)
  CONTENT=$(cat "$f" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" | sed 's/^"//;s/"$//')

  R=$(acurl -X POST "$BASE_URL/api/v1/documents" \
    -H 'Content-Type: application/json' \
    -d "{\"vault\":\"_system\",\"collection\":\"doc-templates\",\"title\":\"Template: $NAME\",\"content\":\"$CONTENT\",\"type\":\"reference\",\"tags\":[\"template\",\"document\",\"$NAME\"]}" 2>/dev/null)
  DOC_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id','FAIL'))" 2>/dev/null)
  echo "  $NAME → $DOC_ID"
done

echo ""
echo "=== Done ==="
echo "Templates available in _system vault."
echo "Use: akb_browse(vault='_system') to view."
