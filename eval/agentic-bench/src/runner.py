"""Runner — iterate (arm × query) and save summary.json per run.

Usage:
  python -m src.runner --arm A2_grep --query q001
  python -m src.runner --arm all --query all --parallel 4
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from .llm_client import LLM
from .mcp_client import mcp_session
from .react_agent import ARM_TOOLS, run_agent_with_session


ROOT = Path(__file__).resolve().parent.parent
EVALSET = ROOT / "evalset"
RUNS = Path(os.environ.get("RUNS_DIR", ROOT / "runs"))


def load_query(qid: str) -> dict[str, Any]:
    p = EVALSET / f"{qid}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"{p} not found")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def list_queries() -> list[str]:
    qs = sorted(p.stem for p in EVALSET.glob("q*.yaml"))
    return qs


def list_arms() -> list[str]:
    return list(ARM_TOOLS.keys())


async def _run_one_using(*, session, qid: str, arm: str, llm: LLM, force: bool) -> dict[str, Any]:
    """Run one (qid, arm) using a caller-provided MCP session.
    Skips if summary.json already exists unless `force`."""
    run_dir = RUNS / arm
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / f"{qid}.summary.json"
    if out_path.exists() and not force:
        with out_path.open() as f:
            d = json.load(f)
        return {"qid": qid, "arm": arm, "status": "SKIP", "error": d.get("error")}

    q = load_query(qid)
    query_text = q["query"]
    started = time.time()
    result = await run_agent_with_session(
        session=session,
        query_id=qid,
        arm=arm,
        query=query_text,
        llm=llm,
    )
    elapsed = time.time() - started
    payload = {
        "query_id": qid,
        "arm": arm,
        "query": query_text,
        "category": q.get("category"),
        "ground_truth": q.get("ground_truth"),
        "final_answer_text": result.final_answer_text,
        "tool_calls_clean": result.tool_calls_clean,
        "iterations": result.iterations,
        "messages_count": result.messages_count,
        "timing": result.timing,
        "usage_total": result.usage_total,
        "finish_reason": result.finish_reason,
        "abort_reason": result.abort_reason,
        "error": result.error,
        "wall_seconds": elapsed,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    status = "OK" if (result.error is None and result.final_answer_text) else "FAIL"
    return {"qid": qid, "arm": arm, "status": status, "error": result.error, "tools": len(result.tool_calls_clean), "tokens": result.usage_total.get("total_tokens"), "wall": elapsed}


async def main_async(args: argparse.Namespace) -> int:
    # Environment / config.
    mcp_url = os.environ["AKB_MCP_URL"]
    mcp_pat = os.environ["AKB_PAT"]
    llm_base = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    llm_key = os.environ["LLM_API_KEY"]
    llm_model = os.environ.get("LLM_MODEL", "qwen/qwen3.5-35b-a3b")

    llm = LLM(base_url=llm_base, api_key=llm_key, model=llm_model)

    arms = list_arms() if args.arm == "all" else [args.arm]
    if args.qids:
        qids = [q.strip() for q in args.qids.split(",") if q.strip()]
    elif args.query == "all":
        qids = list_queries()
    else:
        qids = [args.query]
    pairs = [(qid, arm) for arm in arms for qid in qids]
    print(f"running {len(pairs)} (arm × query) pairs with auto-reconnect MCP session", flush=True)

    # Long-lived MCP session, but rebuild on httpx ConnectTimeout /
    # BaseExceptionGroup so a transient backend hiccup doesn't kill
    # the whole process. Process-level parallelism comes from the
    # multiproc shell spawning one runner per chunk.
    results = []
    pending = list(pairs)
    max_session_restarts = 10
    restarts = 0
    while pending:
        try:
            async with mcp_session(mcp_url, mcp_pat) as session:
                while pending:
                    qid, arm = pending[0]
                    try:
                        r = await _run_one_using(session=session, qid=qid, arm=arm, llm=llm, force=args.force)
                    except Exception as e:
                        # Per-query exception — record, but don't tear down
                        # the session (the next call_tool will detect a
                        # dead session itself).
                        r = {"qid": qid, "arm": arm, "status": "EXC", "error": f"{type(e).__name__}: {e}"}
                    pending.pop(0)
                    results.append(r)
                    print(
                        f"[{arm}] {qid} | {r['status']} | tools={r.get('tools', '-')} tokens={r.get('tokens', '-')} wall={r.get('wall', 0):.1f}s | err={r.get('error') or ''}",
                        flush=True,
                    )
        except (BaseExceptionGroup, Exception) as eg:
            # Session itself died (ConnectTimeout, anyio TaskGroup
            # cleanup, etc). Wait and rebuild a fresh session, then
            # resume from where we left off.
            restarts += 1
            if restarts > max_session_restarts:
                print(f"!! session restart budget exhausted ({restarts}). aborting with {len(pending)} pending.", flush=True)
                break
            backoff = min(30, 5 * restarts)
            print(f"!! session died ({type(eg).__name__}): backing off {backoff}s and rebuilding. pending={len(pending)}, restart={restarts}", flush=True)
            await asyncio.sleep(backoff)
    ok = sum(1 for r in results if r["status"] == "OK")
    skip = sum(1 for r in results if r["status"] == "SKIP")
    fail = sum(1 for r in results if r["status"] not in ("OK", "SKIP"))
    print(f"\nsummary: OK={ok} SKIP={skip} FAIL={fail}", flush=True)
    return 0 if fail == 0 else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--arm", default="all", help='arm name or "all"')
    p.add_argument("--query", default="all", help='query id (e.g. q001) or "all"')
    p.add_argument("--qids", default=None, help='comma-separated qids (overrides --query)')
    p.add_argument("--parallel", type=int, default=1, help='intra-process concurrency. Default 1 (no MCP session race).')
    p.add_argument("--force", action="store_true", help="re-run even if summary exists")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
