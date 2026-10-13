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
- Provider routing is pinned per model in OpenRouter: `deepinfra` for the
  primary model and `akashml` for the lightweight model. Both require `fp8`
  quantization, disable fallback, and require support for tools, tool choice,
  temperature, and max tokens. Model aliases and versions remain pinned in
  `config/run.json`; the registered prompt/completion price ceilings are
  `$0.06/$0.18` per million tokens for primary and `$0.225/$1.98` for
  lightweight. A model/endpoint registry snapshot is captured before sealing.
  Each response's selected canonical model and provider must match its unique
  registered endpoint and usage receipt; compare recomputes that binding from
  the sealed snapshot, and route drift makes the result inconclusive. Provider
  slugs and fallback behavior follow OpenRouter's
  [provider selection contract](https://openrouter.ai/docs/guides/routing/provider-selection).
- SciPy `1.18.1` performs the registered paired, equal-weight cluster BCa
  bootstrap: 20,000 resamples, seed 358, one-sided 95% intervals. A degenerate
  or non-finite interval is inconclusive; no alternate interval is substituted.
- The shared ledger enforces a $50 run ceiling and a $0.10 per-trial ceiling
  with atomic worst-case reservations. Each individual provider request has a
  300-second timeout. Request counts and elapsed time remain recorded as usage
  evidence. Arm order uses `akb-358-v1`; a completed paired trial is reused on
  resume without retrying or selecting around outcomes.

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

Each arm has its own checkpoint. For a full paired resume, copy both parent
artifacts and checkpoints to a working directory, pass the copied artifact
paths as `--baseline-output` and `--candidate-output`, pass the copied
checkpoints, and add `--resume`. The runner validates both artifacts and
checkpoints before opening either checkpoint for writing; after validation,
those working copies become the child outputs. The parent artifact supplies
the exact manifest when the default `--manifest` is used, while the current
task corpus and tool coverage must still match it.

Completed and failed terminal outcomes are reused without retry. Smoke cells
are reused from their sealed checkpoint. The coordinator continues the
original event sequence and links a retry attempt to any start that has no
terminal outcome. The paired ledger is restored once from its shared parent
receipt, and the child records parent artifact/checkpoint hashes plus any
accounting residual separately from per-trial receipts. It does not assign an
unattributed residual to a trial.

`--amendment` and `--selection` are separate source-bound execution modes.
They accept their own operator plans and do not use the full-resume input
contract described above.

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
provider requests or running the 28-trial remeasurement.
