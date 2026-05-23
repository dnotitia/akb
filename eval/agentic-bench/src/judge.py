"""LLM judge — score each (arm, qid) run against the yaml ground truth.

Usage:
  python -m src.judge --arm all --query all --parallel 4
  python -m src.judge --aggregate    # re-aggregate from existing judge files
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
from pathlib import Path
from typing import Any

import yaml

from .llm_client import LLM, LLMError


ROOT = Path(__file__).resolve().parent.parent
EVALSET = ROOT / "evalset"
RUNS = Path(os.environ.get("RUNS_DIR", ROOT / "runs"))
_V4_STYLE = any(s in str(RUNS) for s in ("v4", "v5", "v6", "v7", "v8", "v9"))
ARMS = ["A1_search_only", "A2_grep_only", "A3_tree", "A4_all"] if _V4_STYLE else ["A1_search_only", "A2_grep", "A3_drill"]


JUDGE_SYSTEM = """You are an evaluator scoring an AI agent's answer against ground truth.

You receive:
- The original Korean question
- The agent's final answer (Korean)
- Ground truth: must_mention facts, forbidden phrases, source documents

Score the answer on a strict rubric and return ONLY a JSON object (no prose, no markdown fences) with this exact shape:

{
  "must_mention_matched": [list of must_mention items the answer correctly conveys],
  "must_mention_missing": [list of must_mention items the answer is missing],
  "forbidden_found": [list of forbidden phrases the answer wrongly contains],
  "faithfulness": "high" | "medium" | "low",
  "verdict": "PASS" | "PARTIAL" | "FAIL",
  "reason": "one-sentence Korean explanation"
}

Verdict rule:
- PASS = ALL must_mention covered AND zero forbidden AND faithfulness in {high, medium}
- PARTIAL = ≥half must_mention covered AND zero forbidden
- FAIL = otherwise (or if the answer admits the info couldn't be found)

Be strict — paraphrased synonyms count as a match only if the meaning is identical (e.g. "특정후견의 심판" ↔ "특정후견 심판" yes; "후견" alone — no, too vague)."""


def build_judge_user(question: str, answer: str, gt: dict[str, Any]) -> str:
    must = gt.get("must_mention", []) or []
    forb = gt.get("forbidden", []) or []
    srcs = gt.get("source_docs", []) or []
    return f"""Question:
{question}

Agent answer:
{answer if answer.strip() else "<empty>"}

Ground truth must_mention:
{json.dumps(must, ensure_ascii=False)}

Ground truth forbidden:
{json.dumps(forb, ensure_ascii=False)}

Source docs (for your reference):
{json.dumps(srcs, ensure_ascii=False)}

Score now. Return JSON only."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # Remove ```json ... ``` fencing.
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def _normalize(s: str) -> str:
    """Loose match for provenance: ignore whitespace + punctuation
    differences so "제 14조의2" matches "제14조의 2" in tool output."""
    return re.sub(r"[\s​ \.\,\(\)「」『』\"']+", "", s)


def _retrieved_split(must_mention: list[str], tool_result_texts: list[str]) -> tuple[list[str], list[str]]:
    """Return (retrieved, not_retrieved). A must_mention fact is
    `retrieved` if its normalized form appears as a substring in any
    tool result text. Otherwise the model produced it from prior /
    context — `not_retrieved`."""
    concat_norm = _normalize(" ".join(tool_result_texts))
    retrieved, not_retrieved = [], []
    for fact in must_mention:
        if _normalize(fact) in concat_norm:
            retrieved.append(fact)
        else:
            not_retrieved.append(fact)
    return retrieved, not_retrieved


async def judge_one(*, qid: str, arm: str, llm: LLM, force: bool) -> dict[str, Any]:
    summary_path = RUNS / arm / f"{qid}.summary.json"
    judge_path = RUNS / arm / f"{qid}.judge.json"
    if not summary_path.exists():
        return {"qid": qid, "arm": arm, "status": "NO_SUMMARY"}
    if judge_path.exists() and not force:
        d = json.loads(judge_path.read_text())
        return {"qid": qid, "arm": arm, "status": "SKIP", "verdict": d.get("verdict")}
    summary = json.loads(summary_path.read_text())
    qspec = yaml.safe_load((EVALSET / f"{qid}.yaml").read_text(encoding="utf-8"))

    user = build_judge_user(
        question=qspec["query"],
        answer=summary.get("final_answer_text", "") or "",
        gt=qspec.get("ground_truth") or {},
    )
    try:
        resp = await llm.chat(
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            max_tokens=1500,
            temperature=0.0,
        )
    except LLMError as e:
        return {"qid": qid, "arm": arm, "status": "JUDGE_ERROR", "error": str(e)}

    raw = resp["message"].get("content") or ""
    try:
        verdict_obj = json.loads(_strip_fences(raw))
    except Exception:
        return {"qid": qid, "arm": arm, "status": "JUDGE_PARSE_ERROR", "raw": raw[:500]}

    # Provenance: split must_mention into facts the answer
    # actually pulled from tool output vs. facts that came from
    # the model's prior (no tool result contains them).
    gt_must = (qspec.get("ground_truth") or {}).get("must_mention") or []
    tool_texts = [tc.get("result_text", "") for tc in summary.get("tool_calls_clean", [])]
    retrieved, not_retrieved = _retrieved_split(gt_must, tool_texts)
    provenance = {
        "retrieved": retrieved,
        "from_prior_or_missing": not_retrieved,
        "retrieved_rate": (len(retrieved) / len(gt_must)) if gt_must else None,
    }

    verdict_obj["_qid"] = qid
    verdict_obj["_arm"] = arm
    verdict_obj["_question"] = qspec["query"]
    verdict_obj["_answer"] = summary.get("final_answer_text", "")
    verdict_obj["_provenance"] = provenance
    verdict_obj["_summary_stats"] = {
        "tool_calls": len(summary.get("tool_calls_clean", [])),
        "tokens": summary.get("usage_total", {}).get("total_tokens", 0),
        "wall_seconds": summary.get("wall_seconds", 0),
        "iterations": summary.get("iterations", 0),
        "category": summary.get("category"),
        "abort_reason": summary.get("abort_reason"),
    }
    judge_path.write_text(json.dumps(verdict_obj, ensure_ascii=False, indent=2))
    return {"qid": qid, "arm": arm, "status": "OK", "verdict": verdict_obj.get("verdict")}


def list_query_ids() -> list[str]:
    return sorted(p.stem for p in EVALSET.glob("q*.yaml"))


async def judge_all_async(args: argparse.Namespace) -> None:
    llm = LLM(
        base_url=os.environ.get("JUDGE_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=os.environ.get("JUDGE_API_KEY") or os.environ["LLM_API_KEY"],
        model=os.environ.get("JUDGE_MODEL", "anthropic/claude-haiku-4-5"),
    )
    arms = ARMS if args.arm == "all" else [args.arm]
    qids = list_query_ids() if args.query == "all" else [args.query]
    pairs = [(qid, arm) for arm in arms for qid in qids]
    print(f"judging {len(pairs)} (arm × query) pairs, parallel={args.parallel}, model={llm.model}", flush=True)

    sem = asyncio.Semaphore(args.parallel)

    async def worker(qid: str, arm: str):
        async with sem:
            try:
                r = await judge_one(qid=qid, arm=arm, llm=llm, force=args.force)
            except Exception as e:
                r = {"qid": qid, "arm": arm, "status": "EXC", "error": f"{type(e).__name__}: {e}"}
            print(f"[{arm}] {qid} | {r['status']} | verdict={r.get('verdict', '-')}", flush=True)
            return r

    results = await asyncio.gather(*(worker(qid, arm) for qid, arm in pairs))
    return None


def _infer_verdict(d: dict[str, Any]) -> str:
    """Fallback when the judge model forgot to emit `verdict`.
    Apply the rubric to must_mention / forbidden / faithfulness."""
    v = d.get("verdict")
    if v in ("PASS", "PARTIAL", "FAIL"):
        return v
    matched = d.get("must_mention_matched") or []
    missing = d.get("must_mention_missing") or []
    forb = d.get("forbidden_found") or []
    faith = (d.get("faithfulness") or "").lower()
    total = len(matched) + len(missing)
    if forb:
        return "FAIL"
    if total == 0:
        return "FAIL"
    if not missing and faith in ("high", "medium"):
        return "PASS"
    if len(matched) * 2 >= total:
        return "PARTIAL"
    return "FAIL"


def aggregate() -> None:
    print("\n=== AGGREGATE ===\n")
    rows = []
    for arm in ARMS:
        adir = RUNS / arm
        if not adir.exists():
            continue
        per_q = []
        for jp in sorted(adir.glob("q*.judge.json")):
            d = json.loads(jp.read_text())
            d["verdict"] = _infer_verdict(d)
            per_q.append(d)
        rows.append((arm, per_q))

    print(f"{'arm':<18}{'n':<5}{'PASS':<6}{'PART':<6}{'FAIL':<6}{'pass%':<8}{'prov%':<8}{'tools/q':<10}{'tok/q':<10}{'wall/q':<9}{'tok/pass':<10}")
    metrics = {}
    for arm, per_q in rows:
        n = len(per_q)
        p = sum(1 for d in per_q if d.get("verdict") == "PASS")
        pa = sum(1 for d in per_q if d.get("verdict") == "PARTIAL")
        f = sum(1 for d in per_q if d.get("verdict") == "FAIL")
        tools = [d["_summary_stats"]["tool_calls"] for d in per_q]
        toks = [d["_summary_stats"]["tokens"] for d in per_q]
        walls = [d["_summary_stats"]["wall_seconds"] for d in per_q]
        # Provenance rate = mean fraction of must_mention facts
        # actually pulled from tool output (vs. model prior).
        prov_rates = [
            d.get("_provenance", {}).get("retrieved_rate")
            for d in per_q
            if d.get("_provenance", {}).get("retrieved_rate") is not None
        ]
        prov_pct = 100 * statistics.mean(prov_rates) if prov_rates else 0
        tokens_per_pass = (sum(toks) / p) if p else float("inf")
        pct = 100 * p / n if n else 0
        print(f"{arm:<18}{n:<5}{p:<6}{pa:<6}{f:<6}{pct:<8.1f}{prov_pct:<8.1f}{statistics.mean(tools):<10.2f}{statistics.mean(toks):<10.0f}{statistics.mean(walls):<9.1f}{tokens_per_pass:<10.0f}")
        metrics[arm] = {
            "n": n, "pass": p, "partial": pa, "fail": f,
            "pass_pct": pct,
            "provenance_pct": prov_pct,
            "mean_tools": statistics.mean(tools),
            "mean_tokens": statistics.mean(toks),
            "mean_wall_s": statistics.mean(walls),
            "tokens_per_pass": tokens_per_pass if p else None,
        }

    # Per-category breakdown.
    print("\n--- per category ---")
    cats: dict[str, dict[str, list[int]]] = {}
    for arm, per_q in rows:
        for d in per_q:
            c = d["_summary_stats"]["category"] or "?"
            cats.setdefault(c, {}).setdefault(arm, []).append(1 if d.get("verdict") == "PASS" else 0)
    for c in sorted(cats):
        print(f"  {c}:")
        for arm in ARMS:
            v = cats[c].get(arm, [])
            if v:
                print(f"    {arm}: {sum(v)}/{len(v)} PASS")

    # Save metrics.
    (RUNS / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"\nsaved {RUNS / 'metrics.json'}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--arm", default="all")
    p.add_argument("--query", default="all")
    p.add_argument("--parallel", type=int, default=4)
    p.add_argument("--force", action="store_true")
    p.add_argument("--aggregate", action="store_true")
    args = p.parse_args()
    if not args.aggregate:
        asyncio.run(judge_all_async(args))
    aggregate()


if __name__ == "__main__":
    main()
