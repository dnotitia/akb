"""ReAct loop for the agentic-bench.

One agent run per (query, arm). Loops:
  - LLM call with current messages + arm-filtered tools
  - If tool_calls in response: execute each via MCP, append observation
  - If final answer: stop and return
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .llm_client import LLM, LLMError
from .mcp_client import call_tool, list_tools_full, mcp_session


# Bench-relevant tool whitelist by arm. Tool names are full MCP names
# (e.g. "akb_search"), NOT the prefixed "akb__akb_search" used by some
# proxies — the backend MCP exposes them under bare names.
ARM_TOOLS: dict[str, set[str]] = {
    # v4: vault hint removed from system prompt. Each arm exposes a
    # disjoint search paradigm (hybrid / grep / tree-routing) plus
    # akb_get baseline. A4 is the union — measures whether combining
    # paradigms is synergy or distraction.
    "A1_search_only": {"akb_search", "akb_get"},
    "A2_grep_only":   {"akb_grep", "akb_get"},
    "A3_tree":        {"akb_list_vaults", "akb_browse", "akb_drill_down", "akb_get"},
    "A4_all":         {"akb_search", "akb_grep",
                       "akb_list_vaults", "akb_browse", "akb_drill_down", "akb_get"},
}


SYSTEM_PROMPT_BASE = """You are an AKB retrieval agent. AKB stores documents in vaults; each vault is organised by collections. Answer the user's question by retrieving relevant facts from AKB via the tools available to you.

Rules:
1. You are NOT told which vault has the answer. If your arm has a discovery tool, use it. Otherwise pass an explicit vault to search/grep — or omit `vault` to search across vaults you have access to.
2. Make the fewest tool calls that answer the question. Do NOT repeat the same call.
3. Tool results are truncated at 6KB — narrow your query rather than re-calling.
4. Quote exact text when possible. Do NOT invent facts.
5. If the answer isn't in AKB, say so honestly.
6. After at most 6 tool calls, produce a Korean final answer citing the source (vault, doc, section).
"""

ARM_HINTS: dict[str, str] = {
    "A1_search_only": """
Available tools: `akb_search`, `akb_get`.
- `akb_search(query=..., limit=N)` — hybrid dense + BM25 across all vaults you can access. Omit `vault` to search the entire corpus.
- Results include `uri` and chunk content. Use `akb_get(uri=...)` for full document body when chunk preview is insufficient.
""",
    "A2_grep_only": """
Available tools: `akb_grep`, `akb_get`.
- `akb_grep(pattern=..., files_with_matches=true, limit=10)` — exact-string match across vaults. Omit `vault` for cross-corpus grep.
- Use `akb_get(uri=...)` for the full document body once you have a uri from grep.
- You have no semantic search — work from exact strings only.
""",
    "A3_tree": """
Available tools: `akb_list_vaults`, `akb_browse`, `akb_drill_down`, `akb_get`.
- `akb_list_vaults(filter="키워드")` — discover vaults filtered by name/description substring. 도메인 키워드 (예: filter="법령") 로 좁혀 호출. 인자 없이 호출하면 응답이 잘릴 수 있음.
- `akb_browse(vault=..., filter="키워드", limit=10)` — list collections / docs. filter 인자로 collection name substring 매칭 권장 (e.g. filter="민법"). depth=1 만 인자 없이 호출하면 70+ collection 잘림. 응답에 `total`, `returned` 포함.
- `akb_drill_down(uri=..., section="제N조", pattern="키워드")` — extract section + 그 안 substring grep (pattern 인자). section 이 안 맞아도 응답에 `outline` (available headings list) 자동 포함 — 그 heading 중 하나로 retry. 큰 section (예: 부칙) 안에서 fine-grained query 는 pattern 활용.
- `akb_get(uri=...)` — fetch full doc. drill_down outline 도 막혔을 때.
- Typical flow: list_vaults(filter="도메인") → browse(vault, filter="법령명") → drill_down(uri, section=..., pattern=...).
""",
    "A4_all": """
Available tools: `akb_search`, `akb_grep`, `akb_list_vaults`, `akb_browse`, `akb_drill_down`, `akb_get`.
- You have every search paradigm. Pick the best tool per question:
  - 의미 검색: `akb_search`.
  - 조항·약칭 정확 매칭: `akb_grep`.
  - vault·structure 발견: `akb_list_vaults(filter="키워드")` → `akb_browse(vault=..., filter="법령명")` (둘 다 filter 인자로 좁혀 호출 권장).
  - 깊은 §본문: `akb_drill_down(uri, section=..., pattern="키워드")` — pattern 으로 section 안 substring grep. section 못 찾으면 응답의 `outline` 으로 retry.
  - 전체 doc: `akb_get`.
""",
}


def build_system_prompt(arm: str) -> str:
    return SYSTEM_PROMPT_BASE + ARM_HINTS.get(arm, "")


@dataclass
class Timing:
    start_wall: float
    first_tool_call_s: float | None = None
    last_tool_response_s: float | None = None
    done_s: float | None = None


@dataclass
class AgentResult:
    query_id: str
    arm: str
    final_answer_text: str
    tool_calls_clean: list[dict[str, Any]]
    messages_count: int
    iterations: int
    timing: dict[str, float | None]
    usage_total: dict[str, int]
    finish_reason: str | None
    abort_reason: str | None = None
    error: str | None = None


async def run_agent_with_session(
    *,
    session,
    query_id: str,
    arm: str,
    query: str,
    llm: LLM,
    max_iterations: int = 8,
    max_wall_s: float = 240.0,
    result_text_cap: int = 2000,
) -> AgentResult:
    """Run one (qid, arm) using a caller-provided MCP session.

    Splitting session ownership from the agent loop lets a runner reuse
    a single session across many queries, which avoids the per-query
    open/close cycle that triggered anyio TaskGroup cancellation races
    in v2/v4.
    """
    if arm not in ARM_TOOLS:
        raise ValueError(f"unknown arm: {arm}")

    timing = Timing(start_wall=time.time())
    tool_calls_clean: list[dict[str, Any]] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    final_answer = ""
    finish_reason = None
    error = None
    iterations = 0
    abort_reason: str | None = None
    recent_call_sigs: list[tuple[str, str]] = []

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(arm)},
        {"role": "user", "content": query},
    ]

    try:
        all_tools = await list_tools_full(session)
        allowed = ARM_TOOLS[arm]
        arm_tools = [t for t in all_tools if t["function"]["name"] in allowed]
        if not arm_tools:
            raise RuntimeError(
                f"arm {arm} has no tools available from MCP server. "
                f"Server tools: {[t['function']['name'] for t in all_tools][:30]}"
            )

        for it in range(max_iterations):
            iterations = it + 1
            if time.time() - timing.start_wall > max_wall_s:
                abort_reason = "wall_timeout"
                break
            resp = await llm.chat(messages, tools=arm_tools, tool_choice="auto")
            msg = resp["message"]
            u = resp["usage"]
            usage_total["prompt_tokens"] += u.get("prompt_tokens", 0)
            usage_total["completion_tokens"] += u.get("completion_tokens", 0)
            usage_total["total_tokens"] += u.get("total_tokens", 0)
            finish_reason = resp["finish_reason"]

            # Append assistant message (must include tool_calls for the
            # next turn to be valid OpenAI-style).
            tool_calls = msg.get("tool_calls") or []
            assistant_entry: dict[str, Any] = {
                "role": "assistant",
                "content": msg.get("content") or "",
            }
            if tool_calls:
                assistant_entry["tool_calls"] = tool_calls
            messages.append(assistant_entry)

            if not tool_calls:
                # Final answer.
                final_answer = msg.get("content") or ""
                break

            # Execute each tool call.
            if timing.first_tool_call_s is None:
                timing.first_tool_call_s = time.time() - timing.start_wall

            for tc in tool_calls:
                name = tc["function"]["name"]
                raw_args = tc["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    args = {"_raw": raw_args}

                if name not in allowed:
                    observation = f"<error> tool '{name}' is not allowed in arm {arm}. Allowed: {sorted(allowed)}"
                    is_error = True
                else:
                    is_error, observation = await call_tool(session, name, args)

                # Provenance: store a capped slice of the actual
                # tool result so the judge can verify whether the
                # final answer's facts actually came from this
                # call vs. were synthesised by the model.
                tool_calls_clean.append({
                    "iteration": iterations,
                    "name": name,
                    "args": args,
                    "is_error": is_error,
                    "result_chars": len(observation),
                    "result_text": observation[:result_text_cap],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": observation,
                })

                # Loop detection: 3 identical (name, args) in a row.
                sig = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                recent_call_sigs.append(sig)
                if len(recent_call_sigs) >= 3 and len(set(recent_call_sigs[-3:])) == 1:
                    abort_reason = "dup_call_loop"
                    break

            if abort_reason:
                break

            timing.last_tool_response_s = time.time() - timing.start_wall

        # Force a final-answer pass (tools disabled) if we never
        # got one — covers: max_iterations exhausted, wall_timeout,
        # dup_call_loop, AND the v1 failure mode where the model's
        # very first turn was a tool call that the arm guard
        # rejected (q024/q027/q028: 1 tool call, empty answer).
        if not final_answer:
            hint = {
                "wall_timeout": "Wall budget hit. Give your final answer now from what you've gathered.",
                "dup_call_loop": "You are repeating the same tool call. Stop and give your final answer now from what you already have.",
            }.get(abort_reason or "", "Tool budget exhausted. Give your best final answer now based on what you've already retrieved.")
            messages.append({"role": "user", "content": hint})
            resp = await llm.chat(messages, tools=None)
            msg = resp["message"]
            u = resp["usage"]
            usage_total["prompt_tokens"] += u.get("prompt_tokens", 0)
            usage_total["completion_tokens"] += u.get("completion_tokens", 0)
            usage_total["total_tokens"] += u.get("total_tokens", 0)
            final_answer = msg.get("content") or ""
            finish_reason = resp["finish_reason"]

    except LLMError as e:
        error = f"LLMError: {e}"
    except BaseExceptionGroup as eg:
        # asyncio TaskGroup wraps the actual cause — surface it so we
        # can diagnose. v1+v2 both saw bare "ExceptionGroup: unhandled
        # errors in a TaskGroup (1 sub-exception)" which is useless.
        subs = [f"{type(e).__name__}: {e}" for e in eg.exceptions]
        error = f"ExceptionGroup: {'; '.join(subs)[:300]}"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    timing.done_s = time.time() - timing.start_wall
    return AgentResult(
        query_id=query_id,
        arm=arm,
        final_answer_text=final_answer,
        tool_calls_clean=tool_calls_clean,
        messages_count=len(messages),
        iterations=iterations,
        timing={
            "start_wall": timing.start_wall,
            "first_tool_call_s": timing.first_tool_call_s,
            "last_tool_response_s": timing.last_tool_response_s,
            "done_s": timing.done_s,
        },
        usage_total=usage_total,
        finish_reason=finish_reason,
        abort_reason=abort_reason,
        error=error,
    )


async def run_agent(
    *,
    query_id: str,
    arm: str,
    query: str,
    llm: LLM,
    mcp_url: str,
    mcp_pat: str,
    **kwargs: Any,
) -> AgentResult:
    """Backward-compat wrapper — opens its own MCP session per call.

    This is the v2/v4 pattern. Per-call session open/close stresses
    `mcp.client.streamable_http`'s anyio TaskGroup cleanup and was the
    source of intermittent ExceptionGroup races. Prefer the chunk
    runner that calls `run_agent_with_session` against one long-lived
    session.
    """
    async with mcp_session(mcp_url, mcp_pat) as session:
        return await run_agent_with_session(
            session=session,
            query_id=query_id,
            arm=arm,
            query=query,
            llm=llm,
            **kwargs,
        )
