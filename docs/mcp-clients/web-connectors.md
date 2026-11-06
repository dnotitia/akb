# Connect an MCP client with OAuth

AKB supports two selected authentication methods for its stdio proxy. A PAT
works without an authorization server. A URL-only configuration selects OAuth
and uses the installation's pre-registered public native client.

This guide configures OAuth for the proxy and explicitly approved direct HTTP
clients. Browser login still follows `auth_mode`. OAuth tool access works with
either local or SSO browser login.

## Provision the native client

Set one temporary Keycloak administrator credential pair in your shell.
Use `KC_ADMIN_CLIENT_ID` and `KC_ADMIN_CLIENT_SECRET` for a bootstrap service
account, or `KC_ADMIN_USER` and `KC_ADMIN_PASS` for an administrator user.
`KC_ADMIN_REALM` defaults to `master` and identifies the credential's realm.

Run the operator script against the realm that issues AKB access tokens.

```bash
python3 scripts/keycloak/setup-akb-mcp-oauth.py \
  --kc https://auth.example.com \
  --realm akb \
  --audience https://akb.example.com/prefix/mcp \
  --native-client-id akb-mcp-native
```

The script creates or updates a public client with Authorization Code, S256
PKCE, refresh tokens, and exactly `http://127.0.0.1/oauth/callback` as its
registered redirect. Keycloak's loopback redirect rule admits an ephemeral
port while retaining the exact IP and callback path. The proxy binds that port
before opening the system browser. No wildcard path or client secret is used.
The script disables implicit, password, and service-account grants for this
client. It assigns `basic`, `profile`, and `email` as default client scopes and
the two AKB scopes as optional client scopes. The realm's `basic` scope must
contain an `oidc-sub-mapper` with `access.token.claim` enabled so access tokens
identify the human subject. The script fails before changing the realm if
this required subject configuration is missing. It removes other client scope
assignments, including `roles`, and installs a client-local audience mapper for
the exact resource.

The script enables realm-wide refresh-token revocation with maximum reuse 0.
This affects every client in that realm. It does not request `offline_access`
or open dynamic registration policies. It removes the old setup script's
named scope-level audience mapper before installing the native client's mapper.
It verifies the registration, scope associations, mapper, and rotation settings
by reading them back. Re-running it converges on the same configuration.

The variable-port rule is implemented by
[Keycloak RedirectUtils](https://github.com/keycloak/keycloak/blob/26.4.2/services/src/main/java/org/keycloak/protocol/oidc/utils/RedirectUtils.java).
Validate authorization and refresh against your deployed Keycloak version.
An administrator readback cannot prove the signed audience or token rotation.

## Configure AKB

Set these non-secret fields in `config/app.yaml`.

```yaml
public_base_url: https://akb.example.com/prefix
mcp_oauth_enabled: true
mcp_oauth_native_client_id: akb-mcp-native
mcp_oauth_direct_client_ids: []
keycloak_enabled: true
keycloak_server_url: https://auth.example.com
keycloak_realm: akb
```

Keep the installation's existing `auth_mode` and identity/enrollment policy.
Local browser login does not require a browser OIDC client merely to enable
tool OAuth. SSO browser login retains its separate client and API audience.

The resource and signed access-token audience are exactly
`https://akb.example.com/prefix/mcp`. Leave `mcp_oauth_audience` empty. A supplied
value must equal that canonical resource; divergent historical overrides fail
configuration. A string audience or a singleton array is accepted. Additional,
duplicate, browser, administrator, or service audiences are refused.

## Publish path-specific discovery

For the example above, publish these paths on the same public origin.

| Public URL | Backend path after proxy routing |
| --- | --- |
| `https://akb.example.com/prefix/mcp` | `/mcp` |
| `https://akb.example.com/prefix/api/v1/files/...` | `/api/v1/files/...` |
| `https://akb.example.com/prefix/api/v1/assets/...` | `/api/v1/assets/...` |
| `https://akb.example.com/.well-known/oauth-protected-resource/prefix/mcp` | `/.well-known/oauth-protected-resource/prefix/mcp` |

Route the origin-root `/.well-known/oauth-protected-resource/` prefix to AKB
without removing the resource path after it. The installation prefix belongs
inside the well-known suffix. Publishing only
`/prefix/.well-known/oauth-protected-resource` does not implement RFC 9728.
AKB's unauthenticated challenge names the same path-specific metadata URL.
No root metadata alias is provided.

Check the actual public route.

```bash
curl --fail https://akb.example.com/.well-known/oauth-protected-resource/prefix/mcp
```

The document must contain the exact `resource`, one `authorization_servers`
issuer, the two AKB scopes, and `akb_native_client_id: "akb-mcp-native"`.
It does not advertise `offline_access`. A metadata request for another
resource path returns 404. Metadata is disabled when tool OAuth is disabled.
`/health` reports publicly observable discovery capabilities, but explicitly
does not verify the native registration.

## Connect the stdio proxy

Configure the host to run `npx -y akb-mcp --url` with the canonical MCP URL.
OAuth requires no installation-specific argument beyond that URL.

```json
{
  "mcpServers": {
    "akb": {
      "command": "npx",
      "args": ["-y", "akb-mcp", "--url", "https://akb.example.com/prefix/mcp"]
    }
  }
}
```

The proxy exposes its complete tool catalog immediately. First startup opens
the browser after the local protocol handshake. A call during sign-in returns
`execution: "not_started"`. Submit a new call after authorization succeeds.
Pending or rejected calls never execute later. Inspect the local
`akb_connection` tool for authentication and connectivity status.

PAT mode remains supported through `--pat` or `AKB_PAT`, with `--url` or
`AKB_MCP_URL`. It does not discover OAuth, open a browser, or use the OAuth
credential store. Replace a rejected PAT explicitly. Neither selected method
falls back to the other.

## Admit a direct HTTP client

Register each direct client separately at the authorization server. Give it
an audience mapper for this exact resource, human claims, and the desired AKB
scopes. Add its exact client ID to `mcp_oauth_direct_client_ids`. A dynamically
registered client is refused until an operator explicitly adds its ID.
The setup script does not enable anonymous registration or negotiate DCR.
Browser, companion API, administrator, and service clients remain separate.

Both supported MCP protocol adapters use the same tool authentication and
scope policy. Discovery and catalogs require at least one admitted AKB scope.
Read and write are independent; write does not imply read. Existing Vault,
document, writer, ownership, delegation, integrity, and concurrency checks
still apply. See the [tool-resource contract](../designs/mcp-oauth-dcr/00-overview.md)
for the eight admitted file/image controls and excluded routes.
