# Repository-owned E2E test runtime

This document is the source of truth for AKB's endpoint-driven E2E suites, the
isolated repository-owned runtime used by hosted CI, and the optional clean
Ubuntu 24.04 host bootstrap. It describes repository behavior only; an
external launcher may invoke the same entrypoints but is not part of this
runtime contract.

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
`backend/scripts/ci/e2e_suite_runner.py`. `CURATED_SUITES` is the executable
list, while `DEFERRED_SUITE_GROUPS` is the explicit opt-out list with reviewed
reasons. Every `backend/tests/*_e2e.sh` file must appear in exactly one side of
that manifest. The static check and the live gate both fail when a suite is
unclassified, duplicated, overlaps both sides, or no longer exists.

Validate the manifest without starting any services:

```bash
python backend/scripts/ci/e2e_suite_runner.py --check-manifest
```

The runner is fail-closed. Each suite must finish successfully and provide a
complete `Results: N passed, M failed` line. A missing summary, a non-zero
suite return code, any failed assertion, or an unexpected zero-count suite
fails the gate. `EMPTY_COUNT_ALLOWED` is intentionally empty. Hosted CI,
the isolated runtime, and `scripts/run_canonical_e2e.sh` all execute this same
runner, so there is no second suite array to keep synchronized.

### MCP pytest behavior suite

The first MCP behavior scenario is an authenticated read-only
`akb_list_vaults({})` call through the official MCP Python SDK. The fixture
uses the SDK's public `Client` and Streamable HTTP transport in the pytest
process; it does not invoke Inspector, Node, or a separate MCP driver.

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

The descriptor must come from `backend/scripts/ci/e2e_runtime.py serve` or the
Ubuntu bootstrap. The fixture consumes the descriptor's app/fixture origins,
health and reset operations, discovery-declared credential environment names,
and the existing `empty` reset contract. It does not create another backend,
database, port topology, or credential fixture.

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
| MinIO | dependency Compose | pinned `minio/minio:RELEASE.2025-09-07T16-13-09Z`, `127.0.0.1:9000` by default | S3-compatible file storage |
| embedding stub | Ubuntu host process | `127.0.0.1:8888` | deterministic `/v1/embeddings` responses |
| backend | Ubuntu host process | `127.0.0.1:8000` | AKB application under test |
| frontend (`--with-frontend`) | Ubuntu host process | `127.0.0.1:3000` by default | existing Vite SPA and per-run backend proxy |
| fixture control | supervisor-owned in-process app | `127.0.0.1:8889` | health, discovery, and empty reset |
| MCP pytest behavior suite | Ubuntu host process | no public listener | authenticated `akb_list_vaults` scenario through the official Python SDK |
| curated suite runner | Ubuntu host process | no public listener | exact 15-suite gate and count semantics |

Only PostgreSQL and MinIO are managed by
`backend/scripts/ci/dependency-compose.yaml`. The root `docker-compose.yaml`
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
  the existing Vite SPA on `--frontend-port` (default `3000`), with its `/api`
  and `/mcp` proxy pointed at this run's backend origin. The fixture's
  `POST /reset` performs a safe empty reset and waits for backend readiness
  again; the frontend process remains owned by the same serve lifecycle.

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
  backend/scripts/ci/e2e_runtime.py gate \
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
  backend/scripts/ci/e2e_runtime.py serve \
  --with-frontend --frontend-port 3000 \
  --scenario empty --checkout "$PWD" --runtime-root "$RUNTIME_ROOT"
```

The first stdout line containing the ready schema-v2 JSON is the public
descriptor. Set `AKB_FRONTEND_URL` to its `services.web.origin` when running
the existing browser smoke (`cd frontend && pnpm exec playwright test`). Stop
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
2. installs the Ubuntu archive's `nodejs`/`npm` packages and verifies both
   executables before any selected stdio profile is validated;
3. installs and starts Docker Engine plus Compose v2 idempotently;
4. installs/verifies `uv` and Python 3.14 under the private runtime root;
5. runs `uv sync --locked --extra dev --project backend`; and
6. `exec`s the same Python supervisor in `gate` or `serve` mode.

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
  backend/scripts/ci/e2e_runtime.py gate \
  --scenario empty \
  --checkout "$GITHUB_WORKSPACE" \
  --runtime-root "${RUNNER_TEMP}/akb-e2e-runtime"
```

The workflow supplies the fixture credential environment and the PostgreSQL
execution command without exposing credential values to the descriptor or
runtime files. The curated list and assertion-count behavior come from the
shared suite runner, not from duplicated workflow shell steps.

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
bash backend/scripts/ci/ubuntu_e2e_bootstrap.sh gate \
  --scenario empty --checkout "$PWD"

: "${AKB_E2E_USERNAME:?inject a private per-run username before bootstrap}"
: "${AKB_E2E_PASSWORD:?inject a private per-run password before bootstrap}"
bash backend/scripts/ci/ubuntu_e2e_bootstrap.sh serve \
  --scenario empty --checkout "$PWD"
```

Both commands end in the same supervisor used by hosted CI. The `serve`
descriptor can be consumed by a caller that needs the app and fixture URLs;
the runtime itself remains unaware of that caller's orchestration protocol.
When no `--runtime-root` is supplied, the bootstrap creates a private
`/tmp/akb-e2e-bootstrap.XXXXXX` root. Child processes and dependency resources
are cleaned on exit, while that root and its logs remain caller-owned for
inspection and cleanup.

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
- runtime state stays private and outside the checkout.

Run the focused runtime tests and the static checks described in the
repository contribution guide before committing runtime changes.
