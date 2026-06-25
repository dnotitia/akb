# Connecting AKB to a Web LLM Client (OAuth + DCR)

This guide is for connecting AKB to an MCP client that speaks the
**MCP OAuth Resource Server path** — Claude Code (HTTP transport),
claude.ai Custom Connectors, and ChatGPT Custom Connectors. For
stdio-based clients (Claude Desktop, Codex CLI via `akb-mcp`,
Claude Code via the stdio proxy), see the project README: the PAT
flow there is unchanged.

## What you need

| Piece | Why |
|---|---|
| AKB backend ≥ the version that ships `mcp_oauth_enabled` | Adds the Resource Server code path |
| An OIDC IdP — Keycloak in the reference deployment | AKB does NOT host an Authorization Server; the IdP issues access tokens |
| Realm configuration: `akb:vault:read`, `akb:vault:write` scopes (each with an audience mapper) + DCR trusted hosts + `offline_access` exposed | Lets clients DCR-register and request the scopes the consent screen presents |
| `config/app.yaml`: `mcp_oauth_enabled: true`, a non-empty `public_base_url` | Activates the new path. Default keeps it off so PAT-only deployments are bit-for-bit unchanged |

## Configure the IdP (one-shot)

Either run the realm setup script (recommended) or hand-edit the realm
in the Keycloak admin console. The script is idempotent — re-running
it is a no-op.

```bash
KC_ADMIN_USER=admin KC_ADMIN_PASS=<...> \
    python3 scripts/keycloak/setup-akb-mcp-oauth.py \
        --kc https://auth.example.com \
        --realm akb \
        --audience https://akb.example.com/mcp
```

It will:

1. Add `localhost`, `127.0.0.1` to the realm's DCR `trusted-hosts`
   policy (so Claude Code on the operator's laptop can DCR-register).
2. Create `akb:vault:read` and `akb:vault:write` scopes with audience
   mappers pinned to the AKB `/mcp` URL.
3. Add both scopes to `defaultOptionalClientScopes` so DCR clients can
   request them.

If the realm already had any of the above, those steps no-op.

### Hand-edit checklist (if you skip the script)

In the Keycloak admin console, in your realm:

- **Realm Settings → Client Registration → Policies**: open `Trusted
  Hosts` and add `localhost`, `127.0.0.1` to the trusted hosts list.
- **Client Scopes → Create**: add `akb:vault:read` and `akb:vault:write`
  with the listed display + consent text. Add an `oidc-audience-mapper`
  protocol mapper to each, with `Included Custom Audience` set to your
  AKB `/mcp` URL (e.g. `https://akb.example.com/mcp`).
- **Realm Settings → Client Scopes → Default Client Scopes** (Optional
  column): assign both new scopes as Optional.

## Configure the backend

In `config/app.yaml`:

```yaml
# Existing SSO settings (required — MCP OAuth piggybacks on the JWKS
# config). If your deployment already has SSO on, leave these as-is.
keycloak_enabled: true
keycloak_server_url: "https://auth.example.com"      # browser-facing → token `iss`
keycloak_internal_url: "https://auth.example.com"    # backend → IdP backchannel
keycloak_realm: "akb"

# The new MCP OAuth path
mcp_oauth_enabled: true
public_base_url: "https://akb.example.com"
# mcp_oauth_audience defaults to <public_base_url>/mcp — only set this
# if the MCP endpoint lives on a different hostname than public_base_url.
```

Restart the backend. Sanity check the metadata document:

```bash
curl https://akb.example.com/.well-known/oauth-protected-resource
```

Should return JSON with `resource`, `authorization_servers`, and
`scopes_supported` (including `akb:vault:read`, `akb:vault:write`,
`offline_access`).

## Add AKB to Claude Code

```bash
claude mcp add --transport http akb https://akb.example.com/mcp
```

The command itself does **not** open a browser. `claude mcp list` will
show:

```
akb   ! Needs authentication
```

Complete OAuth one of two ways:

- **Up-front, before the session**:
  ```bash
  claude mcp login akb
  ```
  (requires Claude Code ≥ v2.1.186). A browser opens, prompts you to
  log in to Keycloak, presents the consent screen, and stores the
  resulting tokens on your machine (macOS keychain, or
  `~/.claude/mcp-credentials` on other platforms).

- **Lazily, inside a session**: run `/mcp` in the session, select `akb`,
  and choose `Authenticate`. A browser opens for the same flow.

Once authenticated, Claude Code calls `https://akb.example.com/mcp`
with `Authorization: Bearer <access_token>`. The access token is
auto-refreshed silently as long as the IdP advertises `offline_access`
in its `scopes_supported` — which the AKB realm does.

If the IdP fails to advertise `offline_access`, tokens expire (default
Keycloak access TTL is 5 minutes) and Claude Code re-opens the browser
mid-session. Verify with:
```bash
curl https://auth.example.com/realms/akb/.well-known/openid-configuration \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print("scopes_supported:", d.get("scopes_supported"))'
```

## Add AKB to claude.ai or ChatGPT (Custom Connector)

For claude.ai / ChatGPT, AKB must be reachable from the cloud LLM's
egress — i.e. on a public hostname with TLS. If your deployment is
internal-only, see the next section.

Settings → Connectors → *Add custom connector* → URL
`https://akb.example.com/mcp`. The client auto-discovers
`/.well-known/oauth-protected-resource`, DCR-registers itself against
Keycloak, and runs the consent flow on the next user interaction.

## Internal-only deployments

If AKB is not reachable from the public internet (which is the
Dnotitia reference deployment), Claude Code is the right client:
the OAuth flow runs entirely between **your laptop** ↔ **AKB backend
on the internal network** ↔ **Keycloak on the internal network**.
No tunnel, no public hostname. The browser that opens for consent
runs on the same laptop, so it can reach the internal Keycloak just
like the rest of your session.

claude.ai and ChatGPT Custom Connectors cannot reach an internal-only
backend — the discovery call originates from their cloud. Use Claude
Code (or a stdio client + PAT) for those deployments.

## Troubleshooting

### Claude Code shows "Needs authentication" but `claude mcp login` says "Incompatible auth server"

The realm does not have DCR enabled, or the requesting host is not in
the `trusted-hosts` policy. Re-run `setup-akb-mcp-oauth.py` (or check
that step in the hand-edit checklist).

### Tokens expire and the browser keeps reopening mid-session

The realm is not advertising `offline_access`. Check
`/.well-known/openid-configuration` on the IdP — `scopes_supported`
must include `offline_access`. Keycloak ships it by default; if it is
missing, your realm export removed it.

### `/.well-known/oauth-protected-resource` returns 404

`mcp_oauth_enabled` is not true in the running backend's
`config/app.yaml`. Check `curl https://akb.example.com/readyz` first
to confirm you are hitting the right deployment, then check the
ConfigMap / app.yaml the live backend is reading.

### `/mcp` returns 401 with an OAuth token that worked yesterday

Audience mismatch — the token was minted with `aud` baked from the
scope-level mapper. If you changed `public_base_url` (or
`mcp_oauth_audience`) without re-running `setup-akb-mcp-oauth.py`,
the realm still emits the old audience and the backend rejects it.
Re-run the setup script with the new `--audience` and have users
`claude mcp logout akb && claude mcp login akb` to get a token with
the corrected `aud`.

## How the pieces fit (recap)

```
[Claude Code]                [AKB backend]               [Keycloak]

claude mcp add http://akb/mcp
                       (saves config; no browser)

claude mcp login akb
  ────────── GET /.well-known/oauth-protected-resource ─→
  ←──────────── { authorization_servers: [...] } ──────────
  ─────────────── GET .../openid-configuration ─────────────────→
  ←─────────── { registration_endpoint, ... } ────────────────────
  ──────────────────── POST .../clients-registrations (DCR) ────→
  ←───────────────────── { client_id: ... } ───────────────────────
  [browser opens]
                                                     /authorize?…
                                                     [user logs in + consents]
  ────────────────────────── POST .../token (code + PKCE) ─────→
  ←─────────────── { access_token, refresh_token } ────────────────
                       (tokens stored locally)

claude > "list my AKB vaults"
  ───────────── POST http://akb/mcp  Bearer <access_token> ────→
                                  verify_access_token (JWKS, aud, iss, exp)
                                  resolve_or_provision_keycloak_user
                                  scope check (akb:vault:read)
                                  dispatch akb_list_vaults
                              ←──── { vaults: [...] } ────
```
