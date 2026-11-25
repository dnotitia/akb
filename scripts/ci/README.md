# Repository-owned E2E test runtime

This guide covers backend pytest, the isolated runtime for live E2E, and how
to prepare an Ubuntu 24.04 host for the runtime.

## Backend pytest jobs

The unit job runs tests that need neither the live CI services nor extra setup.
It includes the repository runtime contract tests.

Use `pytestmark = pytest.mark.live_services` when a whole module uses
PostgreSQL, the BM25 extension, Redis, or MinIO from the integration job. In a mixed
module, add `@pytest.mark.live_services` to each test that uses those services.
For extra setup, use `@pytest.mark.needs_resource("resource and how to provide it")`.
Both jobs exclude tests marked `needs_resource`. The live-PG job runs the
exclusive role-prune test separately, before other tests create managed roles.

The backend integration job runs `backend/tests/rest_integration/`. These tests
call the REST and MCP routes in process through ASGI and use disposable
databases on real PostgreSQL. They cover product behavior such as document
edits and idempotency, browse and search filters, activity, and table schemas.

Both jobs exclude `backend/tests/runtime_e2e/` and `backend/tests/mcp_e2e/`.
The isolated runtime runs those suites. The unit and live PostgreSQL jobs
reject unregistered markers, skips during collection, and skips and expected
failures for selected tests.

Run the unit selection locally from `backend/`. The deployment contract tests
need Helm, `kubectl`, and Docker Compose.

```bash
AKB_TEST_FAIL_ON_SKIP=1 uv run --locked --extra dev python -m pytest tests/ \
  --ignore=tests/mcp_e2e --ignore=tests/runtime_e2e --strict-markers \
  -m 'not live_services and not needs_resource'
```

To run the in-process API suite locally, point `AKB_TEST_DSN` at a disposable
PostgreSQL instance with pgvector. The test connection must be able to create
and drop databases and roles. Set `AKB_TEST_S3_ENDPOINT` to a disposable MinIO
instance for the publication tests. Run this from `backend/`.

```bash
REQUIRE_REAL_PG=1 AKB_TEST_FAIL_ON_SKIP=1 uv run --locked --extra dev \
  python -m pytest tests/rest_integration --strict-markers -m live_services
```

The [backend pytest workflow](../../.github/workflows/backend-pytest.yml)
shows the extra services and environment variables for the full integration job.

## Live E2E suites

The backend gate runs `backend/tests/runtime_e2e/`. Its tests use a live HTTP
server, PostgreSQL, and S3-compatible storage for checks such as streaming
responses, PostgreSQL role enforcement, file byte round-trips, and
rollout recovery through the background worker.

The MCP gate runs `backend/tests/mcp_e2e/` against a running AKB backend.
The suite uses the official Python SDK for authenticated product scenarios
and checks the HTTP transport directly where the wire format matters. It also
checks OAuth, the Node stdio proxy, and MCP Inspector over both transports.

The lifecycle job runs the reset-isolation scenario. It holds an active
PostgreSQL writer across reset and checks that storage is isolated while
PostgreSQL and MinIO stay up.

The frontend gate starts the web app and runs the required Playwright tests
against the running AKB backend. Its Vite proxy forwards `/api` and `/mcp`
to that backend.

### Deployed MCP transport smoke

Run the read-only MCP transport smoke from the repository root. Set `AKB_URL`
to the deployed origin and export `AKB_E2E_PAT` from your credential manager.
Use a PAT for a fixture account. For an endpoint with a private certificate
authority, set `AKB_E2E_CA_FILE` to its CA bundle.

```bash
AKB_URL=https://akb.example.com uv run --locked --extra dev --project backend \
  python -m pytest backend/tests/mcp_e2e/test_transport_e2e.py \
  --confcutdir=backend/tests/mcp_e2e
```

The smoke checks authentication, protocol negotiation, and tool discovery
with the fixture account.

### Event Tail on a disposable endpoint

Use an authorized disposable endpoint for Event Tail. The tests create users
and Vaults, write data, and open real HTTP event streams. Set
`AKB_EVENT_TAIL_EXPECT_NATIVE=1` to require the PostgreSQL Native revision
fields. The repository runtime exercises Bare Git by default.

```bash
AKB_URL=http://localhost:8000 AKB_EVENT_TAIL_EXPECT_NATIVE=1 \
  uv run --locked --extra dev --project backend python -m pytest \
  backend/tests/runtime_e2e/test_event_tail.py \
  --confcutdir=backend/tests/runtime_e2e
```

For an SSO-only endpoint, export both `AKB_EVENT_TAIL_TOKEN` and
`AKB_EVENT_TAIL_OTHER_TOKEN` through your credential manager. The first fixture
user must be able to create and delete disposable Vaults. The second must
lack access to those Vaults. Leave both variables unset to have the tests
register local fixture users.

The tests check replay, cursor resume, and access restrictions. The live
check waits for the first heartbeat before writing a document, then compares
the event to the write receipt. It checks the Vault, URI, path, and opaque
revision and verifies that the event omits the display title in Native mode.

### Publication replica budget phase

Before pytest, `gate --suite backend` runs `publication_budget_probe.py`
against two runtime-owned API processes that share PostgreSQL. The supervisor
starts an API-only replica, checks readiness, and holds the fixture reset lock
until the replica acknowledges graceful shutdown. Missing setup or failures in
startup, assertions, replacement, or drain fail the gate.

Five required groups check publication-wide attempts distributed 16/15 between
processes, concurrent admission, correct-password refunds at both boundaries,
replica replacement with unchanged storage and retained backoff escalation,
and a held budget row lock. Contention must return 503 for anonymous and manager
password attempts while valid-token access and another publication still work.
Releasing the lock restores access.
Completion evidence records routing counts, statuses, PID/launch identities and
storage generation. Replacement reuses the same port and database without
a fixture reset. The supervisor drains the replica before pytest starts. On
failure it keeps the fixture unavailable and retains the process for cleanup.

The live-PG CI lane additionally requires `test_publication_budget_postgres.py`
for atomic counters, decay, bounded state, deletion/expiry and contention.
Migration 116 must be installed before serving the new API. A rolling upgrade
has installation-wide protection only after every old API replica is replaced.

### Run focused MCP tests with a descriptor

Reuse a running stack by passing the ready schema-v2 descriptor from `serve`
to pytest. Use `--profile transport-oidc` for
`serve` when running the full MCP selection. The descriptor supplies the app
and fixture origins, reset and discovery operations, and the fixture credential
variable names. Keep those environment variables exported in the pytest shell.

```bash
uv run --locked --extra dev --project backend python -m pytest \
  backend/tests/mcp_e2e -v --tb=short \
  --confcutdir=backend/tests/mcp_e2e \
  --runtime-descriptor /path/to/descriptor.json
```

Replace the suite directory in the first argument with a test file or node ID
to run a smaller selection. Pass `--runtime-descriptor -` to read the descriptor
from stdin. Run one consumer at a time against a runtime whose fixtures it can
reset.

## Repository-owned isolated runtime

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
| MinIO | dependency Compose | digest-pinned [`docker.io/dappros/minio`](#minio-image-provenance) (RELEASE.2025-09-07T16-13-09Z), `127.0.0.1:9000` by default | S3-compatible file storage |
| embedding stub | Ubuntu host process | `127.0.0.1:8888` | deterministic `/v1/embeddings` responses |
| backend | Ubuntu host process | `127.0.0.1:8000` | AKB application under test |
| frontend | Ubuntu host process | `127.0.0.1:3000` by default | Vite app with a proxy to the current run's backend |
| fixture control | supervisor-owned in-process app | `127.0.0.1:8889` | health, discovery, and isolated scenario reset |
| test suite | Ubuntu host process | no public listener | pytest or Playwright for the selected gate |

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

The supervisor has two modes.

- `gate` starts the stack, waits for readiness, and prints the schema-v2
  descriptor. It runs exactly one suite from `--suite backend`, `--suite mcp`,
  or `--suite frontend`, then stops its processes and dependencies. The
  frontend suite starts Vite automatically.
- `serve` starts the stack and prints the ready descriptor. The stack runs
  in the foreground until SIGINT or SIGTERM. Pass `--with-frontend` to start
  Vite on `--frontend-port` (default `3000`). The fixture's
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

The E2E workflow runs the Docker-backed reset and readiness checks in the
**AKB runtime lifecycle** job. Run those checks locally with the Node and npm
setup for the MCP gate.

```bash
AKB_TEST_RESET_ISOLATION=1 uv run --locked --extra dev --project backend python -m pytest \
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

The MCP gate with the `transport-oidc` profile also probes `tools/list` and a
read-only `tools/call` through the real proxy. It runs the Inspector smoke
over HTTP and stdio. `scripts/check.sh` runs the standalone client package
tests. The supervisor reports provisioning failures and test failures as
separate gate events.

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

CI, Docker builders, and the Ubuntu bootstrap use Node.js `22.23.4`, matching
`frontend/package.json`. The published `akb-mcp` CLI also checks the runtime
before loading the proxy or connecting, and exits with an upgrade message on
unsupported Node versions. npm may still install it with an engine warning
under its default configuration; installation success does not bypass this
runtime check. The supported package runtime floors follow the
[October 2026 Node.js security release](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases).

The smoke uses security-patched Node.js (`^22.23.4 || ^24.22.1 || ^26.12.1`) and the exact-pinned
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

### Run a live suite

Run these commands from the repository root. Install Docker with Compose v2
and `uv`. Prepare the Node dependencies for the suite you select.

- For `mcp`, use the Node version in `.nvmrc`. Run
  `(cd packages/akb-mcp-client && npm ci)` to install the Inspector dependency.
- For `frontend`, use the Node and pnpm versions in `frontend/package.json`.
  Run `(cd frontend && pnpm install --frozen-lockfile && pnpm exec playwright install chromium)`.

Generate private fixture credentials and name a directory for the runtime logs.
Use a fresh directory for each run.

```bash
RUNTIME_ROOT="$(mktemp -d /tmp/akb-e2e-runtime.XXXXXX)"
export AKB_E2E_USERNAME="$(uv run --locked --project backend python -c \
  'import secrets; print(f"akb-e2e-{secrets.token_hex(8)}")')"
export AKB_E2E_PASSWORD="$(uv run --locked --project backend python -c \
  'import secrets; print(secrets.token_urlsafe(24))')"
```

Run one of the following commands for the suite you want to check.

```bash
uv run --locked --extra dev --project backend python scripts/ci/e2e_runtime.py gate \
  --suite backend --profile tool-only --runtime-root "$RUNTIME_ROOT"
```

```bash
uv run --locked --extra dev --project backend python scripts/ci/e2e_runtime.py gate \
  --suite mcp --profile transport-oidc --runtime-root "$RUNTIME_ROOT"
```

```bash
uv run --locked --extra dev --project backend python scripts/ci/e2e_runtime.py gate \
  --suite frontend --profile tool-only --runtime-root "$RUNTIME_ROOT"
```

The supervisor stops its processes and dependency containers when the gate
ends. It leaves the private runtime root and logs for inspection. Remove the
root after collecting the logs and unset `AKB_E2E_USERNAME` and
`AKB_E2E_PASSWORD` in your shell.

### Keep a runtime running

Set up the per-run credentials and runtime root as for a live gate. Run `serve`
to keep the backend available for a second terminal. Pass `--with-frontend`
after installing the frontend dependencies if you need the web app.

```bash
uv run --locked --extra dev --project backend python scripts/ci/e2e_runtime.py serve \
  --with-frontend --frontend-port 3000 --runtime-root "$RUNTIME_ROOT" \
  > "$RUNTIME_ROOT/descriptor.json"
```

The descriptor file contains one JSON line when the runtime is ready. Use its
`services.web.origin` as `AKB_FRONTEND_URL` for
`(cd frontend && pnpm run test:e2e:real)`. To run the opt-in browser tests,
use `(cd frontend && pnpm run test:e2e:real:optional)` with the same URL and
any fixture credentials required by those tests. Send SIGINT or SIGTERM to
stop the runtime and clean up its processes and dependencies.

## Optional Ubuntu 24.04 environment bootstrap

`ubuntu_e2e_bootstrap.sh` is an optional host-preparation wrapper. It does not
contain runtime lifecycle logic. On a clean Ubuntu 24.04 host it:

1. verifies the base image and installs `curl`/CA certificates as needed;
2. persists and immediately applies `net.ipv4.tcp_mtu_probing=2` through
   `/etc/sysctl.d/99-akb-e2e-tcp-mtu-probing.conf` before any network download;
   mode 2 keeps probing proactively enabled for VPN/Docker Hub black-hole
   paths, and failure is a provisioning failure that stops the bootstrap;
3. uses the Ubuntu archive's npm to install the repository's pinned Node
   version, then installs the locked MCP client test dependencies when the
   selected profile or capability includes stdio;
4. installs and starts Docker Engine plus Compose v2 idempotently;
5. installs and verifies `uv` and Python 3.14 under the private runtime root;
6. runs `uv sync --locked --extra dev --project backend`;
7. installs the pinned pnpm and frontend dependencies for `--suite frontend`
   or `--with-frontend`; and
8. starts the same Python supervisor in `gate` or `serve` mode.

Package, network, Docker, uv, and Python failures are provisioning failures
and stop immediately with an actionable stderr message. The bootstrap does
not read launcher-specific variables or implement leases, tunnels, handoffs,
or teardown endpoints.

## Environment-specific flows

### GitHub Actions

GitHub Actions runs each live suite in its own disposable runtime. The
**Backend HTTP E2E** job runs `--suite backend --profile tool-only`.
**MCP E2E** runs `--suite mcp --profile transport-oidc`.
**Frontend E2E (real AKB)** runs `--suite frontend --profile tool-only`.
**AKB runtime lifecycle** checks reset and readiness in a separate job.

The workflow supplies the fixture credential environment. Each live gate
has a 30-minute job budget. The frontend gate gives Playwright 10 minutes
for the required browser specs that use AKB. Optional live browser specs
can be run separately from a local checkout.

Three fixture shards run the browser specs that answer their own HTTP against
a production build served by `vite preview`. Each fixture suite has one
worker and a 20-minute Playwright budget. On four vCPUs the 633 fixture tests took
35.3-35.5 minutes one at a time against the Vite dev server (five runs), the
same as in CI, and 18.3-18.6 minutes against the production build: a
navigation there loads 10 scripts instead of 336. More workers on one runner
did not scale: two to four workers saved a third of the time at most and broke
timing assertions, so the suite scales by shards.

After fixture mode, shard 3 builds the frontend bundle and runs the mock
suite and the Markdown reference scenario with MSW. Each mock command has a
10-minute Playwright budget. Shard 3 has a 50-minute job budget. Shards 1
and 2 each have a 30-minute job budget.

A suite timeout fails the job. The connected frontend job uploads
`frontend-e2e.log` on failure. Each fixture shard uploads traces and screenshots
on failure. Shard 3 uploads its fixture results before
running mock mode.

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
  --suite backend --profile tool-only --scenario empty --checkout "$PWD"

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

Preserve these contracts when changing the runtime.

- The live HTTP tests use `AKB_URL`.
- Dependency Compose runs PostgreSQL with pgvector and pinned MinIO. The
  backend, embedding stub, fixture control, and tests run on the host.
- Use Python 3.14 and install dependencies from `backend/uv.lock`.
- Each `gate` invocation selects one suite and fails on test errors.
- `gate --suite frontend` starts Vite. `serve` starts Vite with
  `--with-frontend`. Both use the selected `--frontend-port` and forward API
  requests to the run's backend.
- Stdout contains one schema-v2 JSON descriptor.
- Fixture reset acknowledges backend drain before reusing PostgreSQL and
  MinIO. It closes SQL admission, reports each phase, and fails closed.
- The catalog benchmark runs each of its four cells in isolation. Cells can
  run in parallel, and trials that mutate state in one cell run in sequence.
- Runtime state stays private and outside the checkout.

Run the focused runtime tests and the static checks described in the
repository contribution guide before committing runtime changes.

## CI checks and packaging coverage

Every PR runs static analysis and package tests, frontend Vitest, backend
unit tests, live PostgreSQL tests, three live E2E suites, runtime lifecycle
checks, three frontend fixture E2E shards with mock E2E on shard 3, and the
Keycloak login theme check.
Application packaging is one additional **Docker images** check, rather than
four independent image jobs.

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
that name. The consolidation leaves the test and static status names unchanged.

Validate the selector locally without Docker or registry access:

```bash
uv run --locked --extra dev --project backend python -m pytest \
  backend/tests/test_ci_image_scope_unit.py -q
```

## MinIO image provenance

The MinIO mirror is pinned to an index digest, not a mutable release tag. Its
Linux/amd64 manifest is the same `sha256:a1a8bd4ac40ad7881a245bab97323e18f971e4d4cba2c2007ec1bedd21cbaba2`
as the previous registry pin. The Linux/arm64 image config is also identical;
its compressed layer digests differ, while the config retains the upstream
uncompressed layer identities. This keeps the release and runtime content
unchanged after the previous registry exhausted its public pull quota. The
all-in-one build continues to verify both server and client binary checksums.
