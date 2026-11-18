# Configure OAuth for AKB MCP clients

Use this guide to enable the AKB tool resource on a Keycloak realm. For an
individual stdio connection, follow the [proxy setup](../../packages/akb-mcp-client/README.md).

## Configure Keycloak

Use a Keycloak realm that AKB trusts. On the reference realm, enable the
OpenID Connect client registration provider. Disable Keycloak's raw and
SAML client registration providers and Device Authorization with these startup options.

```text
--spi-client-registration--default--enabled=false
--spi-client-registration--saml2-entity-descriptor--enabled=false
--features-disabled=device-flow
```

Supply the admin credential in `KC_ADMIN_CLIENT_ID` and
`KC_ADMIN_CLIENT_SECRET`, or use `KC_ADMIN_USER` and `KC_ADMIN_PASS` for an
admin user. Set `KC_ADMIN_REALM` if the credential belongs to a realm other
than `master`. Run the provisioning script:

```bash
python3 scripts/keycloak/setup-akb-mcp-oauth.py \
  --kc https://auth.example.com \
  --realm akb \
  --audience https://akb.example.com/mcp
```

The script provisions the Vault scopes for the tool resource and the native
client registration policy. Keep those scopes off the realm's default and
default-optional scope lists. Keycloak assigns them to clients admitted by the
registration policy. The scopes carry the signed tool admission marker and
the canonical resource audience.

The reference policy admits public authorization-code clients with S256 PKCE.
The native redirect is `http://127.0.0.1/oauth/callback` in registration
metadata. Keycloak permits a dynamic loopback port on the authorization
request. Enable refresh-token rotation and restrict alternate grants according
to the provisioned client policy. The CLI requests ordinary refresh tokens.

## Configure AKB

Use the browser `auth_mode` that your installation already uses. Set the
Keycloak issuer and canonical AKB base URL in `config/app.yaml`.

```yaml
auth_mode: local
keycloak_enabled: true
keycloak_server_url: "https://auth.example.com"
keycloak_internal_url: "https://auth.example.com"
keycloak_realm: "akb"
mcp_oauth_enabled: true
public_base_url: "https://akb.example.com"
```

The same tool resource works with `auth_mode: sso`. Users need an admitted
Keycloak identity and the appropriate AKB Vault access in either mode.
PAT-only deployments can leave `mcp_oauth_enabled` off.

Use the exact same base URL when you configure Keycloak's audience.
If the base contains a prefix, retain it in the audience and client setup.
For example, a base of `https://akb.example.com/workspace` has a resource of
`https://akb.example.com/workspace/mcp`. Check its metadata at
`/.well-known/oauth-protected-resource/workspace/mcp`. The well-known path is
relative to the origin. Route that path to AKB when you configure the ingress.

Check the authorization server's advertised configuration with
`curl -s https://akb.example.com/health | jq .mcp_oauth`. Validate policy and
token issuance by completing a browser login against the deployment.

## Connect a direct HTTP client

Configure the direct client's redirect URI in Keycloak. Assign only the Vault
scopes that the client needs. Assigning a scope provisioned by the script
also grants the signed tool admission marker. For a direct client that uses
its own scope mappers, set its exact client ID in `mcp_oauth_client_ids` in
AKB. Every tool access token must carry the exact single MCP resource audience
and a verified human identity.

Use `https://akb.example.com/mcp/` as the HTTP transport endpoint. Configure
the client with that registration and use its OAuth sign-in flow. A
cloud-hosted connector needs network access to both AKB and the authorization
server. If the client requires its own dynamic registration, the operator
must authorize that client's registration and callback in the realm.

[OAuth tool resource and scope reference](../designs/mcp-oauth-dcr/00-overview.md)
