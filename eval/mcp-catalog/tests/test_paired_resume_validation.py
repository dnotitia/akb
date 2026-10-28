from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp_catalog.checkpoint import (
    CheckpointBudget,
    CheckpointDocument,
    CheckpointHeader,
    CheckpointTiming,
    SmokeCellCheckpoint,
    TrialCheckpoint,
    checkpoint_record_hash,
    smoke_record_hash,
    spent_hash,
    timing_hash,
    valid_completed_outcome,
)
from mcp_catalog.contracts import hash_json, load_run_manifest, load_task_corpus
from mcp_catalog.execution import TrialOutcome
from mcp_catalog.runner import (
    BenchmarkRunner,
    _build_artifact_hash_input,
    validate_paired_resume_parent,
)
from mcp_catalog.runtime import RuntimeDescriptor
from paired_artifact_factory import (
    SYNTHETIC_SOURCE_REVISION,
    _runtime_descriptor,
    complete_paired_artifacts,
)

ROOT = Path(__file__).parents[1]


def _parent_checkpoint(
    artifact: dict[str, Any],
    *,
    path: Path,
    manifest,
    tasks,
    descriptor: RuntimeDescriptor,
) -> CheckpointDocument:
    arm = artifact["arm"]
    runner = BenchmarkRunner(manifest, tasks, descriptor, arm=arm)
    planned = runner._planned_keys(SYNTHETIC_SOURCE_REVISION)
    rows = {
        (
            outcome.model_class,
            outcome.transport,
            outcome.task_id,
            outcome.repeat_index,
        ): outcome
        for run in artifact["runs"].values()
        for outcome in (TrialOutcome.model_validate(row) for row in run["trials"])
    }
    records: dict[str, TrialCheckpoint] = {}
    all_outcomes: list[TrialOutcome] = []
    for digest, key in planned.items():
        identity = (key.model_class, key.transport, key.task_id, key.repeat_index)
        outcome = rows[identity]
        all_outcomes.append(outcome)
        records[digest] = TrialCheckpoint(
            status="completed",
            key=key,
            outcome=outcome,
            record_hash=checkpoint_record_hash("completed", key, outcome),
        )

    expected_smoke = {
        cell["cell"]: TrialOutcome.model_validate(cell["outcome"])
        for cell in artifact["smoke_gate"]["cells"]
    }
    smoke_records: dict[str, SmokeCellCheckpoint] = {}
    model_by_class = {model.class_name: model for model in manifest.models}
    for cell_key, outcome in expected_smoke.items():
        model_class, transport = cell_key.split(":", 1)
        smoke_records[cell_key] = SmokeCellCheckpoint(
            status="completed",
            model_class=model_class,
            model_id=model_by_class[model_class].model_id,
            transport=transport,
            outcome=outcome,
            record_hash=smoke_record_hash("completed", model_class, model_by_class[model_class].model_id, transport, outcome),
        )
        all_outcomes.append(outcome)

    spent = CheckpointBudget(
        model_requests=sum(item.model_requests for item in all_outcomes),
        provider_setup_requests=0,
        input_tokens=sum(item.input_tokens for item in all_outcomes),
        output_tokens=sum(item.output_tokens for item in all_outcomes),
        cost_usd=float(sum((Decimal(str(item.cost_usd)) for item in all_outcomes), Decimal("0"))),
        wall_seconds=10.0,
        model_work_seconds=float(sum((Decimal(str(item.latency_seconds)) for item in all_outcomes), Decimal("0"))),
    )
    timing = CheckpointTiming()
    header = CheckpointHeader(
        source_revision=SYNTHETIC_SOURCE_REVISION,
        run_manifest_hash=hash_json(manifest.model_dump(mode="json")),
        task_corpus_hash=hash_json([task.model_dump(mode="json") for task in tasks]),
        arm=arm,
        pre_smoke_seal_hash=artifact["pre_smoke_seal_hash"],
        pre_smoke_seal_inputs=artifact["pre_smoke_seal_inputs"],
    )
    document = CheckpointDocument(
        lifecycle="finalized",
        header=header,
        spent=spent,
        reserved_cost_usd=0,
        reused_trial_count=0,
        rerun_trial_count=0,
        spent_hash=spent_hash(spent),
        timing=timing,
        timing_hash=timing_hash(timing),
        records=records,
        smoke_status="passed",
        smoke_gate=smoke_records,
    )
    path.write_text(json.dumps(document.model_dump(mode="json")), encoding="utf-8")

    artifact["completed_trials"] = len(records)
    artifact["checkpoint"] = {
        "enabled": True,
        "path": str(path),
        "new_trials": len(records),
        "reused_trials": 0,
        "rerun_trials": 0,
        "valid_completed_trials": sum(
            record.status == "completed" and valid_completed_outcome(record.outcome)
            for record in records.values()
        ),
        "lifecycle": document.lifecycle,
        "record_count": len(records),
        "reserved_cost_usd": document.reserved_cost_usd,
        "spent": document.spent.model_dump(mode="json"),
        "spent_hash": document.spent_hash,
        "timing": document.timing.model_dump(mode="json"),
        "timing_hash": document.timing_hash,
        "smoke_status": document.smoke_status,
        "smoke_cell_count": len(document.smoke_gate),
    }
    artifact["paired_budget_used"]["final_reserved_cost_usd"] = "0"
    trial_order = artifact["artifact_hash_input"]["trial_order"]
    artifact["artifact_hash_input"] = _build_artifact_hash_input(artifact, trial_order=trial_order)
    artifact["artifact_hash"] = hash_json(artifact["artifact_hash_input"])
    return document


def test_paired_resume_validates_both_parents_before_continuation(tmp_path: Path) -> None:
    manifest = load_run_manifest(ROOT / "config" / "run.json")
    tasks = load_task_corpus(ROOT / "corpus" / "tasks.json")
    descriptor = RuntimeDescriptor.from_dict(_runtime_descriptor(manifest))
    artifacts = dict(zip(("baseline", "candidate"), complete_paired_artifacts(), strict=True))
    checkpoint_paths = {arm: tmp_path / f"{arm}.json" for arm in artifacts}
    original_bytes: dict[str, bytes] = {}
    for arm, artifact in artifacts.items():
        _parent_checkpoint(
            artifact,
            path=checkpoint_paths[arm],
            manifest=manifest,
            tasks=tasks,
            descriptor=descriptor,
        )
        original_bytes[arm] = checkpoint_paths[arm].read_bytes()

    events, resume_source, paired_budget = validate_paired_resume_parent(
        artifacts,
        checkpoint_paths,
        manifest=manifest,
        tasks=tasks,
        descriptors={"baseline": descriptor, "candidate": descriptor},
    )

    assert len(events) == 608
    assert resume_source["source_accounting"]["residual"]["provider_setup_requests"] == 6
    assert paired_budget["final_reserved_cost_usd"] == "0"
    assert resume_source["parent_artifact_hashes"] == {
        arm: artifacts[arm]["artifact_hash"] for arm in artifacts
    }
    assert {arm: path.read_bytes() for arm, path in checkpoint_paths.items()} == original_bytes
