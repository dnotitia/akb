# Security Policy

## Reporting a Vulnerability

If you've found a security issue in AKB, please report it privately — do
**not** open a public GitHub issue.

Email **support@dnotitia.com** with subject prefix `[SECURITY]` and the following:

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
  development credentials or exposing private signing keys).

## Hardening Checklist for Operators

When deploying AKB beyond local development:

- [ ] Generate and persist installation-owned RS256 local-session keys; keep
      the private PEM out of source control and restrict access to it. Follow
      the [local JWT migration and rotation guide](README.md#local-jwt-migration-and-rotation)
      when replacing keys, retaining prior public JWKs for an overlap window.
- [ ] Set independent strong values for `system_hmac_secret` and other
      configured capability/session secrets in `secret.yaml` (for example,
      `openssl rand -hex 32` for each). `jwt_secret` is legacy migration input
      for non-session HMAC capabilities; changing it does not rotate RS256
      human-session signing keys.
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
