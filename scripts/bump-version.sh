#!/usr/bin/env bash
# Bump the AKB product version.
#
# Single source of truth: backend/pyproject.toml ([project].version).
# frontend/package.json is mirrored to the same value.
#
# packages/akb-mcp-client (the `akb-mcp` npm proxy) follows its own npm
# semver lifecycle and is NOT touched here — bump it separately when the
# proxy itself changes.
#
# Usage:  scripts/bump-version.sh <x.y.z>
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: $0 <x.y.z>" >&2
  exit 1
fi

NEW="$1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

python3 - "$ROOT/backend/pyproject.toml" "$NEW" <<'PY'
import pathlib, re, sys
path, new = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
text2, n = re.subn(r'^version = "[^"]+"', f'version = "{new}"', text, count=1, flags=re.M)
if n == 0:
    sys.exit("pyproject.toml: [project].version line not found")
path.write_text(text2)
PY

python3 - "$ROOT/frontend/package.json" "$NEW" <<'PY'
import json, pathlib, sys
path, new = pathlib.Path(sys.argv[1]), sys.argv[2]
data = json.loads(path.read_text())
data["version"] = new
path.write_text(json.dumps(data, indent=2) + "\n")
PY

echo "Bumped to $NEW:"
echo "  backend/pyproject.toml"
echo "  frontend/package.json"
echo ""
echo "Next:"
echo "  git commit -am 'chore: bump to v$NEW'"
echo "  git tag v$NEW"
echo "  git push && git push --tags"
echo "  REGISTRY=... deploy/k8s/deploy.sh   # builds and pushes :$NEW and :latest"
