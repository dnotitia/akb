# Repository-owned E2E test runtime

This document is the source of truth for AKB's endpoint-driven E2E suites, the
isolated repository-owned runtime used by hosted CI, and the optional clean
Ubuntu 24.04 host bootstrap. It describes repository behavior only.

## Three layers

### 1. Endpoint-driven E2E suites

The shell suites under `backend/tests/` are HTTP clients. They target the
endpoint in `AKB_URL` (defaulting in each suite to `http://localhost:8000`),
create ephemeral users/vaults, and clean up their own data. They do not need to
know whether the endpoint is a normal local Compose backend, a deployed
backend, or the repository-owned runtime backend.

Run one suite against a reachable endpoint:

```bash
AKB_URL=http://localhost:8000 bash backend/tests/test_mcp_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_publications_e2e.sh
```

The shared curated gate is implemented by
`scripts/ci/e2e_suite_runner.py`. `CURATED_SUITES` is the executable
list, while `DEFERRED_SUITE_GROUPS` is the explicit opt-out list with reviewed
reasons. Every `backend/tests/*_e2e.sh` file must appear in exactly one side of
that manifest. The static check and the live gate both fail when a suite is
unclassified, duplicated, overlaps both sides, or no longer exists.

Validate the manifest without starting any services:

```bash
python scripts/ci/e2e_suite_runner.py --check-manifest
```

The runner is fail-closed. Each suite must finish successfully and provide a
complete `Results: N passed, M failed` line. A missing summary, a non-zero
suite return code, any failed assertion, or an unexpected zero-count suite
fails the gate. `EMPTY_COUNT_ALLOWED` is intentionally empty. Hosted CI,
the isolated runtime, and `scripts/run_canonical_e2e.sh` all execute this same
runner, so there is no second suite array to keep synchronized.

### Publication replica budget phase

Before the MCP/shell suites, every isolated `gate` runs
`publication_budget_probe.py` against two distinct runtime-owned API processes
sharing PostgreSQL. The supervisor starts an API-only replica, checks readiness,
and holds the fixture reset lock until that replica has acknowledged graceful
shutdown. Missing setup or failed startup, assertions, replacement or drain fail
the gate; there are no optional credentials or single-origin fallbacks.

Five required groups check publication-wide attempts distributed 16/15 between
processes, concurrent admission, correct-password refunds at both boundaries,
actual replica process replacement with unchanged storage and retained backoff
escalation, and a deliberately held budget row lock. Contention must return 503
for both anonymous and manager password attempts while valid-token access and
another publication continue to work; releasing the lock restores access.
Completion evidence records routing counts, statuses, PID/launch identities and
storage generation, never passwords or tokens. Replacement reuses the same
port and database without a fixture reset. The replica is drained before any
MCP fixture reset or shell suite starts; failure leaves the fixture unavailable
and retains process ownership for cleanup.

The live-PG CI lane additionally requires `test_publication_budget_postgres.py`
for atomic counters, decay, bounded state, deletion/expiry and contention.
Migration 116 must be installed before serving the new API. A rolling upgrade
has installation-wide protection only after every old API replica is replaced.

### MCP pytest behavior suite

The authenticated MCP behavior suite runs through the official Python SDK.
`test_list_vaults_e2e.py` remains the small typed `akb_list_vaults({})`
canary, while `test_product_e2e.py` covers the baseline product scenarios and
`test_detail_e2e.py` covers the migrated detailed regressions: exact-text edit,
body hash/OCC, collection boundaries, Unicode search, graph link/unlink, grep
replacement, and ownership transfer. `test_publication_okf_e2e.py` covers
detailed publication lifecycle and public-resolution oracles, the MCP-only
move regression, and OKF export/import round-trips with their writer boundary.
The baseline suite covers vault and document lifecycle, browse/search/
drill-down, move and aliases, relations/activity/history/diff, access roles
and public levels, tables/SQL/DDL, basic publication, help, and deletion.
Each pytest test receives the existing fixture's reset/login/SDK lifecycle;
scenarios that need role boundaries add a second user through the same
authenticated endpoint and client lifecycle.
The fixture uses the SDK's public `Client` and Streamable HTTP transport in the
pytest process; it does not invoke Inspector, Node, or a separate MCP driver.

`test_table_schema_e2e.py` checks the real table and Vault schema HTTP responses
against the same runtime's served OpenAPI contract after create, alter, and
migration, including replay of the same migration key. These six cases run in
the mandatory pytest gate with the shared descriptor/reset/PAT lifecycle;
missing runtime setup fails preparation rather than skipping coverage. To run
only these cases against a repository-owned `serve` runtime, use the pytest
command below with `backend/tests/mcp_e2e/test_table_schema_e2e.py` as the test
path and keep `--confcutdir=backend/tests/mcp_e2e` and `--runtime-descriptor`.

`test_graph_catalog_e2e.py` checks provenance, orphan membership and titles,
link/unlink/move/delete identity, MCP results and actual Reader/Outsider access
through the same mandatory runtime fixture lifecycle. It creates its secondary
users in the disposable fixture and does not skip ACL checks for missing PATs.
The selected runtime backend is exercised through HTTP; Native catalog paging,
concurrent edits and stable endpoint identity are additionally required in the
live-PG lane via `test_graph_native_catalog_pg.py` and
`test_native_graph_resource_identity_pg.py`.

`test_adoption_vault_delete_e2e.py` requires planned, applied and blocked
adoption deletion through HTTP, with a non-admin owner, Reader and system
operator. It checks denied deletion, retained UUIDs, same-name recreation and
rejected replay through the same reset/PAT fixtures. The live-PG adoption suite
also checks upgrade retention and both deletion/adoption lock orders. Apply
commits each target separately: a deleted later target returns 409 after earlier
live targets may have committed; read the retained plan to inspect partial progress.

`test_rollout_authority_e2e.py` requires one- and two-installation recovery
through the real worker, with a non-admin owner and registry operator. A NULL
canary makes a real NOT NULL migration fail; authorized row repair and resume
must preserve the source job, checksum, grant and data. The cases also cover
original-request replay while blocked, denial of new requests without active
authority, credential rotation/revocation and denial of historical reads/replays
after uninstall even when an unrelated installation becomes active. They use
the mandatory descriptor/reset/PAT lifecycle without optional credentials.

`test_read_capacity_e2e.py` requires six real HTTP checks for counted and
schema-only catalogs, page/sentinel ordering, exact and filtered count-only
requests, row CAS, index/schema compatibility, and Reader/Outsider ACLs.
Each case uses the descriptor/reset lifecycle and mints a real non-admin owner;
ACL actors are also created through the isolated API, with no optional PATs or
endpoint variables. Live-PG tests separately verify that schema-only catalogs
never call row counting and both fresh/existing Tables receive the page index.

The detailed security suite also covers declarative table constraints and
indexes, stable permission envelopes, private-document and graph boundaries,
SQL failure-state integrity, and PAT write scopes. The PG-native ACL backstops
remain in `test_pg_rbac_e2e.sh` and `test_vault_scope_sql_e2e.sh`; the retained
shell security suite keeps only REST, health, and role-source checks plus the
MCP setup calls needed to seed those REST assertions.

The mixed shell suites retain only their non-migrated boundaries: transport and
protocol/session checks, direct REST comparisons and ACLs, file byte hashes,
and SQL/cross-vault checks. Their migrated MCP product assertions and unused
raw JSON-RPC helpers were removed so each detailed MCP behavior has one
SDK-based execution path. MCP setup calls that seed data for a retained REST
or SQL check remain. The separate stdio/Inspector boundaries remain in their
existing lanes.

The repository runtime remains the owner of backend startup, readiness,
fixture reset, credentials, and teardown. Give the same schema-v2 descriptor
to the local and hosted command through stdin:

```bash
uv run --locked --extra dev --project backend python -m pytest \
  backend/tests/mcp_e2e -v --tb=short \
  --confcutdir=backend/tests/mcp_e2e --runtime-descriptor -
```

To consume a descriptor file instead, pass its path to the same option:

```bash
uv run --locked --extra dev --project backend python -m pytest \
  backend/tests/mcp_e2e -v --tb=short \
  --confcutdir=backend/tests/mcp_e2e \
  --runtime-descriptor /path/to/descriptor.json
```

The descriptor must come from `scripts/ci/e2e_runtime.py serve` or the
Ubuntu bootstrap. The fixture consumes the descriptor's app/fixture origins,
health and reset operations, discovery-declared credential environment names,
and the existing `empty` reset contract. A normal descriptor does not create
another backend, database, port topology, or credential fixture. The native
catalog benchmark launcher is documented in `eval/mcp-catalog/README.md` and
composes this runtime through its CLI and descriptor contract.

### 2. Repository-owned isolated runtime

The runtime supervisor is `e2e_runtime.py`. It owns the test infrastructure
around AKB and keeps state outside the checkout. Its private runtime root
contains `config/`, `logs/`, `state/`, and vault state with restrictive
permissions. The supervisor runs backend and embedding processes with the
runtime root as their working directory and uses `--app-dir` to point them at
the checkout's `backend/` directory. Runtime execution therefore does not
write generated configuration or fixture state into the checkout.

The topology is deliberately small:

| Component | Process owner | Endpoint / image | Responsibility |
| --- | --- | --- | --- |
| PostgreSQL + pgvector | dependency Compose | `pgvector/pgvector:pg16`, `127.0.0.1:15432` by default | AKB database and vector index |
| MinIO | dependency Compose | digest-pinned `icr.io/fusion-open/minio/minio` (RELEASE.2025-09-07T16-13-09Z), `127.0.0.1:9000` by default | S3-compatible file storage |
| embedding stub | Ubuntu host process | `127.0.0.1:8888` | deterministic `/v1/embeddings` responses |
| backend | Ubuntu host process | `127.0.0.1:8000` | AKB application under test |
| frontend (`--with-frontend`) | Ubuntu host process | `127.0.0.1:3000` by default | existing Vite SPA and per-run backend proxy |
| fixture control | supervisor-owned in-process app | `127.0.0.1:8889` | health, discovery, and isolated scenario reset |
| MCP pytest behavior suite | Ubuntu host process | no public listener | authenticated MCP product scenarios through the official Python SDK |
| curated suite runner | Ubuntu host process | no public listener | curated shell gate and count semantics |

Only PostgreSQL and MinIO are managed by
`scripts/ci/dependency-compose.yaml`. The root `docker-compose.yaml`
is the normal development stack and is the default path for contributors;
the dependency Compose file is an internal implementation detail of this
runtime and should not be started directly.

Callers that share a host with another development stack may select free
`RuntimeConfig.postgres_port` and `RuntimeConfig.minio_port` values. The
supervisor passes those exact values to Compose, generated AKB configuration,
readiness probes, and fixture database/S3 clients; the defaults above remain
unchanged for the canonical local and CI paths.

The supervisor has two modes:

- `gate`: starts dependencies, fixture control, embedding stub, and backend;
  waits for readiness; prints the schema v2 descriptor; runs the shared
  pytest behavior scenario and curated suite runner; then stops child
  processes and dependency resources.
- `serve`: starts the same stack, prints the ready descriptor, and remains in
  the foreground until SIGINT/SIGTERM. Pass `--with-frontend` to also start
  the existing frontend package's `pnpm run dev` contract on `--frontend-port`
  (default `3000`), with its `/api` and `/mcp` proxy pointed at this run's
  backend origin. The fixture's
  `POST /reset` reuses PostgreSQL, MinIO, their volumes/network, and the
  embedding process. It closes fixture-control admission and stops stdio, then
  asks the backend to stop accepting requests and claiming new worker work.
  The test-only `e2e_backend.py` launcher drains HTTP and worker tasks, joins
  executor work, and emits a private PID/launch-bound shutdown report. Reset
  requires that report, normal process exit, and no remaining owned process
  groups. A signal, timeout, cancellation, or forced kill is not a drain proof.

  With the backend stopped, a maintenance connection closes PostgreSQL's
  database admission and waits for earlier client sessions to finish/close.
  Row cleanup and removal of tables added by tests run in one transaction;
  installation/migration singleton state remains valid. A missing canonical
  table fails closed. Admission reopens only after successful cleanup. Fixtures
  are seeded offline, then the backend is restarted once to rebuild its caches;
  a fresh PAT and stdio initialization must succeed before readiness is published.

  Git paths, configuration, and MinIO buckets use a new generation on the same
  storage. Earlier paths/buckets remain retired until runtime cleanup, so even
  an ambiguous delayed remote PUT cannot enter the next scenario. Retired
  objects can consume storage during long runs. Supervisor cleanup removes its
  Compose volumes; the caller removes the private runtime root after collecting
  logs. Frontend and fixture origins remain stable, and the installed Node
  consumer is reused. Reset never truncates tables under live backend workers.

  Reset has one total `--timeout-seconds` budget (default 180 seconds). The MCP
  pytest and catalog clients allow 210 seconds for the default reset response.
  Failure or cancellation leaves the fixture `failed`, hides stale discovery,
  and rejects further mutations/resets; dispose it and start a fresh runtime.
  Queued controls from an earlier generation are rejected. Evidence records
  `strategy: reuse_after_drain`, shutdown acknowledgement, generation, stable
  dependency/replaced application identities, total/per-phase times, and failure
  phase. These are test isolation guarantees, not a distributed transaction.

A local #686 comparison used `transport-proxy`, `empty`, no frontend, three
serial idle resets per implementation, and identical prebuilt PostgreSQL/pinned
MinIO images. Preparation and final cleanup were excluded. On macOS arm64 with
Docker Engine 29.1.2:

| Reset strategy | Three samples (seconds) | Mean |
| --- | --- | --- |
| Full recreation (`a6d0f86f`) | 14.088 / 14.165 / 14.179 | 14.144s |
| Dependency reuse after acknowledged drain | 5.370 / 6.978 / 6.211 | 6.186s |

Reuse saved 7.957 seconds/reset (56.3%). Mean phases were backend drain 0.637s,
offline PG reset 0.512s, fixture preparation 0.569s, backend restart 3.980s,
stdio restart 0.076s; dependency identity verification adds about 0.412s.
An earlier uncoordinated live reset measured 1.038s, but did not ensure writer
completion. This small local sample measures idle reset latency, not load p95,
full-suite throughput, or monetary cost. The measurement Compose used a prebuilt
PG image rather than invoking a build. Retired namespace storage was not included
in the time measurement; it remains until runtime disposal. Runtime phase evidence
allows measurements on other hosts.

The fixture password hash is computed once per runtime and reused while the
injected password is unchanged. Reset still restores account permissions/status
and scenario data; fresh tokens and storage generations are unchanged. The cache
has one private in-memory entry, is replaced on password rotation, and is only
populated after successful hashing. It is not emitted in discovery or logs.

A subsequent five-reset comparison against `d9a5082f` used the same idle profile
and images. Fixture preparation mean fell from 0.496s to 0.191s (61.4%); median
fell from 0.456s to 0.158s. Total reset mean was 3.790s before and 3.372s after
(11.0%). Backend startup mean was nearly unchanged at 2.240s/2.250s. Across initial
preparation plus five resets, supervisor bcrypt calls fell from six to one.
These before/after samples are the relevant comparison for the cache change;
startup latency differs from the earlier three-sample run above.

The E2E workflow also runs the Docker-backed reset regression explicitly:

```bash
AKB_TEST_RESET_ISOLATION=1 uv run --locked --project backend python -m pytest \
  backend/tests/test_e2e_runtime_reset_isolation.py -v --tb=short
```

It holds a database writer across reset, proves reset waits and new SQL admission
is closed, then verifies PG/MinIO/embed identities stay stable while PAT/stdio
and Git/bucket generations rotate. A real API document verifies backend config
loading; canonical-table corruption must leave reset/readiness/SQL admission closed.

The native catalog benchmark launcher is separate from this generic runtime;
its interface and descriptor composition are documented in
`eval/mcp-catalog/README.md`. The generic runtime itself has no model or
benchmark-topology knowledge.

Each invocation also selects one explicit capability profile. The default
`tool-only` profile starts only the HTTP backend, PAT fixture, and shared
fixture control app. `transport-proxy` adds a clean install of the checkout's
`akb-mcp` package into the private runtime root and starts its real executable
over stdin/stdout. `oidc-resource-server` adds an ephemeral RSA issuer/JWKS
fixture with deterministic token variants. `transport-oidc` composes both.
`keycloak-overlay` is intentionally rejected with
`blocked_runtime_config`; browser SSO and the real Keycloak service remain a
specialist overlay. Add a capability explicitly with repeatable
`--capability stdio` / `--capability oidc` (or use `--profile`). Optional
processes are never started for `tool-only`, and an unavailable selected
capability is a hard failure rather than a skip.

The ready descriptor is the only line written to stdout by the supervisor.
Operational logs go to stderr and the private runtime log directory. Its
shape is schema v2:

```json
{"schema_version":2,"status":"ready","scenario":"empty","services":{"app":{"origin":"http://127.0.0.1:8000","health":{"method":"GET","url":"http://127.0.0.1:8000/readyz"},"discovery":{"method":"GET","url":"http://127.0.0.1:8000/openapi.json"}},"fixture":{"origin":"http://127.0.0.1:8889","health":{"method":"GET","url":"http://127.0.0.1:8889/health"},"reset":{"method":"POST","url":"http://127.0.0.1:8889/reset","content_type":"application/json","body":{"scenario":"empty"}},"discovery":{"method":"GET","url":"http://127.0.0.1:8889/discover"}}},"credentials":{"username_env":"AKB_E2E_USERNAME","password_env":"AKB_E2E_PASSWORD","login_path":"/api/v1/auth/login"}}
```

Credential values are read only from the named environment variables and are
never put in the descriptor, argv, or runtime logs. The only supported
scenarios are `empty`, `app-installation-lifecycle`, `app-release-rollout`,
and `app-control-plane`; callers must pass the selected scenario when mapping
a runtime command. Both `gate` and `serve` require
`AKB_E2E_USERNAME` and `AKB_E2E_PASSWORD` to be present before the supervisor
starts; the runtime does not generate or persist those values.

The runtime source identity uses `AKB_E2E_SOURCE_REVISION` first when it is
present. It must be a lowercase 40-hex Git SHA. When the variable is absent,
the supervisor uses `git rev-parse HEAD` from a real checkout. A raw checkout
without `.git` must receive the explicit variable; missing or invalid input
fails preparation as `blocked_runtime_config`, and `unknown` is never emitted
as the source revision.

For a selected transport profile the supervisor mints one candidate-bound PAT
in memory, passes it to the real proxy only as the `AKB_PAT` child environment
value, and exposes only the configured PAT environment-name in descriptor and
discovery. The private discovery `runtime` object records the exact source
revision, backend/proxy artifact versions, protocol revision, transport,
selected capabilities, tool-case coordinates, fixture reset, and whether the
stdio initialize/tools-list/read probes crossed the process boundary. The
OIDC discovery object exposes issuer/JWKS/token coordinates and variant names,
never an access token or signing key.

With `serve --with-frontend`, the same descriptor adds a `web` service whose
`origin` is the screen address and whose `health` operation is `GET` on that
origin. Its fixture discovery adds the same `web` origin, the browser input
name `AKB_FRONTEND_URL`, and the backend proxy target. This keeps the web
address, readiness check, and API connection source-neutral while preserving
the existing `app` and `fixture` services.

`gate` runs the existing curated HTTP suite runner for every profile. A
transport profile additionally runs the existing proxy contract/reconnect
tests after the clean install and observes `tools/list` plus a read-only
`tools/call` through the real child process. Provisioning failures and live
product-assertion failures are emitted as separate gate events.

### MCP Inspector consumer smoke

The repository-owned MCP Inspector command is a development tool, not a
second runtime. It lives with the stdio client and is installed by the
client's existing npm workflow:

```bash
(cd packages/akb-mcp-client && npm ci)
```

The package entrypoint provides the machine-readable consumer smoke. It
consumes the ready descriptor printed by the repository runtime and never
needs a global Inspector install:

```bash
npm --prefix packages/akb-mcp-client run --silent inspect -- \
  --target both --descriptor /path/to/descriptor.json
```

The smoke uses Node.js `>=22.19.0` and the exact-pinned
`@modelcontextprotocol/inspector@2.4.0` public executable. For each selected
transport it runs `initialize`, `tools/list --strict --format json`, and
`akb_list_vaults({})` through the actual Inspector child process. It reports
HTTP and stdio independently, retains Inspector diagnostics and warnings,
and exits non-zero when either transport or the representative schema/result
check fails.

The smoke obtains endpoints, fixture reset, credential environment names, and
the clean installed `akb-mcp` executable from the ready descriptor and its
fixture discovery. A fixture PAT is placed only in a private 0700 run
directory's 0600 temporary Inspector config, which is deleted in `finally`
cleanup. The PAT is not placed in argv, logs, command output, reports, or
uploaded artifacts. The focused package regression exercises the same public
entrypoint with a synthetic marker and verifies config removal and redaction.

For a direct local gate, use the uv-managed Python environment to generate
per-run values and export them without placing a credential value in the
command line or repository files:

```bash
uv sync --locked --extra dev --project backend
(cd packages/akb-mcp-client && npm ci)
RUNTIME_ROOT="$(mktemp -d /tmp/akb-e2e-runtime.XXXXXX)"
export AKB_E2E_USERNAME="$(uv run --locked --project backend python -c \
  'import secrets; print(f"akb-e2e-{secrets.token_hex(8)}")')"
export AKB_E2E_PASSWORD="$(uv run --locked --project backend python -c \
  'import secrets; print(secrets.token_urlsafe(24))')"
uv run --locked --project backend python \
  scripts/ci/e2e_runtime.py gate \
  --scenario empty --checkout "$PWD" --runtime-root "$RUNTIME_ROOT"
unset AKB_E2E_USERNAME AKB_E2E_PASSWORD
```

To run the existing frontend against that isolated backend, install the
frontend's locked dependencies first, then use the public `serve` entrypoint:

```bash
(cd frontend && pnpm install --frozen-lockfile)
RUNTIME_ROOT="$(mktemp -d /tmp/akb-fe-runtime.XXXXXX)"
export AKB_E2E_USERNAME="$(uv run --locked --project backend python -c \
  'import secrets; print(f"akb-fe-{secrets.token_hex(8)}")')"
export AKB_E2E_PASSWORD="$(uv run --locked --project backend python -c \
  'import secrets; print(secrets.token_urlsafe(24))')"
uv run --locked --project backend python \
  scripts/ci/e2e_runtime.py serve \
  --with-frontend --frontend-port 3000 \
  --scenario empty --checkout "$PWD" --runtime-root "$RUNTIME_ROOT"
```

The first stdout line containing the ready schema-v2 JSON is the public
descriptor. Set `AKB_FRONTEND_URL` to its `services.web.origin` when running
the existing browser smoke (`cd frontend && pnpm run test:e2e:real`). Stop
the foreground command with SIGINT/SIGTERM; the supervisor then cleans the
frontend, backend, embedding process, fixture control, and dependency Compose
resources for that run only.

On gate completion or SIGINT/SIGTERM, the supervisor stops its child
processes and dependency Compose resources. It intentionally leaves the
private runtime root and logs for inspection; the caller removes the
explicit temporary root after collecting logs. No credential value is written
to the descriptor, logs, argv, or committed files.

### 3. Optional Ubuntu 24.04 environment bootstrap

`ubuntu_e2e_bootstrap.sh` is an optional host-preparation wrapper. It does not
contain runtime lifecycle logic. On a clean Ubuntu 24.04 host it:

1. verifies the base image and installs `curl`/CA certificates as needed;
2. persists and immediately applies `net.ipv4.tcp_mtu_probing=2` through
   `/etc/sysctl.d/99-akb-e2e-tcp-mtu-probing.conf` before any network download;
   mode 2 keeps probing proactively enabled for VPN/Docker Hub black-hole
   paths, and failure is a provisioning failure that stops the bootstrap;
3. installs the Ubuntu archive's `nodejs`/`npm` packages and verifies both
   executables before any selected stdio profile is validated;
4. installs and starts Docker Engine plus Compose v2 idempotently;
5. installs/verifies `uv` and Python 3.14 under the private runtime root;
6. runs `uv sync --locked --extra dev --project backend`; and
7. `exec`s the same Python supervisor in `gate` or `serve` mode.

Package, network, Docker, uv, and Python failures are provisioning failures
and stop immediately with an actionable stderr message. The bootstrap does
not read launcher-specific variables or implement leases, tunnels, handoffs,
or teardown endpoints.

## Environment-specific flows

### Normal local development

The root Compose stack is the default contributor path:

```bash
docker compose up -d
AKB_URL=http://localhost:8000 bash backend/tests/test_mcp_e2e.sh
```

Use any individual suite against the running backend. This path uses the
normal root Compose services and is independent of the isolated runtime.

### GitHub Actions

The hosted runner is already provisioned with Docker, uv, and Python 3.14.
The workflow keeps the locked dependency contract and calls the supervisor
directly:

```bash
uv sync --locked --extra dev --project backend
uv run --locked --project backend python \
  scripts/ci/e2e_runtime.py gate \
  --scenario empty \
  --checkout "$GITHUB_WORKSPACE" \
  --runtime-root "${RUNNER_TEMP}/akb-e2e-runtime"
```

The workflow supplies the fixture credential environment and the PostgreSQL
execution command without exposing credential values to the descriptor or
runtime files. The curated list and assertion-count behavior come from the
shared suite runner, not from duplicated workflow shell steps.

The combined API/browser job has a 45-minute budget. In CI, Playwright's whole
browser suite is bounded to 30 minutes, leaving time for runtime preparation,
the API suites, cleanup, and failure-log upload. Existing per-test timeouts and
assertions remain unchanged. A browser-suite timeout is a failing check, not a
successful or skipped run; its diagnostics stay in `frontend-e2e.log` within the
runtime's failure artifacts.

### Clean Ubuntu 24.04 host

Use the optional bootstrap when the host needs its container and Python
toolchain prepared first. The bootstrap provisions uv and Python itself, but
it cannot invent the fixture credentials before the supervisor starts. The
caller must inject both private per-run values into the environment; the
following guards make that requirement explicit without assuming a system
`python3`:

```bash
: "${AKB_E2E_USERNAME:?inject a private per-run username before bootstrap}"
: "${AKB_E2E_PASSWORD:?inject a private per-run password before bootstrap}"
bash scripts/ci/ubuntu_e2e_bootstrap.sh gate \
  --scenario empty --checkout "$PWD"

: "${AKB_E2E_USERNAME:?inject a private per-run username before bootstrap}"
: "${AKB_E2E_PASSWORD:?inject a private per-run password before bootstrap}"
bash scripts/ci/ubuntu_e2e_bootstrap.sh serve \
  --scenario empty --checkout "$PWD"
```

Both commands end in the same supervisor used by hosted CI. The `serve`
descriptor can be consumed by a client that needs the app and fixture URLs.
When no `--runtime-root` is supplied, the bootstrap creates a private
`/tmp/akb-e2e-bootstrap.XXXXXX` root. Child processes and dependency resources
are cleaned on exit, while that root and its logs remain caller-owned for
inspection and cleanup.

The bootstrap does not set a fixed interface MTU. TCP MTU probing is the only
network adjustment it owns, and it is applied before `apt`, `curl`, or `uv`
network activity so VPN paths can recover from black-holed packet sizes.

## Runtime change checklist

When changing this area, preserve all of the following:

- endpoint-driven suites continue to use `AKB_URL`;
- dependency Compose remains limited to pgvector/PostgreSQL and pinned MinIO;
- backend, embedding stub, fixture control, and suite runner remain host-side
  processes;
- Python `>=3.14` and `backend/uv.lock` are used with `uv sync --locked`;
- the suite manifest and fail-closed assertion-count semantics remain shared;
- the optional frontend process uses an explicit `--with-frontend` flag, an
  isolated `--frontend-port`, and the run's backend origin for Vite proxying;
- descriptor stdout stays parseable as one schema v2 JSON line; and
- fixture reset acknowledges backend drain before reusing PostgreSQL/MinIO,
  fences SQL admission, publishes phase evidence, and fails closed; and
- the catalog benchmark's four cell runtimes remain isolated, with parallel
  cells and serial mutable-state trials inside each cell; and
- runtime state stays private and outside the checkout.

Run the focused runtime tests and the static checks described in the
repository contribution guide before committing runtime changes.

## CI checks and packaging coverage

Every PR retains the five existing checks: static analysis/package tests,
backend unit tests, live PostgreSQL tests, the repository runtime E2E gate,
and frontend mock E2E. Application packaging is one additional **Docker
images** check, rather than four independent image jobs.

The PostgreSQL image is built and exercised by both the live-DB job and the
repository runtime. It needs no third build in the packaging workflow.
Backend, frontend and all-in-one packaging remain checked because running
the source tests alone does not prove that their Dockerfiles build.

On PRs, `image_scope.py` compares the complete PR diff against its merge base:

| Changed input | Application images built |
| --- | --- |
| Backend source, dependencies or Docker inputs | Backend + all-in-one |
| Frontend workspace or Docker inputs | Frontend + all-in-one |
| `deploy/all-in-one/` | All-in-one |
| Backend tests/changelog, human documentation, agent settings, standalone packages, deployment configuration | None; existing test/static checks still run |
| Image workflow/selector, root Docker ignore rules, or unclassified input | All three |
| Main push, manual run, or unavailable PR diff | All three |

The selector keeps deleted paths and both sides of renames. A new shared build
input must be assigned to its consumers when changing a Dockerfile; currently
standalone `packages/`, `config/`, `eval/`, and non-image deployment manifests
are outside the application build inputs. Unclassified paths conservatively
select all three application images. PostgreSQL changes remain covered by the
unchanged live-DB and runtime jobs.

Workflow triggers stay unconditional: a PR with no image changes completes the
packaging check without installing a builder, instead of leaving a required
workflow pending. Selected builds share one runner and use separate GitHub
Actions layer-cache scopes. Nothing is published. A failed build still fails
the job, while the other selected builds continue unless the run is cancelled.

The consolidated status is named `Docker images`; repositories with required
checks configured should replace the old `Docker image (...)` contexts with
that name. The five test/static status names are unchanged.

Validate the selector locally without Docker or registry access:

```bash
uv run --locked --extra dev --project backend python -m pytest \
  backend/tests/test_ci_image_scope_unit.py -q
```
