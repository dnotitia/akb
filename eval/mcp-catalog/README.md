# MCP tool catalog benchmark

This directory contains the source-blind AKB baseline/candidate catalog
benchmark. It compares the two arms with the same task corpus, resettable
fixture contract, model manifest, and statistical procedure. It does not use
the arm filtering or ReAct loop from `eval/agentic-bench`.

## Fixed execution contract

- MCP protocol revision: `2026-07-28`.
- Agent and MCP tools: PydanticAI `2.41.0` `Agent` and `MCPToolset`.
- MCP clients/transports: FastMCP Client `4.0.3` official Streamable HTTP and
  stdio transports.
- Evaluation: Pydantic Evals `2.41.0` `Dataset.evaluate`, `repeat`,
  `CaseLifecycle`, case evaluators, and report evaluators own repetition,
  lifecycle, scoring, and reporting.
- `MCPToolset` receives the server's complete `tools/list`; filtering, lazy
  loading, built-in coding tools, and automatic tool retries are disabled.
- Prompts in `corpus/tasks.json` do not name tools or MCP methods. The
  `operation_map` is scorer control-plane data outside the prompts.
- Fixture state probes deterministically check final state and destructive
  protection. A run without before/after observations cannot pass safety or
  task success.

The manifest registers OpenRouter, the DeepSeek primary model, the Qwen
lightweight model, two independent tasks per category, three repeats, paired
task-mean 95% confidence intervals, request/time/cost limits, and token
evidence. Repeats are averaged per task and are not counted as independent
tasks.

The models are fixed to `deepseek/deepseek-v4-flash-0731` and
`qwen/qwen3.8-27b`. Both requests use only the OpenRouter `parasail` upstream.
Request bodies force `allow_fallbacks=false`, `require_parameters=true`, and
the registered per-model `max_price`; no `models` fallback array is sent.
PydanticAI's inferred OpenAI strict-tool flag is disabled for the
OpenRouter/Parasail path. The complete 44-tool definition is retained.

Registered prices are input/output `$0.14/$0.28` and `$0.24/$2.20` per million
tokens. The total hard cost cap is `$50`. Each trial reserves `$0.10` before
starting, and the manifest's trial/smoke reservation total is `$36.40`, below
the hard cap. Input, output, and total tokens remain evidence and secondary
metrics; they are not an independent cumulative token gate. Both models use
`max_tokens=8,192` to allow the full catalog and terminal response.
`budget_used.wall_seconds` is cumulative elapsed wall-clock, not the sum of
parallel lane durations. Lane work is recorded separately as
`model_work_seconds`.

## Install and validate

```bash
uv sync --locked --extra dev --project eval/mcp-catalog
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench validate
```

Validate a ready runtime descriptor from a file:

```bash
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench validate \
  --descriptor /private/run/descriptor.json
```

The benchmark descriptor must be a ready schema-v2 descriptor produced by the
native benchmark runtime launcher below. The launcher composes child
descriptors produced by `scripts/ci/e2e_runtime.py serve`; a direct generic
runtime descriptor is not a four-cell benchmark descriptor. `--descriptor -`
reads the same JSON from stdin.

## Native benchmark runtime

The benchmark-specific four-cell launcher is the
`mcp-catalog-runtime` command. It composes the repository-owned
`scripts/ci/e2e_runtime.py serve` interface and starts four isolated cells:
`primary/lightweight × http/stdio`. Each cell has its own runtime root,
Compose project, ports, mutable fixture data, and process set. The launcher
prints one aggregated schema-v2 descriptor to stdout and keeps operational
logs in the private runtime root.

Start it with the registered fixture scenario and save its descriptor:

```bash
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-runtime serve \
  --scenario app-control-plane \
  --runtime-root /private/run/catalog-runtime \
  > /private/run/descriptor.json
```

The launcher passes `--profile transport-proxy` and
`--scenario app-control-plane` to each child. It assigns distinct app,
embedding, fixture, PostgreSQL, MinIO, and Compose project coordinates using
the registered port stride. It does not run model calls or warm the benchmark.
Stop the foreground launcher with SIGINT or SIGTERM; it terminates every cell,
and each child performs its normal runtime cleanup.

The aggregated descriptor retains the first cell's schema-v2 shape and adds a
`benchmark_cells` map containing all four child descriptors. Its evidence
contains the corresponding per-cell evidence and the benchmark provider
environment names. No credential value is written to the descriptor, argv,
logs, checkpoint, or artifact.

### Credential environment names

The benchmark provider values are read from:

```text
MCP_BENCH_OPENROUTER_BASE_URL
MCP_BENCH_OPENROUTER_API_KEY
```

The base URL must be exactly `https://openrouter.ai/api/v1`. The native runtime
launcher requires the fixture login values named by its descriptor, which are
`AKB_E2E_USERNAME` and `AKB_E2E_PASSWORD` by default. Those values let each
cell mint a fresh runtime PAT after reset. The descriptor advertises
`AKB_E2E_PAT` as the stdio PAT environment name; the actual PAT value remains
private to the child process. The manifest's `read_only` profile is
`MCP_BENCH_READ_ONLY_PAT`; a descriptor without isolated cells may use that
PAT directly, while the four-cell launcher mints a fresh read-scope PAT from
the runtime login values.

The provider values belong to the benchmark process, not the serving runtime.
The following checks fail before catalog or model calls when required values
are missing:

```bash
export MCP_BENCH_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
: "${MCP_BENCH_OPENROUTER_API_KEY:?set this value in the environment}"
: "${AKB_E2E_USERNAME:?required by the native runtime launcher}"
: "${AKB_E2E_PASSWORD:?required by the native runtime launcher}"
```

## Source revision

Runtime source identity uses `AKB_E2E_SOURCE_REVISION` when present. It must
be a lowercase 40-hex Git SHA. When absent, the runtime uses
`git rev-parse HEAD` from a real checkout. A checkout without `.git` must
receive the explicit variable; missing or invalid input fails before resource
creation as `blocked_runtime_config`, and `unknown` is never emitted.

## Baseline and candidate runs

Start the native launcher in one terminal, then run both arms against its
saved descriptor. Baseline and candidate must use the same corpus and
manifest, but may be run against separate candidate-bound descriptors when
the runtime is started separately. Corpus hash, protocol, repeats,
model/settings, and fixture reset contracts must remain identical.

```bash
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench run --arm baseline \
  --descriptor /private/run/descriptor.json \
  --output /private/run/baseline.json \
  --checkpoint /private/run/baseline.checkpoint.json

uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench run --arm candidate \
  --descriptor /private/run/descriptor.json \
  --output /private/run/candidate.json \
  --checkpoint /private/run/candidate.checkpoint.json

uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench compare \
  --baseline /private/run/baseline.json \
  --candidate /private/run/candidate.json \
  --output /private/run/comparison.json
```

The stdin descriptor form remains supported:

```bash
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench validate --descriptor -
```

Without a provider key or required runtime login/PAT input, `run` exits as
`needs_user_input` before catalog or model calls. Only environment variable
names are stored in descriptors. `default` uses the runtime's full-scope PAT;
`read_only` uses the registered `read` scope. With isolated cells, the login
path mints fresh credentials after every reset; the runner never reuses a
stale PAT when no mint path is available.

## Checkpoints and resume

With `--checkpoint`, each trial writes a redacted JSON checkpoint through an
fsynced temporary file followed by atomic replace. Resume with the same
checkpoint path:

```bash
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench run --arm baseline \
  --descriptor /private/run/descriptor.json \
  --output /private/run/baseline-resumed.json \
  --checkpoint /private/run/baseline.checkpoint.json \
  --resume /private/run/baseline.checkpoint.json
```

The exact source revision, run-manifest hash, task-corpus hash, arm, model,
transport, task, and repeat index must match. Only a completed trial with
valid provider usage/cost and routing evidence is reused. A measured
`success=false` trial caused by a request/output limit or tool/terminal action
failure is also reusable when usage/cost and both state observations exist.
Provider/infrastructure failures without evidence and incomplete trials are
rerun. A damaged, mismatched, or secret-bearing checkpoint fails closed before
the first provider call. Prior usage and cost remain in the checkpoint, so
resume cannot bypass the `$50` cap.

## Smoke and lifecycle gates

Before a paid full run, each arm executes the four smoke cells. Each smoke
cell sends a real request with the complete unfiltered toolset, performs at
least one successful MCP call, receives a follow-up terminal response, and
records positive usage plus Parasail routing evidence. Any pre-response
failure, zero usage, token limit, tool-call failure, or missing terminal
response blocks the full trial set. Smoke validates the minimal multi-turn
provider/tool contract; it is not token calibration. Passing smoke checkpoints
may be reused.

Each trial waits for both declared app/fixture health responses to report
`status=ready` and for the declared fixture scenario to match before state
observation or provider execution. Reset or PAT cleanup failure preserves the
primary failure stage, records completed trials and `budget_used` in a
redacted `status=incomplete` artifact, and exits non-zero. Incomplete artifacts
are not valid `compare` inputs.

Normal HTTP requests use a 30-second timeout. The repository runtime's
180-second reset/readiness budget is separate. Failures without model request,
usage/cost, or state evidence remain incomplete and are written to a failed
checkpoint; a zero-request arm cannot be a successful baseline. A failure
with provider usage/cost and both state observations remains a measured
unsuccessful trial and is charged, scored, and checkpointed.

Failure evidence distinguishes redacted exception chains, HTTP status, and
`failure_kind`: `provider`, `output_limit`, `request_limit`,
`terminal_response`, `tool`, or `budget`. 429 and missing usage/cost are
provider failures; hidden retries and fallbacks are disabled, and only resume
can retry them.

The four cells use separate runtime and Compose namespaces. PostgreSQL/MinIO
containers, Compose network/volumes, and backend/embedding/stdio process
identities remain stable across in-place reset. Reset clears only application
rows, MinIO objects, and Git fixture data, then reseeds and waits for
readiness. Adjacent trials do not run duplicate teardown/setup resets; the
next setup owns the single reset boundary. Reset count/time and identity
preservation are recorded in evidence.

Timing records every resume attempt, including cumulative
`end_to_end_wall_seconds` and provisioning, credential, catalog capture,
fixture reset, provider-wait, model execution, checkpoint, and other overhead.
429/rate-limit time remains provider failure evidence and is assigned to
`provider_wait`. Overlapping parallel intervals are partitioned once; timing
categories sum to the actual elapsed wall time. Aggregate lane work remains
`model_work_seconds` and is not the wall-time guard.

## Evidence and comparison

Each run artifact includes:

- exact runtime source revision, backend/proxy artifact versions, protocol,
  scenario, reset body, and capability profile;
- the actual unfiltered `tools/list` per transport and credential profile,
  tool count, canonical catalog hash, UTF-8 byte count, and four-token estimate;
- model class/id/version/settings, Pydantic Evals report, raw model arguments,
  server-facing arguments, tool results/errors, usage, latency, and cost;
- final response, fixture before/after state, and deterministic state checks;
- cumulative wall-clock, checkpoint new/reused/rerun counts, fixture reset
  count/time, dependency identity preservation, four-cell smoke results, and
  reproducible `artifact_hash_input`/`artifact_hash`.

Comparison averages repeats per task and computes the paired-difference
one-sided 95% lower bound. Passing requires zero safety regressions, success
lower bound at least `-3%p`, zero destructive/authorization regressions, at
least 50% catalog-token reduction, no first-action or argument-error
deterioration, and an overall token or latency improvement. Insufficient or
unpaired samples produce `inconclusive`.

## Developer checks

```bash
uv run --locked --project eval/mcp-catalog ruff check mcp_catalog tests
uv run --locked --project eval/mcp-catalog mypy --python-version 3.14 mcp_catalog
uv run --locked --project eval/mcp-catalog --extra dev --extra server pytest -q
```

Run the repository contributor gate from the repository root:

```bash
bash scripts/check.sh
```
