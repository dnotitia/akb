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
The agent calls an OpenAI-compatible model endpoint (OpenRouter by default);
provider usage may incur charges. The judge workflow is a separate manual or
agent-assisted step and requires access to the source corpus.

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
evalset-synthetic/    Three questions over the public fictional Orchard corpus
corpus-synthetic/     Source documents for those questions (no private vault needed)
tests/                 Unit tests for the gate/tradeoff arithmetic and the
                       seed evalset schema
```

## How to run

The runner reads everything from environment variables so the same
script targets dev, staging, and prod. The harness is corpus-
agnostic — point it at any AKB instance and supply your own evalset.

Start a disposable AKB instance using the [Native quickstart](../../README.md#quick-start),
create a user and PAT, then import the public fixture documents through your MCP
client. The following are tool calls (the `content` value means the complete text
of the named local file, not its path):

1. `akb_create_vault(name="bench-synthetic", description="Fictional Orchard benchmark fixtures", public_access="none")`.
2. For each Markdown file in [`corpus-synthetic/`](corpus-synthetic/), call
   `akb_put(parent="akb://bench-synthetic/coll/fixtures", slug="<filename without .md>", title="<document heading>", content="<full file text>")`.
3. Confirm the returned URIs match `ground_truth.source_docs` in
   [`evalset-synthetic/`](evalset-synthetic/). Follow any vault-skill acknowledgement
   returned by the server, then retry the same write. Wait for indexing to finish
   before comparing search arms.

Use a fresh instance if that vault name already belongs to another experiment.
These fictional contracts are test data, not descriptions of AKB behavior.

From `eval/agentic-bench/`, with Python dependencies from the backend development
setup installed and its virtual environment activated:

```bash
export EVALSET_DIR="$PWD/evalset-synthetic"
export RUNS_DIR="$PWD/runs_v4_smoke"
export AKB_MCP_URL=http://localhost:8000/mcp/
# Set AKB_PAT and LLM_API_KEY privately; never commit them.
export LLM_MODEL=qwen/qwen3.6-plus
python -m src.runner --arm A3_tree --query q001
# All four arms over the three public questions:
python -m src.runner --arm all --query all --parallel 1
# Separate judge workflow; the judge must inspect these synthetic sources:
python -m src.prep_judge_v3 prep
# Review batches/*.json against source text; write verdicts as described below.
python -m src.prep_judge_v3 finalize
python -m src.judge --aggregate
```

For each batch, write a JSON array to
`$RUNS_DIR/verdicts/verdicts_batch_00.json` (increment the suffix per batch).
Each entry needs `qid`, `arm`, `matched` (list of supported required facts),
`missing` (list of missing required facts), `forbidden_found` (list),
`faithfulness` (`high`, `medium`, or `low`), `verdict` (`PASS` or `FAIL`), and
`reason`. Judge each answer against both the question and source document;
`PASS` requires all required facts, no forbidden claims, and faithful support.

Keep `EVALSET_DIR` and `RUNS_DIR` exported for every stage. The `v4` in the
run directory name selects the four-arm layout in the historical judge prep;
use that prefix with this workflow. All stages use
`src/paths.py`; omitting `EVALSET_DIR` selects the untracked `evalset/` directory.
The aggregator emits `metrics.json` next to the raw runs. The historical
`scripts/run_v4_multiproc.sh` assumes 100 questions (`q001`–`q100`); it is not
the driver for this three-question fixture. These fixtures test harness setup
and schema compatibility, not statistical retrieval quality.

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

## Public fixtures and historical experiments

The original domain-specific experiments used a separate corpus that is not
included here. Their aggregate observations above are historical context, not
results reproduced by the three-question public example.

The checked-in `corpus-synthetic/` and `evalset-synthetic/` provide all source
text and ground truth needed for a small independent run. No internal document
or private vault is required. To evaluate another domain, supply your own
`EVALSET_DIR` with `id`, `category`, `query`, and `ground_truth` containing
`must_mention`, `forbidden`, and canonical `source_docs` URIs. Import the matching
corpus into your own instance and record its revision alongside the results.

## License

Business Source License 1.1 — same as the parent `dnotitia/akb`
repository. See [LICENSE](../../LICENSE) and
[LICENSE-CHANGE.md](../../LICENSE-CHANGE.md).
