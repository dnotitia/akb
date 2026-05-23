"""Prepare batched judge inputs for hand-evaluation by the main session.

Each batch_NN.json contains 25 (qid, arm) prompts with question + answer
+ ground_truth so the main-session model can score them directly via
LLM reasoning (not substring matching).

Usage:
  python -m src.prep_judge_v3 prep      # write batches/batch_*.json
  python -m src.prep_judge_v3 finalize  # merge verdicts back to judge.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

import os
ROOT = Path(__file__).resolve().parent.parent
EVALSET = ROOT / "evalset"
RUNS = Path(os.environ.get("RUNS_DIR", ROOT / "runs_v3"))
BATCHES = RUNS / "batches"
VERDICTS = RUNS / "verdicts"
_V4_STYLE = any(s in str(RUNS) for s in ("v4", "v5", "v6", "v7", "v8", "v9"))
ARMS = ["A1_search_only", "A2_grep_only", "A3_tree", "A4_all"] if _V4_STYLE else ["A1_search_only", "A2_grep", "A3_drill"]
BATCH_SIZE = 20
ANSWER_CAP = 1200  # main session reasoning context budget


def all_pairs() -> list[tuple[str, str]]:
    qids = sorted(p.stem for p in EVALSET.glob("q*.yaml"))
    return [(qid, arm) for arm in ARMS for qid in qids]


def prep() -> None:
    BATCHES.mkdir(parents=True, exist_ok=True)
    pairs = all_pairs()
    for batch_idx in range(0, len(pairs), BATCH_SIZE):
        rows = []
        for qid, arm in pairs[batch_idx : batch_idx + BATCH_SIZE]:
            sum_path = RUNS / arm / f"{qid}.summary.json"
            if not sum_path.exists():
                continue
            summary = json.loads(sum_path.read_text())
            qspec = yaml.safe_load((EVALSET / f"{qid}.yaml").read_text(encoding="utf-8"))
            gt = qspec.get("ground_truth") or {}
            answer = summary.get("final_answer_text", "") or ""
            if len(answer) > ANSWER_CAP:
                answer = answer[:ANSWER_CAP] + f"\n<truncated, orig {len(answer)}>"
            rows.append({
                "qid": qid,
                "arm": arm,
                "category": qspec.get("category"),
                "question": qspec["query"],
                "answer": answer,
                "must_mention": gt.get("must_mention") or [],
                "forbidden": gt.get("forbidden") or [],
            })
        bid = batch_idx // BATCH_SIZE
        (BATCHES / f"batch_{bid:02d}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"batch_{bid:02d}: {len(rows)} rows")


def _normalize(s: str) -> str:
    return re.sub(r"[\s​ \.\,\(\)「」『』\"']+", "", s)


def _retrieved_split(must: list[str], tool_texts: list[str]) -> tuple[list[str], list[str]]:
    concat = _normalize(" ".join(tool_texts))
    retrieved, missing = [], []
    for fact in must:
        (retrieved if _normalize(fact) in concat else missing).append(fact)
    return retrieved, missing


def finalize() -> None:
    """Read verdicts_batch_*.json (main-session output) and write per-
    (qid, arm) judge.json with the same schema as judge.py."""
    verdict_files = sorted(VERDICTS.glob("verdicts_batch_*.json"))
    if not verdict_files:
        print("no verdicts_batch_*.json found in", VERDICTS, file=sys.stderr)
        sys.exit(1)
    all_verdicts: dict[tuple[str, str], dict] = {}
    for vf in verdict_files:
        for v in json.loads(vf.read_text()):
            all_verdicts[(v["qid"], v["arm"])] = v
    n = 0
    for (qid, arm), v in all_verdicts.items():
        sum_path = RUNS / arm / f"{qid}.summary.json"
        if not sum_path.exists():
            continue
        summary = json.loads(sum_path.read_text())
        qspec = yaml.safe_load((EVALSET / f"{qid}.yaml").read_text(encoding="utf-8"))
        gt = qspec.get("ground_truth") or {}
        must = gt.get("must_mention") or []
        tool_texts = [tc.get("result_text", "") for tc in summary.get("tool_calls_clean", [])]
        retrieved, from_prior = _retrieved_split(must, tool_texts)
        out = {
            "must_mention_matched": v.get("matched", []),
            "must_mention_missing": v.get("missing", []),
            "forbidden_found": v.get("forbidden_found", []),
            "faithfulness": v.get("faithfulness", "medium"),
            "verdict": v.get("verdict", "FAIL"),
            "reason": v.get("reason", ""),
            "_qid": qid,
            "_arm": arm,
            "_question": qspec["query"],
            "_answer": summary.get("final_answer_text", ""),
            "_provenance": {
                "retrieved": retrieved,
                "from_prior_or_missing": from_prior,
                "retrieved_rate": (len(retrieved) / len(must)) if must else None,
            },
            "_summary_stats": {
                "tool_calls": len(summary.get("tool_calls_clean", [])),
                "tokens": summary.get("usage_total", {}).get("total_tokens", 0),
                "wall_seconds": summary.get("wall_seconds", 0),
                "iterations": summary.get("iterations", 0),
                "category": qspec.get("category"),
                "abort_reason": summary.get("abort_reason"),
            },
        }
        (RUNS / arm / f"{qid}.judge.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        n += 1
    print(f"wrote {n} judge.json files")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prep"
    if cmd == "prep":
        prep()
    elif cmd == "finalize":
        finalize()
    else:
        print("usage: prep | finalize", file=sys.stderr)
        sys.exit(1)
