from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mcp_catalog.cli as cli_module
from mcp_catalog.contracts import load_run_manifest

ROOT = Path(__file__).parents[1]


def test_run_paired_restores_one_parent_budget_after_readonly_validation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    manifest_payload = manifest.model_dump(mode="json")
    outputs = {
        "baseline": tmp_path / "baseline.json",
        "candidate": tmp_path / "candidate.json",
    }
    for path in outputs.values():
        path.write_text(json.dumps({"manifest": manifest_payload}), encoding="utf-8")
    checkpoint_paths = {
        "baseline": tmp_path / "baseline.checkpoint.json",
        "candidate": tmp_path / "candidate.checkpoint.json",
    }
    args = cli_module.build_parser().parse_args(
        [
            "run-paired",
            "--baseline-descriptor",
            str(tmp_path / "baseline-descriptor.json"),
            "--candidate-descriptor",
            str(tmp_path / "candidate-descriptor.json"),
            "--baseline-output",
            str(outputs["baseline"]),
            "--candidate-output",
            str(outputs["candidate"]),
            "--comparison-output",
            str(tmp_path / "comparison.json"),
            "--baseline-checkpoint",
            str(checkpoint_paths["baseline"]),
            "--candidate-checkpoint",
            str(checkpoint_paths["candidate"]),
            "--resume",
        ]
    )

    descriptor = SimpleNamespace(raw={"descriptor": "sealed"})
    loaded_manifests = []

    def load_inputs(*_args, manifest_override=None):
        loaded_manifests.append(manifest_override)
        return manifest, [], descriptor

    source = {
        "schema_version": 1,
        "source_accounting": {
            "budget_failure": None,
            "residual": {"model_requests": 0},
        },
    }
    parent_budget = {
        "model_requests": 12,
        "provider_setup_requests": 2,
        "input_tokens": 100,
        "output_tokens": 40,
        "cost_usd": 1.25,
        "wall_seconds": 20.0,
        "model_work_seconds": 8.0,
        "max_total_cost_usd": manifest.budget.max_total_cost_usd,
        "max_cost_per_trial_usd": manifest.budget.max_cost_per_trial_usd,
        "final_reserved_cost_usd": "0",
    }
    validation_calls = []

    def validate_parents(artifacts, paths, *, manifest, tasks, descriptors):
        validation_calls.append((artifacts, paths, manifest, tasks, descriptors))
        assert all(path.is_file() for path in outputs.values())
        return [], source, parent_budget

    created_runners: list[Any] = []

    class FakeRunner:
        def __init__(self, _manifest, _tasks, _descriptor, **kwargs) -> None:
            self.kwargs = kwargs
            self.secrets = ()
            self.arm = kwargs["arm"]
            created_runners.append(self)

        async def run(self):
            return {
                "arm": self.arm,
                "status": "incomplete",
                "artifact_hash_input": {"trial_order": []},
            }

    written = []
    attached_budgets = []
    monkeypatch.setattr(cli_module, "load_inputs", load_inputs)
    monkeypatch.setattr(cli_module, "validate_paired_resume_parent", validate_parents)
    monkeypatch.setattr(cli_module, "BenchmarkRunner", FakeRunner)
    monkeypatch.setattr(cli_module, "attach_paired_execution_evidence", lambda _artifact, **kwargs: attached_budgets.append(kwargs["budget_used"]))
    monkeypatch.setattr(cli_module, "_reconcile_paired_artifact_reservations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "compare_artifacts", lambda *_args: {"verdict": "inconclusive"})
    monkeypatch.setattr(cli_module, "write_json", lambda path, payload, *_args: written.append((path, payload)))

    assert cli_module.asyncio.run(cli_module.run_paired(args)) == 1

    assert len(validation_calls) == 1
    assert loaded_manifests == [manifest, manifest]
    assert len(created_runners) == 2
    assert all(runner.kwargs["preloaded_shared_budget"] is True for runner in created_runners)
    assert all(runner.kwargs["resume_source"] is source for runner in created_runners)
    ledger = created_runners[0].kwargs["shared_ledger"]
    assert ledger is created_runners[1].kwargs["shared_ledger"]
    assert (ledger.requests, ledger.provider_setup_requests) == (12, 2)
    assert (ledger.input_tokens, ledger.output_tokens, float(ledger.cost_usd)) == (100, 40, 1.25)
    assert attached_budgets[0]["model_requests"] == 12
    assert len(written) == 3
