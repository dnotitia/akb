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
  judge.py             Aggregator: per-arm pass%, provenance%, accuracy gate,
                       tokens per correct answer, payload per call, tradeoff
  paths.py             EVALSET_DIR / RUNS_DIR — one resolution for all stages
scripts/
  run_v4_multiproc.sh  Multi-process driver — N processes × M queries each
evalset-project-akb/   Tracked seed questions over the `project-akb` vault —
                       the only worked examples of the schema in this repo
                       (`evalset/` itself stays private, see below)
tests/                 Unit tests for the gate/tradeoff arithmetic and the
                       seed evalset schema
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
`runs/`. `EVALSET_DIR` selects the question set (default `evalset/`; the
tracked seed set is `evalset-project-akb/`) and is read by the runner, the
judge prep and the aggregator through one helper (`src/paths.py`), so a
run cannot be produced against one question set and scored against
another. The aggregator emits `metrics.json` next to the raw runs.

## The accuracy gate and the cost tradeoff

A change that makes responses cheaper is only worth having if the answers
are still right, so the aggregator states the gate rather than leaving it
to be read off the table:

```bash
RUNS_DIR=runs_v1 python -m src.judge --aggregate \
  --accuracy-floor 0.95 \
  --redirect-cost 8000 \
  --saving-per-call 250
```

- `--accuracy-floor` (default **0.95**) — an arm below it is `FAIL`. The
  readout repeats each arm at 0.90 and 0.80 so a near miss can be read
  against the thresholds a reviewer asks about next.
- `--redirect-cost` **D** — tokens it costs to redirect one wrong answer
  (the re-prompt plus the retry it triggers). The expected cost per
  question is `(1 − p) × D`.
- `--saving-per-call` — tokens the change under test saves per tool call,
  scaled by the measured calls per question to compare like with like.
- `--fail-on-gate` — exit 1 when any arm is below the floor. Off by
  default, so reading a run never fails a shell; turn it on to use the
  bench as a gate.

The two cost options go together. Given one alone the aggregator refuses
the run rather than treating the other as zero, which would report a net
that always favours the change.

`net/q` is the saving less the expected redirect cost: positive means the
change pays for the answers it is expected to break. `break-even p` is the
accuracy at which the two are equal, `p* = 1 − saving / D` — the number
the decision actually turns on. Both are levels, not deltas: the harness
does not know how much accuracy a change cost, only what accuracy the run
measured, so a before/after comparison is still two runs.

The aggregate table also carries **tokens per correct answer** (spend
divided by answers that survived the rubric, not by questions asked) and a
**payload per call** section (mean response characters the agent had to
read, per call and per question). Both are the units a payload change
moves. The harness counts characters, not tokens: a tokenizer would pin a
model the bench does not otherwise depend on. Payload is read from the run
summaries, so `--aggregate` reports it for runs judged before this readout
existed; a run whose summaries carry no size at all is reported as
`unknown`, never as zero.

Neither cost input has a default. Without them the readout is pass/fail
only — what a redirect costs is a property of the deployment, and the
harness will not invent it.

## Required tools

The harness assumes an AKB backend at version **0.2.3 or later**,
because the four-arm tree-routing arm needs the slim + filterable
versions of `akb_list_vaults` / `akb_browse` and the `mode='outline'`
path on `akb_drill_down`. Earlier backends will run but the tree
arm will look much worse than it should — the early-version
benchmarks in our own history demonstrate exactly that failure mode.

## Findings from the seed run

The dataset we shipped this harness against was a 100-question
single-vault corpus with ≈ 5 700 documents, a deep section
hierarchy, and a long-form domain text style. Eight backend
iterations measured against the same evalset and agent model
(`qwen3.6-plus`), changing only the search-tool surface between
runs:

| Run | Backend change | A1 search | A2 grep | A3 tree | A4 all |
|---|---|---|---|---|---|
| Early | baseline | 73 % | 33 % | **4 %** | 63 % |
| Mid | `akb_list_vaults` slim + filter | 80 % | 46 % | 76 % | 86 % |
| Final | + `akb_browse` slim/filter, `akb_drill_down` pattern + outline | 78 % | 46 % | **81 %** | **84 %** |

What pulled the tree arm from 4 % to 81 % wasn't the agent — it
was the response shape. The early `akb_list_vaults` returned full
metadata for every vault, so on a tenant with 70+ vaults the
target one was silently truncated past the client's read window;
the agent then hallucinated answers from its prior. Switching to
slim `{name, description}` rows with an optional substring filter
made the target vault visible again. The same shape applied to
`akb_browse` recovered another 5pp. `akb_drill_down`'s `pattern`
arg + `outline` fallback closed the remaining sub-section
discovery gaps.

Other observations the runs surface:

- **`akb_get` is given to every arm.** The harness measures the
  marginal value of *how the agent finds the right document*, not
  whether the agent can read at all. With `akb_get` everywhere,
  the grep-only arm (A2) still tops out around 46 % — finding a
  URI doesn't help much if the agent then dumps the whole document
  into context and gets lost. Grep without drill-down is a trap.
- **A4 (full toolbox) wins by a small margin over A1**, and by a
  larger margin only on section-pinpoint and weak-token queries.
  Hybrid search alone is enough for the easy ⅔ of an evalset; the
  extra tools earn their keep on a long-tail of brittle queries.
- **Vault descriptions are part of retrieval.** A vault whose
  description doesn't mention its domain is invisible to an agent
  doing discovery — the obvious-in-hindsight rule that didn't make
  it into anyone's onboarding docs until the bench surfaced it.

The Korean-law evalset and per-run summaries that produced these
numbers live in the private sister repo.

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

`evalset-project-akb/` is the exception that is checked in: three seed
questions over the `project-akb` vault (the AKB family's own
design/decision record), written so this repository carries at least one
worked example of the schema. Point the harness at them with
`EVALSET_DIR=evalset-project-akb`. They cover a cross-repository seam
(the Product-API contract in `product/pipeline/modules/collector.md`), a
single decision with a client-visible consequence (`ADR-016`, write-lane
admission), and a two-sided module contract (`product/akb-oss/modules/backend.md`).
Each answer lives in one section of one document, so an arm that retrieves
the right document and stops has not answered the question.

## License

Business Source License 1.1 — same as the parent `dnotitia/akb`
repository. See [LICENSE](../../LICENSE) and
[LICENSE-CHANGE.md](../../LICENSE-CHANGE.md).
