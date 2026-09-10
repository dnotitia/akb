# Home and AI connection onboarding

Status: implementation under review

Date: 2026-09-09

## Problem

Home should help people reopen knowledge, choose a Vault, and review changes.
Its navigation already identifies the page. Repeating a large Home masthead,
fixed-height sparse cards, and onboarding panels competing with content adds
visual weight without helping a decision.

The existing connection journey also diverged between Home and Settings:

- Token creation preceded client and authentication selection, even for OAuth.
- Settings rendered a token prefix plus an ellipsis inside copyable configuration.
  A prefix is an identifier, not a usable credential.
- Setup could be collapsed based on an unrelated browser preference, while
  Home redirected existing-token users to that hidden guide.
- Examples assumed particular company Vaults and unsupported task semantics.
- Copy controls could report success when the Clipboard API was absent.
- Token activity did not prove that any particular external client was connected.

## Reference synthesis

These are interaction references, not feature-parity promises.

| Reference | Observed pattern | AKB adaptation |
| --- | --- | --- |
| [Notion connection UI](https://www.notion.com/releases/2025-08-19) | Recognizable AI tools and explicit connection entry points | Start with tool choice rather than a token-management form |
| [Notion MCP setup](https://developers.notion.com/guides/mcp/get-started-with-mcp) | Client-specific installation, authorization, and troubleshooting | Show only the selected client's configuration and supported auth path |
| [Atlassian MCP setup](https://support.atlassian.com/atlassian-ai-gateway/docs/get-started-with-the-atlassian-remote-mcp-server/) | Separate client setup from authorization; explain existing permissions | Explain authorization before token creation; retain user's Vault permissions |
| [GitHub Copilot MCP](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers) | Separate adding, managing, and using servers | Separate connection setup from token lifecycle management |

The frontend-design guidance favors task-specific language over decorative
headlines. UI/UX Pro Max's onboarding guidance favors user freedom: connection
is optional, can be dismissed, and remains available later. Existing AKB tokens,
Pretendard typography, focus patterns, and neutral surfaces remain authoritative.

## Chosen flow

Home connection action or Settings setup action
→ shared tool selection
→ supported authentication method
→ selected configuration
→ read-only request in the external client.

No new connector marketplace or backend connection registry is introduced.
No OAuth option is offered unless advertised by this deployment and supported
by its existing snippet catalog. Existing full PATs can be reused; lost token
secrets cannot be recovered from the token list. New credentials are created
only on explicit submission, never on opening a guide or switching tools.

Home retains optional guidance; both new and existing-token users open the same
setup instead of taking different paths. Settings separately exposes credential
reissue and revoke, with accurate failure messaging. Copied configurations are
not evidence of a successful connection. External-agent verification uses a
read-only prompt and a clear statement that this browser cannot verify the
external client. PAT last-use means credential activity only.

## Density

- No duplicate body masthead. A compact location label stays in the app header;
  a screen-reader H1 retains document heading hierarchy.
- Recent history is bounded to four; one result becomes a compact full-width row.
- Four-column Vault preview remains, without a forced minimum card height.
- Section gaps are 28–32px; section-to-content gaps are 12px.
- Update rows use 12px vertical padding, readable 14px titles, 12px metadata,
  and explicit separation between location and update provenance.
- Connection setup is one shared form, not nested cards for every step.
- Token rows are a divided ledger; lifecycle actions do not require another
  full-width action band within every token card.
- Code remains 12px and selectable; density must not make configuration illegible.

## Related defect

The local frontend proxy matched `/health` but not `/health/vault/{name}`.
Authenticated per-Vault health requests received SPA HTML rather than JSON,
causing the global indexing-unavailable notice. Route both health paths to the
backend without bypassing backend authorization. Align the all-in-one proxy
and add a route-matching regression test. Kubernetes manifests are unchanged.

## Boundaries and acceptance

Preserve existing backend contracts and authorization. Never invent zero counts,
connection success, OAuth support, or missing document summaries. Validate
Home sparse/full/empty states, light/dark and mobile/desktop, shared setup,
token-vs-OAuth branches, copy failure, repeated submit, and secret dismissal.
Real third-party OAuth authorization is not automated against user accounts;
client configuration and UI branches can be tested with isolated fixtures.
