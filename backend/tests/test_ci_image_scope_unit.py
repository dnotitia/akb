"""Image selection must retain every affected package without duplicate PG builds."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/ci/image_scope.py"


@pytest.fixture
def image_scope():
    spec = importlib.util.spec_from_file_location("image_scope", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("paths", "expected"), [
    (["backend/app/main.py"], {"backend", "all-in-one"}),
    (["backend/mcp_server/server.py"], {"backend", "all-in-one"}),
    (["backend/pyproject.toml"], {"backend", "all-in-one"}),
    (["backend/.dockerignore"], {"backend", "all-in-one"}),
    (["frontend/src/pages/home.tsx"], {"frontend", "all-in-one"}),
    (["frontend/packages/markdown-editor/scripts/check-public-api.mjs"], {"frontend", "all-in-one"}),
    (["frontend/nginx.conf"], {"frontend", "all-in-one"}),
    (["deploy/all-in-one/app.yaml"], {"all-in-one"}),
    (["deploy/all-in-one/Dockerfile.dockerignore"], {"all-in-one"}),
    (["deploy/postgres/Dockerfile", "deploy/postgres/vchord_bm25/fix.patch"], set()),
    (["docs/operations/search-retrieval.md", "README.md"], set()),
    (["backend/tests/test_search.py", "backend/CHANGELOG.md"], set()),
    ([".agents/roles.toml", "AGENTS.md", "CLAUDE.md", ".claude/settings.json"], set()),
    (["packages/akb-mcp-client/index.mjs"], set()),
    ([".github/workflows/images.yml"], {"backend", "frontend", "all-in-one"}),
    (["scripts/ci/image_scope.py"], {"backend", "frontend", "all-in-one"}),
    ([".dockerignore"], {"backend", "frontend", "all-in-one"}),
    (["new-build-input.txt"], {"backend", "frontend", "all-in-one"}),
    (["backend/app/main.py", "frontend/src/app.tsx"], {"backend", "frontend", "all-in-one"}),
    ([], set()),
])
def test_changed_inputs_select_the_consuming_images(image_scope, paths, expected):
    assert image_scope.select_images(paths) == expected


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def test_pr_diff_keeps_deleted_and_renamed_build_inputs(tmp_path):
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "ci@example.invalid")
    _git(tmp_path, "config", "user.name", "CI test")
    source = tmp_path / "backend/app/example.py"
    source.parent.mkdir(parents=True)
    source.write_text("old content\n")
    deleted = tmp_path / "frontend/public/old asset.txt"
    deleted.parent.mkdir(parents=True)
    deleted.write_text("old asset\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "--quiet", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    destination = tmp_path / "docs/example.md"
    destination.parent.mkdir()
    source.rename(destination)
    deleted.unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "move source and delete asset")
    output = tmp_path / "outputs"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--event", "pull_request", "--base", base,
         "--head", _git(tmp_path, "rev-parse", "HEAD"), "--github-output", str(output)],
        cwd=tmp_path, text=True, capture_output=True, check=True,
    )
    assert json.loads(result.stdout) == ["all-in-one", "backend", "frontend"]
    assert output.read_text().splitlines() == [
        "backend=true", "frontend=true", "all-in-one=true", "any=true",
    ]


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "pull_request"])
def test_full_runs_and_unavailable_diff_never_skip_builds(tmp_path, event):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--event", event, "--base", "0" * 40, "--head", "1" * 40],
        cwd=tmp_path, text=True, capture_output=True, check=True,
    )
    assert json.loads(result.stdout) == ["all-in-one", "backend", "frontend"]
