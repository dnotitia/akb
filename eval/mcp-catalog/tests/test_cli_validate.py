from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_validate_rejects_infinite_per_trial_cost(tmp_path: Path) -> None:
    manifest = json.loads((ROOT / "config" / "run.json").read_text(encoding="utf-8"))
    manifest["budget"]["max_cost_per_trial_usd"] = float("inf")
    manifest_path = tmp_path / "run.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcp_catalog.cli",
            "validate",
            "--manifest",
            str(manifest_path),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 2
    assert "configuration_error" in completed.stderr
    assert "max_cost_per_trial_usd" in completed.stderr
