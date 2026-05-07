# Contributing to AKB

Thanks for considering a contribution. AKB is licensed under
[PolyForm Noncommercial 1.0](./LICENSE) — by submitting a contribution you
agree that it is licensed under the same terms.

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
# Backend E2E (requires the docker compose stack up)
AKB_URL=http://localhost:8000 bash backend/tests/test_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_edit_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_security_edge_e2e.sh
# … see backend/tests/ for the full list

# Frontend
cd frontend && pnpm test
```

The E2E suites create ephemeral users and vaults and clean up after
themselves. They poll `/health` for indexing completion before running search
assertions, so a slow remote embedding endpoint won't cause flakes.

## Code Style

- **Python**: ruff (configured in `backend/pyproject.toml`, line length 120).
  Run `ruff check backend/app` before submitting.
- **TypeScript / React**: follow the existing patterns; the codebase is
  consistent with React 19 + Radix UI + Tailwind v4 conventions.

## Pull Request Checklist

- [ ] All E2E suites pass against your local stack.
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
