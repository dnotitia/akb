# Contributing to AKB

Thanks for considering a contribution.

## Contributor Terms

By submitting a contribution (pull request, patch, or any code/content
addition) to this repository, you agree that:

1. Your contribution is licensed to Dnotitia, Inc. and downstream users
   under the license that applies to the directory you're contributing
   to:
   - Contributions to `packages/akb-mcp-client/` are licensed under the
     [MIT License](./packages/akb-mcp-client/LICENSE).
   - Contributions to every other directory (backend, frontend,
     deployment manifests, eval, etc.) are licensed under the
     [Business Source License 1.1](./LICENSE), including the
     Additional Use Grant currently in effect and the automatic
     conversion to the Change License (Apache 2.0) on the Change Date.
2. You grant Dnotitia, Inc. and downstream users a perpetual, worldwide,
   non-exclusive, royalty-free, irrevocable patent license to make, use,
   sell, offer for sale, import, and otherwise transfer your contribution,
   limited to patent claims you can license that would necessarily be
   infringed by your contribution alone or in combination with the project.
3. You grant Dnotitia, Inc. the right to relicense your contribution under
   any future license adopted by the AKB project — including a more
   permissive open-source license. This preserves the project's ability
   to evolve its licensing without re-negotiating with every contributor.
4. You retain copyright on your contribution.
5. You have the right to submit the contribution under these terms — i.e.
   you wrote it yourself, or it comes from sources whose licenses permit
   inclusion in this project.

The "AKB", "Dnotitia", and "Seahorse" names and logos are trademarks of
Dnotitia, Inc. and are not covered by the software license. See
[TRADEMARKS.md](./TRADEMARKS.md) for the trademark policy.

## Development Setup

```bash
# 1. Configure
cp config/app.yaml.example   config/app.yaml
cp config/secret.yaml.example config/secret.yaml
$EDITOR config/secret.yaml   # at minimum, set embed_api_key

# 2. Run the stack
docker compose up -d

# 3. Tail backend logs
docker compose logs -f backend
```

Backend code lives in `backend/app/`; the MCP server in `backend/mcp_server/`;
the frontend in `frontend/`. The stdio MCP proxy that ships on npm lives
under `packages/akb-mcp-client/`.

The local Compose path and the repository-owned E2E runtime are separate
development paths. See [`backend/scripts/ci/README.md`](backend/scripts/ci/README.md)
for the suite, runtime, and clean-host bootstrap contracts.

## Configuration

The backend reads exactly two YAML files — `app.yaml` (non-secret) and
`secret.yaml` (gitignored) — from `./config/` or `/etc/akb/`. **No environment
variables are read by the backend.** When you need a new setting:

1. Add the field with a sensible default to `Settings` in
   `backend/app/config.py`.
2. Add the same key (with explanatory comment) to
   `config/app.yaml.example` (or `secret.yaml.example` if it's a secret).

## Running Tests

```bash
# Individual endpoint-driven suites against the normal local Compose stack
AKB_URL=http://localhost:8000 bash backend/tests/test_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_edit_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_security_edge_e2e.sh
# … see backend/tests/ for the full list

# CI-equivalent isolated full gate (uses the repository-owned runtime and
# its private PostgreSQL/MinIO dependency stack, not the root Compose app)
uv sync --locked --extra dev --project backend
RUNTIME_ROOT="$(mktemp -d /tmp/akb-e2e-runtime.XXXXXX)"
export AKB_E2E_USERNAME="$(uv run --locked --project backend python -c \
  'import secrets; print(f"akb-e2e-{secrets.token_hex(8)}")')"
export AKB_E2E_PASSWORD="$(uv run --locked --project backend python -c \
  'import secrets; print(secrets.token_urlsafe(24))')"
uv run --locked --project backend python \
  backend/scripts/ci/e2e_runtime.py gate \
  --scenario empty --checkout "$PWD" --runtime-root "$RUNTIME_ROOT"
unset AKB_E2E_USERNAME AKB_E2E_PASSWORD

# Frontend
cd frontend && pnpm test
```

The authenticated MCP pytest scenario is run by the repository-owned runtime
gate and can also consume a ready schema-v2 descriptor directly. Local and
hosted CI use the same pytest entrypoint and the same `akb_list_vaults({})`
assertion:

```bash
uv run --locked --extra dev --project backend python -m pytest \
  backend/tests/mcp_e2e -v --tb=short \
  --confcutdir=backend/tests/mcp_e2e --runtime-descriptor -
```

For a descriptor file, replace `-` with its path:

```bash
uv run --locked --extra dev --project backend python -m pytest \
  backend/tests/mcp_e2e -v --tb=short \
  --confcutdir=backend/tests/mcp_e2e \
  --runtime-descriptor /path/to/descriptor.json
```

The descriptor must come from `backend/scripts/ci/e2e_runtime.py serve` or the
Ubuntu bootstrap. This pytest command consumes the existing runtime; it does
not start or tear down a backend, database, or fixture service.

For an isolated browser session, the same runtime can own the existing Vite
frontend and point its `/api` and `/mcp` proxy at the run's backend:

```bash
(cd frontend && pnpm install --frozen-lockfile)
uv run --locked --project backend python \
  backend/scripts/ci/e2e_runtime.py serve \
  --with-frontend --frontend-port 3000 \
  --scenario empty --checkout "$PWD" --runtime-root "$RUNTIME_ROOT"
```

Use `services.web.origin` from the ready schema-v2 descriptor as
`AKB_FRONTEND_URL` for `cd frontend && pnpm exec playwright test`. The
frontend flag is opt-in so the existing backend/MCP `gate` and `serve` paths
keep their current process and dependency requirements.

The E2E suites create ephemeral users and vaults and clean up after
themselves. They poll `/health` for indexing completion before running search
assertions, so a slow remote embedding endpoint won't cause flakes. An
individual suite targets whatever `AKB_URL` points at; the isolated full gate
starts its own host-process backend/embed/fixture topology. Read the detailed
runtime contract before changing CI or host bootstrap behavior. The supervisor
cleans child processes and dependency resources on exit, but leaves the
private `RUNTIME_ROOT` and its logs for inspection; the caller owns removing
that explicit temporary directory after collecting what it needs.

## Static Analysis Gate

`scripts/check.sh` is the single entry point CI runs (`.github/workflows/check.yml`)
and the one to run before opening a PR. It has two prerequisites it now
checks for itself and refuses to start without — both used to fail as
something other than "you are missing a prerequisite":

**1. Python analyzers on the version this repo requires.** `backend/pyproject.toml`
declares `requires-python = ">=3.14"` and the code uses syntax older
interpreters cannot parse. mypy and bandit build their AST with the
interpreter they run on, so `--python-version` does not help: on an older
one mypy stops after the first file it cannot parse and reports the other
few hundred as unchecked, while bandit skips the file and still exits 0.
Install them against the required version, at the versions CI pins:

```bash
uv tool install --python 3.14 --force 'mypy==2.1.0'
uv tool install --python 3.14 --force 'bandit[toml]==1.9.4'
uv tool install --python 3.14 --force 'ruff==0.15.16'
uv tool install --python 3.14 --force 'detect-secrets==1.5.0'
```

**2. Node deps in all three node projects.** `frontend/`,
`packages/akb-client/` and `packages/akb-mcp-client/` are separate projects
with separate lockfiles and separate `node_modules`, and the gate runs steps in
each. They are not all the same package manager. Installing only some of them
is the common mistake:

```bash
(cd frontend && pnpm install --frozen-lockfile)
(cd packages/akb-client && pnpm install --frozen-lockfile)
(cd packages/akb-mcp-client && npm ci)
```

Then:

```bash
bash scripts/check.sh
```

## Code Style

- **Python**: ruff (configured in `backend/pyproject.toml`, line length 120).
  Run `ruff check backend/app` before submitting.
- **TypeScript / React**: follow the existing patterns; the codebase is
  consistent with React 19 + Radix UI + Tailwind v4 conventions.

## Pull Request Checklist

- [ ] `bash scripts/check.sh` passes (see Static Analysis Gate above).
- [ ] All E2E suites pass against your local stack.
- [ ] Changes to the E2E runtime/bootstrap also pass the isolated full gate;
      see [`backend/scripts/ci/README.md`](backend/scripts/ci/README.md).
- [ ] No secrets, internal hostnames/IPs, or personal info in commits or
      diffs (check `git diff` carefully).
- [ ] New configuration is reflected in `config/*.yaml.example`.
- [ ] User-facing behaviour changes are noted in the PR description.

## Commit Messages

Conventional-commit-ish prefixes are used in the existing history:
`feat:`, `fix:`, `refactor:`, `perf:`, `docs:`, `test:`. Keep the subject
line under 72 characters.

## Reporting Issues

For non-security bugs and feature requests, open an issue. For security
vulnerabilities, follow [SECURITY.md](./SECURITY.md) — do not open a public
issue.
