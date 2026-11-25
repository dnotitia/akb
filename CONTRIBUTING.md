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

The example `config/app.yaml.example` is a template for a new Native
installation. The API requires identity fields that the template leaves blank.
The repository-owned E2E runtime exercises Bare Git. To use Bare Git in your
development stack, set `document_revision_backend` to `bare_git`.

```bash
# 1. Configure
cp config/app.yaml.example   config/app.yaml
cp config/secret.yaml.example config/secret.yaml
# Pin the revision backend for this Compose development stack.
sed 's/^document_revision_backend: .*/document_revision_backend: bare_git/' config/app.yaml > config/app.yaml.tmp
mv config/app.yaml.tmp config/app.yaml
$EDITOR config/app.yaml
$EDITOR config/secret.yaml   # at minimum, set embed_api_key

# 2. Generate persistent RS256 signing keys once (never on each restart).
# config/app.yaml.example already points to /etc/akb/local-session/*.
docker compose build backend
docker compose run --rm --no-deps -v "$PWD/config:/etc/akb" backend \
  python -m app.cli generate-local-session-keyset \
  --output-dir /etc/akb/local-session

# 3. Run the stack
docker compose up -d

# 4. Tail backend logs
docker compose logs -f backend
```

To develop against PostgreSQL Native instead, follow the
[root README quickstart](./README.md#quick-start): prepare persistent identity
with `prepare-native-config` and layer `docker-compose.native.yaml`. Never point
that overlay at a Compose project whose database already holds Bare Git data.

Backend code lives in `backend/app/`; the MCP server in `backend/mcp_server/`;
the frontend in `frontend/`. The stdio MCP proxy that ships on npm lives
under `packages/akb-mcp-client/`.

The local Compose path and the repository-owned E2E runtime are separate
development paths. See [`scripts/ci/README.md`](scripts/ci/README.md)
for the suite, runtime, and clean-host bootstrap contracts.

## Configuration

The backend reads exactly two YAML files — `app.yaml` (non-secret) and
`secret.yaml` (gitignored) — from `./config/` or `/etc/akb/`. **No environment
variables configure application settings in `app/config.py`.** Deployment-layer
exceptions include PostgreSQL pool sizing (`AKB_PG_POOL_MIN_SIZE` /
`AKB_PG_POOL_MAX_SIZE`), the optional stats listener (`AKB_STATS_PORT`), and
process composition (`AKB_PROCESS_ROLE`, `AKB_TOKENIZER_PROCESSES`). When you
need a new application setting:

1. Add the field with a sensible default to `Settings` in
   `backend/app/config.py`.
2. Add the same key (with explanatory comment) to
   `config/app.yaml.example` (or `secret.yaml.example` if it's a secret).

## Running Tests

From the repository root, run the suite for the boundary you changed.

| Change | Tests |
| --- | --- |
| Backend code without live dependencies | Backend unit pytest selection |
| REST or MCP behavior that needs PostgreSQL | Live PostgreSQL pytest selection, including `backend/tests/rest_integration/` |
| Backend behavior that needs a running API or storage service | Runtime gate with `--suite backend` |
| Live MCP protocol or proxy behavior | Runtime gate with `--suite mcp` |
| A browser flow connected to AKB | Runtime gate with `--suite frontend` |

Use the [backend pytest commands](scripts/ci/README.md#backend-pytest-jobs) for
unit and integration tests. For live checks, the runtime starts a disposable
AKB instance and cleans up its processes and dependencies when the suite ends.
Follow the [runtime setup and gate commands](scripts/ci/README.md#run-a-live-suite).

The frontend also has unit tests and browser specs that provide their own HTTP
fixtures. Once the frontend dependencies and Playwright Chromium are installed,
run these from the repository root.

```bash
(cd frontend && pnpm test)
(cd frontend && pnpm run test:e2e:fixture)
```

Use `serve` when you need a running backend for a focused SDK test or browser
session. See [how to keep a runtime running](scripts/ci/README.md#keep-a-runtime-running).

## Static Analysis Gate

Run `bash scripts/check.sh` before opening a PR. It includes the frontend
app's Vitest suite. In CI, the **Frontend unit and component tests (Vitest)**
job runs that suite. The **Static analysis and package tests** job runs the
script with `--skip-frontend-vitest` and still tests the SDK, Markdown
editor, and stdio proxy packages.

The script checks two prerequisites before running the gate.

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

**2. Node deps in the workspace and independent projects.** `frontend/` and
`frontend/packages/markdown-editor/` are the two members of the frontend pnpm
workspace;
`packages/akb-client/` and `packages/akb-mcp-client/` remain independent
projects. The workspace uses `frontend/pnpm-lock.yaml`; each independent
project uses its own lockfile, and the gate runs steps in each. Installing only
some of them is the common mistake:

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
      see [`scripts/ci/README.md`](scripts/ci/README.md).
- [ ] No secrets, internal hostnames/IPs, or personal info in commits or
      diffs (check `git diff` carefully).
- [ ] New configuration is reflected in `config/*.yaml.example`.
- [ ] User-facing behaviour changes are noted in the PR description.

## Public Documentation

Keep architecture, API contracts, decision rationale, and useful historical
records public. Mark historical results and superseded proposals with their
scope and status so readers can distinguish them from current instructions.

Before publishing documentation:

- Remove operator-specific runtime/usage snapshots, personal filesystem paths,
  and private ticket or repository references that readers cannot use.
- Verify commands and tool arguments against the current implementation and
  check relative links. Separate archived recipes from supported installation
  instructions; record the revision for reproducible historical results.
- Prefer small synthetic examples with public source fixtures over examples
  that depend on private vaults, customer data, or operational credentials.
- Describe licensing accurately: the backend is BSL **source-available**;
  `akb-mcp` is MIT-licensed. See [License](README.md#license).

## Commit Messages

Conventional-commit-ish prefixes are used in the existing history:
`feat:`, `fix:`, `refactor:`, `perf:`, `docs:`, `test:`. Keep the subject
line under 72 characters.

## Reporting Issues

For non-security bugs and feature requests, open an issue. For security
vulnerabilities, follow [SECURITY.md](./SECURITY.md) — do not open a public
issue.
