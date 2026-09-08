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
# `evalset/` is the private Korean-law set the harness shipped against;
# `EVALSET_DIR` points the same code at another one — e.g. the tracked
# `evalset-project-akb/` seed questions.
EVALSET = Path(os.environ.get("EVALSET_DIR", ROOT / "evalset"))
RUNS = Path(os.environ.get("RUNS_DIR", ROOT / "runs"))

# The accuracy the batch is gated on. A payload change that makes answers
# cheaper is only worth having if the answers are still right, so the gate
# is stated up front rather than read off the table afterwards.
DEFAULT_ACCURACY_FLOOR = 0.95
# Reported alongside the configured floor so a near miss can be read against
# the two thresholds a reviewer is likely to ask about next.
TRADEOFF_FLOORS = (0.90, 0.80)
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


# ── gate + payload arithmetic ────────────────────────────────────────
#
# All pure, all unit-tested. The bench exists to answer one question — is a
# cheaper response still a correct one — and that answer is arithmetic over
# two measured numbers (accuracy, payload) and two the operator supplies
# (what a wrong answer costs to redirect, what the change saves per call).


def gate_verdict(pass_rate: float, floor: float) -> str:
    """PASS when measured accuracy is at or above the floor.

    `pass_rate` and `floor` are both fractions in [0, 1]. Equality passes: a
    floor of 0.95 means "95 % is acceptable", not "better than 95 %".
    """
    return "PASS" if pass_rate >= floor else "FAIL"


def tokens_per_correct_answer(total_tokens: float, passes: int) -> float:
    """Tokens spent per answer that was actually right.

    Mean tokens per question flatters an arm that answers cheaply and wrongly;
    this divides the same spend by the answers that survived the rubric. An
    arm with no passing answer has no finite cost per correct answer.
    """
    if passes <= 0:
        return float("inf")
    return total_tokens / passes


def payload_per_call(result_chars: list[int], questions: int) -> dict[str, float]:
    """Aggregate the per-tool-call response sizes an agent had to read.

    Characters, not tokens: the runner stores what the tool returned, and the
    token count depends on a tokenizer the harness deliberately does not pin.
    """
    calls = len(result_chars)
    total = float(sum(result_chars))
    return {
        "calls": calls,
        "total_chars": total,
        "chars_per_call": (total / calls) if calls else 0.0,
        "chars_per_question": (total / questions) if questions else 0.0,
    }


def break_even_accuracy(*, redirect_cost: float, saving: float) -> float | None:
    """The accuracy at which a payload saving exactly pays for the redirects
    it costs: `(1 - p) * D == saving`, so `p = 1 - saving / D`.

    This is the number the tradeoff actually turns on. Below it the expected
    cost of re-asking outweighs what was saved per question; above it the
    saving wins. `None` when no redirect cost was supplied — without `D` there
    is nothing to trade the saving against.
    """
    if redirect_cost <= 0:
        return None
    return 1.0 - (saving / redirect_cost)


def tradeoff_row(
    *,
    pass_rate: float,
    floor: float,
    redirect_cost: float,
    saving_per_call: float,
    calls_per_question: float,
) -> dict[str, Any]:
    """One row of the accuracy/cost readout at a single floor.

    `expected_redirect_tokens` is `(1 - p) * D`: the share of questions the
    agent gets wrong, times what it costs to redirect one of them.
    `saving_tokens` is the payload saving over the calls one question
    actually makes. `net_tokens` is what is left — positive means the change
    pays for the redirects it is expected to cause.
    """
    saving = saving_per_call * calls_per_question
    expected_redirect = (1.0 - pass_rate) * redirect_cost
    return {
        "floor": floor,
        "pass_rate": pass_rate,
        "verdict": gate_verdict(pass_rate, floor),
        "expected_redirect_tokens": expected_redirect,
        "saving_tokens": saving,
        "net_tokens": saving - expected_redirect,
        "break_even_accuracy": break_even_accuracy(
            redirect_cost=redirect_cost, saving=saving
        ),
    }


def arm_result_chars(arm: str, qids: list[str]) -> list[int]:
    """Per-call response sizes for one arm, read from the run summaries.

    The judge file carries the *count* of tool calls; the sizes live in the
    run summary's `tool_calls_clean`, which is where the runner already wrote
    `result_chars`. Reading them here means `--aggregate` reports payload for
    runs that were judged before this readout existed.
    """
    chars: list[int] = []
    for qid in qids:
        summary_path = RUNS / arm / f"{qid}.summary.json"
        if not summary_path.exists():
            continue
        try:
            summary = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for call in summary.get("tool_calls_clean") or []:
            size = call.get("result_chars")
            if isinstance(size, int):
                chars.append(size)
            elif isinstance(call.get("result_text"), str):
                chars.append(len(call["result_text"]))
    return chars


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


def aggregate(
    *,
    accuracy_floor: float = DEFAULT_ACCURACY_FLOOR,
    redirect_cost: float = 0.0,
    saving_per_call: float = 0.0,
) -> dict[str, Any]:
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
            d["_qid"] = d.get("_qid") or jp.name.split(".")[0]
            per_q.append(d)
        rows.append((arm, per_q))

    print(f"{'arm':<18}{'n':<5}{'PASS':<6}{'PART':<6}{'FAIL':<6}{'pass%':<8}{'prov%':<8}{'tools/q':<10}{'tok/q':<10}{'wall/q':<9}{'tok/correct':<13}{'gate':<6}")
    metrics: dict[str, Any] = {}
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
        tok_per_correct = tokens_per_correct_answer(sum(toks), p)
        pass_rate = (p / n) if n else 0.0
        pct = 100 * pass_rate
        verdict = gate_verdict(pass_rate, accuracy_floor)
        payload = payload_per_call(
            arm_result_chars(arm, [d["_qid"] for d in per_q]), n
        )
        print(f"{arm:<18}{n:<5}{p:<6}{pa:<6}{f:<6}{pct:<8.1f}{prov_pct:<8.1f}{statistics.mean(tools):<10.2f}{statistics.mean(toks):<10.0f}{statistics.mean(walls):<9.1f}{tok_per_correct:<13.0f}{verdict:<6}")
        metrics[arm] = {
            "n": n, "pass": p, "partial": pa, "fail": f,
            "pass_pct": pct,
            "pass_rate": pass_rate,
            "provenance_pct": prov_pct,
            "mean_tools": statistics.mean(tools),
            "mean_tokens": statistics.mean(toks),
            "mean_wall_s": statistics.mean(walls),
            # Kept under its original name for anything already reading it.
            "tokens_per_pass": tok_per_correct if p else None,
            "tokens_per_correct_answer": tok_per_correct if p else None,
            "payload": payload,
            "accuracy_floor": accuracy_floor,
            "gate": verdict,
        }

    # Accuracy floor + the cost tradeoff behind it.
    #
    # A payload change is worth having when what it saves per question is
    # more than what the answers it breaks cost to redirect. `D` and the
    # per-call saving are operator inputs — the harness does not know what a
    # re-prompt costs in this deployment, and refuses to invent it.
    print(
        f"\n--- accuracy floor {accuracy_floor:.2f} "
        f"(D={redirect_cost:.0f} tok/redirect, saving={saving_per_call:.0f} tok/call) ---"
    )
    if redirect_cost <= 0 and saving_per_call <= 0:
        print("  no cost inputs given — pass/fail only "
              "(pass --redirect-cost and --saving-per-call for the tradeoff)")
    print(f"  {'arm':<18}{'floor':<8}{'p':<8}{'verdict':<9}{'(1-p)xD':<12}{'saving/q':<12}{'net/q':<12}{'break-even p':<13}")
    floors = [accuracy_floor, *TRADEOFF_FLOORS]
    for arm, _ in rows:
        m = metrics[arm]
        arm_rows = []
        for floor in floors:
            row = tradeoff_row(
                pass_rate=m["pass_rate"],
                floor=floor,
                redirect_cost=redirect_cost,
                saving_per_call=saving_per_call,
                calls_per_question=m["mean_tools"],
            )
            arm_rows.append(row)
            break_even = row["break_even_accuracy"]
            break_even_text = "-" if break_even is None else f"{break_even:.3f}"
            print(
                f"  {arm:<18}{floor:<8.2f}{row['pass_rate']:<8.3f}{row['verdict']:<9}"
                f"{row['expected_redirect_tokens']:<12.0f}{row['saving_tokens']:<12.0f}"
                f"{row['net_tokens']:<12.0f}{break_even_text:<13}"
            )
        metrics[arm]["tradeoff"] = arm_rows

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

    # Payload per call — what the agent had to read to get there.
    print("\n--- payload per call ---")
    print(f"  {'arm':<18}{'calls':<8}{'chars/call':<13}{'chars/q':<12}")
    for arm, _ in rows:
        pay = metrics[arm]["payload"]
        print(
            f"  {arm:<18}{pay['calls']:<8}{pay['chars_per_call']:<13.0f}"
            f"{pay['chars_per_question']:<12.0f}"
        )

    # Save metrics.
    (RUNS / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"\nsaved {RUNS / 'metrics.json'}")
    return metrics


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--arm", default="all")
    p.add_argument("--query", default="all")
    p.add_argument("--parallel", type=int, default=4)
    p.add_argument("--force", action="store_true")
    p.add_argument("--aggregate", action="store_true")
    p.add_argument(
        "--accuracy-floor",
        type=float,
        default=DEFAULT_ACCURACY_FLOOR,
        help=(
            "Accuracy the batch is gated on, as a fraction "
            f"(default {DEFAULT_ACCURACY_FLOOR}). An arm below it is FAIL."
        ),
    )
    p.add_argument(
        "--redirect-cost",
        type=float,
        default=0.0,
        help=(
            "D: tokens it costs to redirect one wrong answer (re-prompt plus "
            "the retry it triggers). Used for the (1-p)xD readout."
        ),
    )
    p.add_argument(
        "--saving-per-call",
        type=float,
        default=0.0,
        help=(
            "Tokens saved per tool call by the change under test. Scaled by "
            "the measured calls per question to compare against (1-p)xD."
        ),
    )
    args = p.parse_args()
    if not (0.0 <= args.accuracy_floor <= 1.0):
        p.error("--accuracy-floor is a fraction in [0, 1]")
    if not args.aggregate:
        asyncio.run(judge_all_async(args))
    aggregate(
        accuracy_floor=args.accuracy_floor,
        redirect_cost=args.redirect_cost,
        saving_per_call=args.saving_per_call,
    )


if __name__ == "__main__":
    main()
