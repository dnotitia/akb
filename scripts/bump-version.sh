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

python3 - "$ROOT" "$NEW" <<'PYCODE'
import json, pathlib, re, sys
root, new = pathlib.Path(sys.argv[1]), sys.argv[2]
project = root / "backend/pyproject.toml"
lock = root / "backend/uv.lock"
frontend = root / "frontend/package.json"
project_text, lock_text = project.read_text(), lock.read_text()
project_next, count = re.subn(
    r'(^\[project\]\n(?:(?!^\[).)*?^version = ")[^"]+("$)',
    lambda match: match[1] + new + match[2], project_text, flags=re.M | re.S,
)
if count != 1:
    sys.exit("pyproject.toml: project version not found")
# Change only the local editable package; registry versions and lock metadata
# remain unchanged. Refuse before any write if its unique block is missing.
lock_next, count = re.subn(
    r'(\[\[package\]\]\nname = "akb"\nversion = ")[^"]+("\nsource = \{ editable = "\." \})',
    lambda match: match[1] + new + match[2], lock_text,
)
if count != 1:
    sys.exit("uv.lock: unique editable akb package not found")
frontend_data = json.loads(frontend.read_text())
frontend_data["version"] = new
project.write_text(project_next)
lock.write_text(lock_next)
frontend.write_text(json.dumps(frontend_data, indent=2) + "\n")
PYCODE

echo "Bumped to $NEW:"
echo "  backend/pyproject.toml"
echo "  backend/uv.lock (editable package only)"
echo "  frontend/package.json"
echo ""
echo "Next:"
echo "  Review and merge the version/changelog PR, then check out its main merge commit."
echo "  git tag -a backend-v$NEW -m 'Backend $NEW'"
echo "  git push && git push origin backend-v$NEW"
echo "  REGISTRY=... AKB_PROFILE=standalone deploy/k8s/deploy.sh   # builds and pushes :$NEW and :latest"
