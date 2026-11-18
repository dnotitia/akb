# OAuth tool resource

AKB is an OAuth resource server. The authorization server handles client
registration, authorization, consent, and token issuance. AKB validates the
resulting token and applies workspace authorization. The human browser API
keeps its own credential profile. Either `local` or `sso` browser login can
run alongside tool OAuth.

## Resource and authorization

The canonical resource is `<public_base_url>/mcp` without a trailing slash.
The HTTP adapter accepts the MCP protocol at the installation's `/mcp/`
endpoint. For a prefixed installation such as
`https://akb.example.com/workspace`, the OAuth audience is
`https://akb.example.com/workspace/mcp`. Its path-aware metadata document is
at `/.well-known/oauth-protected-resource/workspace/mcp`.

AKB validates the token signature with the configured issuer's keys and checks
the access token kind, lifetime, subject, authorized party, and exact single
audience. The realm must admit the authorized party through its DCR policy or
the operator must select the direct HTTP client explicitly. User identity comes
from a verified issuer and subject. The principal retains its existing Vault
and administrator authority.

| Operation | Required scope |
|---|---|
| MCP discovery and catalog | At least one Vault scope |
| MCP reads and file download capability | `akb:vault:read` |
| MCP writes, file upload and replacement reservation or confirmation, file deletion, image upload and discard | `akb:vault:write` |

The read and write scopes grant independent permissions. MCP classifies
operations and checks arguments that can turn a read into a write. Vault and
document access checks run after scope checks. Account, browser, administrator,
service, and other REST operations keep separate credential profiles. File
byte transfers use the capability issued for that transfer.

## Stdio sign-in and credential ownership

The explicit `akb-mcp login` command discovers the protected resource and its
issuer. It registers a public client through DCR, runs Authorization Code with
S256 PKCE, and stores the registration and grant. Login uses a loopback
listener for one bounded browser attempt. The listener checks the callback
path, issuer, state, and request origin before passing the code to the exchange.

Stdio reads the saved grant and refreshes it when needed. Both MCP and the
proxy's file controls use the same access-token generation. SQLite gives one
local process ownership of login or refresh for each installation. The owner
writes refresh intent before it sends the refresh token. A completed exchange
publishes the replacement grant after saving it durably. An interrupted
rotation can require a separate login.

Each tool invocation submits its business requests once. An interrupted file
workflow reports its last completed stage and available file or replacement
identifiers. The proxy uses a separate request for byte transfer and sends
only the transfer capability on that request.

[Configure users and operator settings](../../mcp-clients/web-connectors.md).
[Connect a stdio host](../../../packages/akb-mcp-client/README.md).
