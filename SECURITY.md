# Security Policy

## Reporting a Vulnerability

If you've found a security issue in AKB, please report it privately — do
**not** open a public GitHub issue.

Email **security@dnotitia.com** with:

- A description of the issue and its impact.
- Steps to reproduce (proof-of-concept code is welcome).
- Affected versions / commit hashes if known.
- Your name/handle for attribution (optional).

We aim to respond within 5 business days and will keep you updated through
the disclosure timeline.

## Scope

In scope:

- Backend service (`backend/`) — authentication, authorisation, SQL
  injection, SSRF, file traversal, secrets handling.
- MCP proxy (`packages/akb-mcp-client/`) — stdio ↔ HTTP bridging.
- Default configuration in `config/*.yaml.example` and
  `docker-compose.yaml`.
- Container images built from this repository.

Out of scope:

- Third-party dependencies (please report upstream — but feel free to
  notify us so we can update pins).
- Self-hosted deployments where the operator has materially changed the
  configuration in ways that weaken security (e.g. running with the default
  `jwt_secret`).

## Hardening Checklist for Operators

When deploying AKB beyond local development:

- [ ] Rotate `jwt_secret` to a strong random value (`openssl rand -hex 32`).
- [ ] Set strong `db_password`; never reuse the docker-compose default.
- [ ] If using Qdrant with an API key, set `vector_api_key`.
- [ ] Restrict MCP `/mcp/` and REST `/api/` endpoints to authenticated
      callers (the default; do not disable PAT/JWT checks).
- [ ] Place AKB behind TLS (an Ingress / reverse proxy) — backend serves
      plain HTTP by design.
- [ ] Keep `secret.yaml` out of source control (the default `.gitignore`
      enforces this; verify in any fork).

## Disclosure

Once a fix is available, we coordinate a release and credit the reporter
unless they prefer to remain anonymous.
