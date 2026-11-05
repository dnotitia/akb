# Tool OAuth resource

Status is current. This contract replaces the earlier MCP-only OAuth and
anonymous DCR design. The separate browser API, administrator, service, PAT,
and per-file capability contracts remain active.

## One resource and two credential methods

For canonical installation base B, including its deployment prefix, the tool
resource is exactly `B/mcp`. OAuth access tokens must carry that signed
audience as a string or singleton array. Combined and duplicate audiences are
refused. Historical audience overrides cannot name another resource.

`keycloak-tool-resource-v1` verifies the configured issuer, pinned JWKS and
RS256 signature, access-token kind, human subject/session claims, expiry,
exact audience, and an admitted authorized party before account projection.
The native client's `azp` equals `mcp_oauth_native_client_id`. Other direct
clients require an exact entry in `mcp_oauth_direct_client_ids`. Arbitrary
same-realm clients and service accounts are refused.

The existing issuer/subject projector resolves an active human account.
Matching email does not link a local account. Database administrator authority
and existing MCP/SQL business permissions remain unchanged.

PAT mode continues to use token-store scope, revocation, and Vault checks.
It does not require OAuth metadata or an authorization server. Selecting PAT
or OAuth never enables fallback to the other credential method.

## Explicit route authority

OAuth tool credentials authorize MCP and exactly these installation-relative
REST controls. Each dependency checks scope before protected body consumption
or file service execution.

| Method and path | Required scope |
| --- | --- |
| `POST /api/v1/files/{vault}/upload` | `akb:vault:write` |
| `POST /api/v1/files/{vault}/{file_id}/confirm` | `akb:vault:write` |
| `POST /api/v1/files/{vault}/{file_id}/replace` | `akb:vault:write` |
| `POST /api/v1/files/{vault}/{file_id}/replace/{replacement_id}/confirm` | `akb:vault:write` |
| `GET /api/v1/files/{vault}/{file_id}/download` | `akb:vault:read` |
| `DELETE /api/v1/files/{vault}/{file_id}` | `akb:vault:write` |
| `POST /api/v1/assets/{vault}` | `akb:vault:write` |
| `DELETE /api/v1/assets/{vault}/{file_id}` | `akb:vault:write` |

The dependency selects one verifier. An unverified audience containing the
tool resource selects the strict tool verifier, which independently proves
that audience. A mixed audience fails there. Any invalid selected bearer is
final and never falls through to a browser cookie or another verifier.
Existing local, API, PAT, and delegated-service credentials retain their
existing checks on these shared controls.

General document, table, account, token-management, browser-session,
administrator, and dedicated service APIs do not admit this profile. Nearby
file listing, metadata, cancel-upload, body-placement, image-policy, attachment
copy, and stable asset routes retain their existing credential contracts.
A general OAuth bearer never substitutes for a per-file byte capability.

## Shared scope and catalog policy

`app.services.tool_resource_policy` owns the complete MCP and eight-control
operation registry. Unclassified operations are refused. Read and write are
independent. `akb_grep` with a non-null `replace`, including an empty string,
requires write scope. MCP protocol discovery and catalogs require at least
one admitted AKB scope in both transport adapters.

`mcp_server.tool_catalog` composes the exact write acknowledgement schema for
both live catalogs and generated catalogs. The generator reads complete
canonical `TOOLS`, including optional creation arguments. Installation-specific
live refinements do not determine the initial public grammar.

```bash
uv run --locked --project backend python scripts/generate_mcp_catalog.py \
  --output /tmp/akb-tools.json --refinements-output /tmp/akb-tool-refinements.json
uv run --locked --project backend python scripts/generate_mcp_catalog.py \
  --output /tmp/akb-tools.json --refinements-output /tmp/akb-tool-refinements.json --check
```

Generation refuses duplicate names, missing operation classifications,
mutating-argument drift, and reserved acknowledgement collisions. The proxy
adds its local file and connection tools exactly once. Catalog visibility
confers no Vault or data authority.

## Discovery and native registration

Protected-resource metadata uses RFC 9728 path insertion. For
`https://akb.example.com/prefix/mcp`, its URL is
`https://akb.example.com/.well-known/oauth-protected-resource/prefix/mcp`.
The challenge and public endpoint name that same resource-specific address.
`akb_native_client_id` publishes the pre-registered public native client ID.

The authorization server owns code, consent, token issuance, and refresh.
The proxy owns PKCE and the exact dynamic-port loopback callback. Refresh
rotation does not require requesting `offline_access`. Client-local audience
mapping is explicit; the `resource` authorization parameter alone does not
configure Keycloak's signed audience.

See [Connect an MCP client with OAuth](../../mcp-clients/web-connectors.md)
for provisioning, public reverse-proxy routing, and both authentication modes.
