# AKB Agentic Search Bench

A small, opinionated harness for measuring how well an LLM agent can
actually answer questions over an AKB vault — not just whether a
single retriever returns the right chunk, but whether the whole
discovery → narrow → fetch loop holds up over a real corpus.

The runner is corpus-agnostic. The evalset and raw results that
shipped with the original Korean-law experiments are kept in a
separate private repo (this repo only publishes the harness).

## What it measures

Four arms, each exposing a different subset of AKB's search tools to
the same ReAct loop. `akb_get` is given to every arm so they're all
allowed to read documents — the variable is *how they find the
right one*.

| Arm | Tools | Paradigm |
|---|---|---|
| `A1_search_only` | `akb_search` + `akb_get` | Hybrid retrieval (dense + BM25) |
| `A2_grep_only` | `akb_grep` + `akb_get` | Literal / regex match |
| `A3_tree` | `akb_list_vaults` + `akb_browse` + `akb_drill_down` + `akb_get` | Tree routing — discover → navigate → drill |
| `A4_all` | Union of A1 + A2 + A3 | Full toolbox |

For each (arm, query) pair the runner:

1. Opens one MCP session for the chunk (multi-process isolation; one
   session reused per process — avoids the anyio TaskGroup race that
   per-call session open/close used to trigger).
2. Runs a ReAct loop with a per-arm system prompt that only lists
   the tools that arm has, with a wall budget, a max iteration
   count, and a duplicate-call guard (3 identical tool calls in a
   row → force final answer).
3. Stores the answer, tool-call trace, token usage, timing, and
   abort reason to `runs_<version>/<arm>/<qid>.summary.json`.

The judge is a separate step — a Claude sub-agent reasons over the
ground-truth `must_mention` / `forbidden` / `faithfulness` rules
without using substring matchers (those under-credit paraphrase).
No external commercial LLM API; the harness assumes Claude
subscription via sub-agent and OpenRouter-backed cheap models for
the agent itself.

## Layout

```
src/
  llm_client.py        OpenAI-compat chat client with 429/5xx exponential backoff
  mcp_client.py        Streamable HTTP MCP wrapper (per-process session reuse)
  react_agent.py       Per-arm ARM_TOOLS / ARM_HINTS + ReAct loop
  runner.py            Chunk runner: 1 session, sequential queries, auto-reconnect
  prep_judge_v3.py     Pre/post processing for the offline judge step
  judge.py             Aggregator: per-arm pass%, provenance%, per-category breakdown
scripts/
  run_v4_multiproc.sh  Multi-process driver — N processes × M queries each
```

## How to run

The runner reads everything from environment variables so the same
script targets dev, staging, and prod. The harness is corpus-
agnostic — point it at any AKB instance and supply your own evalset.

```bash
# 1) Author an evalset/q*.yaml per question. Schema:
#    id, category, query, ground_truth: {must_mention, forbidden, source_docs}
# 2) Smoke-test a single (arm, query) pair to confirm the agent is
#    actually using the tools you expect:
RUNS_DIR=runs_smoke \
AKB_MCP_URL=https://... AKB_PAT=... \
LLM_API_KEY=... LLM_MODEL=qwen/qwen3.6-plus \
python -m src.runner --arm A3_tree --query q001
# 3) Full run — one process per (arm, chunk):
bash scripts/run_v4_multiproc.sh
# 4) Hand off to a Claude sub-agent for verdicts:
python -m src.prep_judge_v3 prep
# (sub-agent writes runs_*/verdicts/verdicts_batch_*.json)
python -m src.prep_judge_v3 finalize
RUNS_DIR=runs_v1 python -m src.judge --aggregate
```

`RUNS_DIR` is required for any version other than the default
`runs/`. The aggregator emits `metrics.json` next to the raw runs.

## Required tools

The harness assumes an AKB backend at version **0.2.3 or later**,
because the four-arm tree-routing arm needs the slim + filterable
versions of `akb_list_vaults` / `akb_browse` and the `mode='outline'`
path on `akb_drill_down`. Earlier backends will run but the tree
arm will look much worse than it should — the early-version
benchmarks in our own history demonstrate exactly that failure mode.

## What we keep private

The Korean-law evalset, the per-run summaries, the per-query judge
verdicts, the v1–v8 result narratives, and the vault snapshots all
live in a sister repo. They aren't checked in here because:

- the ground-truth answers are domain-specific and were hand-
  authored — they aren't a public dataset;
- the raw model outputs include Korean text that's load-bearing for
  the analysis but not useful as an OSS artefact;
- the harness is the part that's reusable across corpora.

If you fork this for your own domain, you'll write your own
`evalset/`. The schema is intentionally tiny.

## License

PolyForm Noncommercial 1.0.0 — same as the parent `dnotitia/akb`
repository. See [LICENSE](../../LICENSE).
