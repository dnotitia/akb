#!/bin/bash

# Shared test-only caller for AKB's MCP 2026-07-28 stateless HTTP surface.
# It deliberately has no session-id or initialize fallback so endpoint suites
# exercise the public contract directly.

MCP_PROTOCOL_VERSION="2026-07-28"

mcp_modern_request() {
  local pat=$1
  local request_id=$2
  local method=$3
  local params_json=${4:-{}}
  local name=${5:-}
  local client_name=${6:-mcp-e2e}
  local body

  body=$(python3 - "$request_id" "$method" "$params_json" "$client_name" <<'PY'
import json
import sys

request_id = int(sys.argv[1])
method = sys.argv[2]
params = json.loads(sys.argv[3])
client_name = sys.argv[4]
if not isinstance(params, dict):
    raise SystemExit("MCP params must be a JSON object")
params = dict(params)
params["_meta"] = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": client_name, "version": "1"},
}
print(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, separators=(",", ":")))
PY
  )

  local -a headers=(
    -H "Authorization: Bearer $pat"
    -H "Content-Type: application/json"
    -H "Accept: application/json"
    -H "Mcp-Protocol-Version: $MCP_PROTOCOL_VERSION"
    -H "Mcp-Method: $method"
  )
  if [ -n "$name" ]; then
    headers+=(-H "Mcp-Name: $name")
  fi
  curl -sk -X POST "${BASE_URL}/mcp/" "${headers[@]}" -d "$body" 2>&1
}

mcp_modern_discover() {
  mcp_modern_request "$1" 1 server/discover '{}' '' "${2:-mcp-e2e}"
}

mcp_modern_call() {
  local pat=$1
  local tool=$2
  local args=$3
  local request_id=${4:-$RANDOM}
  local client_name=${5:-mcp-e2e}
  mcp_modern_request "$pat" "$request_id" tools/call \
    "$(python3 - "$tool" "$args" <<'PY'
import json
import sys
print(json.dumps({"name": sys.argv[1], "arguments": json.loads(sys.argv[2])}, separators=(",", ":")))
PY
)" "$tool" "$client_name"
}
