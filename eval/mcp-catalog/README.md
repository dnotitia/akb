# MCP tool catalog benchmark

This source-blind benchmark compares the baseline and mixed MCP catalogs using
the same task contracts, resettable fixture, provider setup, and paired run
ledger. The benchmark evaluates behavior through logical operations and state
outcomes; task prompts do not name MCP tools.

## Registered contract

- The baseline exposes 45 HTTP and 51 stdio tools. The candidate exposes 33
  HTTP and 39 stdio tools. The public contract contains 45 HTTP/stdio logical
  operations plus six stdio-only file and image operations.
- The corpus has 40 Korean and English task variants across 20 semantic
  clusters. Eighteen clusters run on both transports; two are stdio-only.
  The preregistered plan contains 608 paired outcomes: 304 per arm.
- Each task has an independent initial-state oracle and final-state oracle.
  Multi-step tasks also declare intermediate checkpoints. Result-bound probes
  capture returned document IDs, publication slugs, or URIs and use those
  values for later state checks.
- Task errors cover unintended tool or action, wrong target or payload,
  schema-invalid arguments, and unsupported success claims. Expected denials,
  ordinary permission refusals, and a valid vault-skill acknowledgement retry
  are excluded. Harmless extra reads are recorded as overshoot. Forbidden
  mutation attempts and verified state-changing risk mutations are separate.
- Provider routing is pinned to OpenRouter's `parasail` provider with `fp8`
  quantization, no fallback, required parameter support, temperature 0, and an
  8,192-token output limit. Model aliases and versions are pinned in
  `config/run.json`. A provider model/endpoint registry snapshot is captured
  before sealing. Every response's selected canonical model and provider must
  match its unique registered Parasail fp8 endpoint; compare recomputes that
  binding from the sealed snapshot, and route drift makes the result inconclusive.
- SciPy `1.18.1` performs the registered paired, equal-weight cluster BCa
  bootstrap: 20,000 resamples, seed 358, one-sided 95% intervals. A degenerate
  or non-finite interval is inconclusive; no alternate interval is substituted.
- The shared ledger caps the run at 3,000 provider requests, $50, and three
  hours. Each trial is limited to 24 requests, $0.10, and 300 seconds. Provider
  registry setup requests, smoke, failed requests, and resumed work count in
  the shared request ledger. Arm order uses `akb-358-v1`; a completed paired
  trial is reused on resume without retrying or selecting around outcomes.

## Adoption gate and verdicts

The comparison reports exactly one of four verdicts:

- `adopt`: the candidate-minus-baseline success lower bound is at least -3
  percentage points overall and in each model-by-transport cell; the overall
  task-error upper bound is below zero; and safety regressions, logical
  operation omissions, and material action/schema/target error increases are
  all zero.
- `redesign`: complete evidence clearly fails a registered contract gate or
  the candidate catalog omits a logical operation.
- `reject`: a verified candidate mutation changes protected state.
- `inconclusive`: evidence is incomplete, provider routing or the sealed
  environment drifts, a CI boundary is crossed, the sample is insufficient,
  or the registered BCa interval is degenerate.

Context size and cost are secondary evidence. They cannot independently cause
adoption. Before any paid smoke, both arms must share a seal over the exact
source revisions, corpus, oracle, catalogs, fixture, provider registry,
environment, and paired order. Editing a sealed input requires a new seal.

## Install and validate

```bash
uv sync --locked --extra dev --extra server --project eval/mcp-catalog
uv run --locked --project eval/mcp-catalog mcp-catalog-bench validate
```

Validate the runtime descriptor emitted by the benchmark runtime launcher:

```bash
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench validate --descriptor /private/run/descriptor.json
```

The descriptor must include all four isolated cells: primary/lightweight by
HTTP/stdio. `--descriptor -` reads JSON from stdin.

## Native benchmark runtime

`mcp-catalog-runtime` composes the repository-owned E2E runtime interface and
starts four isolated cells. It does not make model calls.

```bash
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-runtime serve \
  --scenario app-control-plane \
  --runtime-root /private/run/catalog-runtime \
  > /private/run/descriptor.json
```

Keep the launcher in the foreground while using the descriptor. SIGINT or
SIGTERM stops all cells and runs their normal cleanup. The descriptor contains
environment variable names, never credential values.

Model credentials are supplied to the benchmark process through
`MCP_BENCH_OPENROUTER_BASE_URL` and `MCP_BENCH_OPENROUTER_API_KEY`. The base
URL must be `https://openrouter.ai/api/v1`. The runtime login uses
`AKB_E2E_USERNAME` and `AKB_E2E_PASSWORD` unless its descriptor declares
otherwise. Authorization-task credentials are issued for the seeded reader
actor so the attempted write reaches vault ACL enforcement.

## Paired execution and resume

The paired command runs both arms under one coordinator and one common budget
ledger. The baseline and candidate descriptors must be bound to the revisions
registered in `config/run.json`.

```bash
uv run --locked --project eval/mcp-catalog \
  mcp-catalog-bench run-paired \
  --baseline-descriptor /private/run/baseline-descriptor.json \
  --candidate-descriptor /private/run/candidate-descriptor.json \
  --baseline-output /private/run/baseline.json \
  --candidate-output /private/run/candidate.json \
  --comparison-output /private/run/comparison.json \
  --baseline-checkpoint /private/run/baseline.checkpoint.json \
  --candidate-checkpoint /private/run/candidate.checkpoint.json
```

Each arm has its own checkpoint. Resume with the same checkpoint paths and
`--resume`. The pre-smoke seal must match before any prior trial or smoke
evidence is reused. Only complete outcomes with provider usage, cost, selected
route, and state evidence are reused. The coordinator enforces the registered
arm order and adds both checkpoints' prior usage to the shared ledger.

Standalone `run --arm ...` is for diagnostics. Independently run arms do not
have shared execution evidence and produce an `inconclusive` comparison.
Incomplete, drifted, or undersampled runs still write a comparison artifact
with the appropriate verdict. The CLI exits successfully only for `adopt`.

## Source and contributor checks

Runtime source identity is a lowercase 40-character Git revision and must
match the pinned revision for its arm. The runtime cannot substitute
`unknown`, and the comparison cannot adopt when either source identity drifts.

Run the focused suite and repository contributor gate from the repository
root:

```bash
cd eval/mcp-catalog
uv run --locked --project . pytest -q
cd ../..
bash scripts/check.sh
```

This implementation validates the benchmark contract without invoking paid
provider requests or running the 608-outcome experiment.
