"""The aggregator's command-line contract, and what it does with runs that
predate the readout.

Two things are easy to get wrong here and expensive to notice late: half a
cost tradeoff silently reads as a free win, and a run nobody measured reads
as a run that sent nothing.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

BENCH_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(args: list[str], runs_dir: Path, **env_extra) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "RUNS_DIR": str(runs_dir), **env_extra}
    return subprocess.run(
        [sys.executable, "-m", "src.judge", "--aggregate", *args],
        cwd=BENCH_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _seed_run(
    runs_dir: Path,
    arm: str = "A1_search_only",
    verdicts: tuple[str, ...] = ("PASS", "PASS", "FAIL"),
    calls: list[dict] | None = None,
) -> None:
    arm_dir = runs_dir / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    if calls is None:
        calls = [
            {"name": "akb_search", "result_chars": 1800, "result_text": "x"},
            {"name": "akb_drill_down", "result_chars": 5200, "result_text": "y"},
        ]
    for index, verdict in enumerate(verdicts, start=1):
        qid = f"q{index:03d}"
        (arm_dir / f"{qid}.judge.json").write_text(json.dumps({
            "verdict": verdict,
            "must_mention_matched": ["a"] if verdict == "PASS" else [],
            "must_mention_missing": [] if verdict == "PASS" else ["a"],
            "forbidden_found": [],
            "faithfulness": "high",
            "_qid": qid,
            "_arm": arm,
            "_provenance": {"retrieved": ["a"], "retrieved_rate": 1.0},
            "_summary_stats": {
                "tool_calls": len(calls), "tokens": 12000, "wall_seconds": 40.0,
                "iterations": 3, "category": "decision-lookup", "abort_reason": None,
            },
        }))
        (arm_dir / f"{qid}.summary.json").write_text(json.dumps({
            "tool_calls_clean": calls,
            "usage_total": {"total_tokens": 12000},
            "wall_seconds": 40.0,
        }))


@pytest.fixture
def runs(tmp_path):
    # The arm list is chosen from the runs-dir name, so keep the v4 marker.
    directory = tmp_path / "runs_v4_test"
    directory.mkdir()
    return directory


# ── the cost inputs go together ─────────────────────────────────────


def test_no_cost_inputs_reports_the_gate_only(runs):
    _seed_run(runs)
    result = _run_cli([], runs)

    assert result.returncode == 0, result.stderr
    assert "pass/fail only" in result.stdout
    assert "break-even p" not in result.stdout
    metrics = json.loads((runs / "metrics.json").read_text())
    assert metrics["A1_search_only"]["tradeoff"] is None
    assert metrics["A1_search_only"]["gate"] == "FAIL"


def test_both_cost_inputs_produce_the_tradeoff(runs):
    _seed_run(runs)
    result = _run_cli(["--redirect-cost", "8000", "--saving-per-call", "250"], runs)

    assert result.returncode == 0, result.stderr
    assert "break-even p" in result.stdout
    metrics = json.loads((runs / "metrics.json").read_text())
    tradeoff = metrics["A1_search_only"]["tradeoff"]
    assert [row["floor"] for row in tradeoff] == [0.95, 0.90, 0.80]


@pytest.mark.parametrize(
    "args",
    [["--redirect-cost", "8000"], ["--saving-per-call", "250"]],
)
def test_one_cost_input_alone_is_refused(runs, args):
    _seed_run(runs)
    result = _run_cli(args, runs)

    assert result.returncode != 0
    assert "go together" in result.stderr


# ── the gate as an exit code ────────────────────────────────────────


def test_reading_a_failing_run_does_not_fail_the_shell(runs):
    _seed_run(runs, verdicts=("FAIL", "FAIL", "FAIL"))
    assert _run_cli([], runs).returncode == 0


def test_fail_on_gate_exits_non_zero_below_the_floor(runs):
    _seed_run(runs, verdicts=("PASS", "PASS", "FAIL"))
    result = _run_cli(["--fail-on-gate"], runs)

    assert result.returncode == 1
    assert "gate FAILED" in result.stderr
    assert "A1_search_only" in result.stderr


def test_fail_on_gate_passes_when_every_arm_clears_the_floor(runs):
    _seed_run(runs, verdicts=("PASS", "PASS", "PASS"))
    assert _run_cli(["--fail-on-gate"], runs).returncode == 0


def test_a_lower_floor_can_clear_the_same_run(runs):
    _seed_run(runs, verdicts=("PASS", "PASS", "FAIL"))
    assert _run_cli(["--fail-on-gate", "--accuracy-floor", "0.6"], runs).returncode == 0


# ── payload from older runs ─────────────────────────────────────────


def test_a_summary_with_only_result_text_is_still_measured(runs):
    _seed_run(runs, calls=[{"name": "akb_search", "result_text": "y" * 1234}])
    _run_cli([], runs)

    payload = json.loads((runs / "metrics.json").read_text())["A1_search_only"]["payload"]
    assert payload["calls"] == 3
    assert payload["chars_per_call"] == 1234


def test_an_unmeasurable_run_reports_unknown_not_zero(runs):
    _seed_run(runs, calls=[{"name": "akb_search"}])
    result = _run_cli([], runs)

    assert "unknown" in result.stdout
    payload = json.loads((runs / "metrics.json").read_text())["A1_search_only"]["payload"]
    assert payload is None


def test_an_arm_that_made_no_call_reports_a_measured_zero(runs):
    _seed_run(runs, calls=[])
    _run_cli([], runs)

    payload = json.loads((runs / "metrics.json").read_text())["A1_search_only"]["payload"]
    assert payload is not None
    assert payload["calls"] == 0
    assert payload["chars_per_call"] == 0.0


# ── one evalset for the whole pipeline ──────────────────────────────


@pytest.fixture
def stubbed_mcp_client(monkeypatch):
    """`src.runner` reaches the MCP SDK through `src.mcp_client`, whose SDK
    pin is the bench's own and need not match whatever interpreter runs these
    tests. The path resolution under test has nothing to do with it, so the
    module is stubbed rather than imported."""
    import types

    stub = types.ModuleType("src.mcp_client")
    stub.mcp_session = None
    stub.call_tool = None
    stub.list_tools_full = None
    stub.MCPCallError = RuntimeError
    monkeypatch.setitem(sys.modules, "src.mcp_client", stub)
    return stub


def test_every_stage_resolves_the_same_evalset(monkeypatch, stubbed_mcp_client):
    seed = BENCH_ROOT / "evalset-project-akb"
    monkeypatch.setenv("EVALSET_DIR", str(seed))

    import src.judge
    import src.paths
    import src.prep_judge_v3
    import src.react_agent
    import src.runner

    importlib.reload(src.paths)
    importlib.reload(src.react_agent)
    judge = importlib.reload(src.judge)
    runner = importlib.reload(src.runner)
    prep = importlib.reload(src.prep_judge_v3)
    try:
        # One helper, one answer — a run whose questions changed between the
        # runner and the judge would score answers against other questions.
        assert {judge.EVALSET, runner.EVALSET, prep.EVALSET} == {seed}
        assert judge.list_query_ids() == ["q001", "q002", "q003"]
        assert runner.list_queries() == ["q001", "q002", "q003"]
    finally:
        monkeypatch.delenv("EVALSET_DIR", raising=False)
        for module in (src.paths, src.judge, src.runner, src.prep_judge_v3):
            importlib.reload(module)


def test_the_default_evalset_is_unchanged_without_the_variable(monkeypatch, stubbed_mcp_client):
    monkeypatch.delenv("EVALSET_DIR", raising=False)

    import src.judge
    import src.paths

    importlib.reload(src.paths)
    judge = importlib.reload(src.judge)

    assert judge.EVALSET == BENCH_ROOT / "evalset"
