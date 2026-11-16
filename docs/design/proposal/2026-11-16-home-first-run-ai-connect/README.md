# Home first-run guide: connect an AI app, then the first Vault

Status: proposal — implemented; pending review
Date: 2026-11-16

## Problem

A person who signs up through SSO arrives on Home with no Vaults. Until now
that account saw only the "Create your first vault" panel and a compact
"Connect an AI tool" launcher in the corner. The expanded connection invitation
required at least one Vault, so the people who most need the AI connection
never saw it. The launcher opens a complete setup guide, but that guide is
written around terminal commands and configuration files, which is a large step
for someone who has just signed in.

## Decision

When this AKB can describe its sign-in (`/api/v1/auth/config` is available)
and the verified Vault directory is empty, Home leads with a first-run guide
above "Your vaults":

1. **Pick an app**: Claude Code, Codex, Claude Desktop, or Other tools.
2. **Open the app with a prepared setup request.** The guide links to the app
   with the request already in its composer. The person reviews it and presses
   Send; nothing is sent automatically.
   - Claude Code: `claude://code/new?q=…` (the Claude desktop app's Code tab).
   - Codex: `codex://new?prompt=…` (the Codex app, or the Codex mode of the
     ChatGPT app).
   - Claude Desktop chats: a Claude Code session configures the desktop app's
     local MCP server, then the request opens in a new chat
     (`claude://claude.ai/new?q=…`).
3. **Approve** the command in the app and, with OAuth, sign in in the browser.
4. **Ask the app for the first Vault**, again through a prepared request.
5. When the first Vault appears, the same place shows it with three requests to
   try next and a link to open the Vault.

"Other tools" opens the existing complete setup guide (`QuickstartDialog`), so
the floating "Connect an AI tool" launcher is hidden while the guide is shown.
"Create your first vault" stays below as the browser alternative, with an
outline button so the guide keeps the view's single orange action.

### The sign-in follows the deployment

The guide chooses the credential from the public auth config instead of
assuming one deployment:

| Deployment | Claude Code and Codex | Claude Desktop chats |
| --- | --- | --- |
| `mcp_oauth.enabled` with an external identity provider | OAuth; copy names the provider's `display_name` | Connection key |
| `mcp_oauth.enabled` with only the Keycloak realm login | OAuth; copy says "your AKB account" | Connection key |
| MCP OAuth disabled, or local login | Connection key | Connection key |

Claude Desktop chats reach remote MCP servers from Anthropic's cloud, which
cannot reach an AKB on a private network, so they always use the local
`akb-mcp` proxy with a key.

A connection key is created in place with the existing issuance contract
(`PatIssuanceForm`, defaulting to the app name, 30-day expiry, read and write).
The receipt is bound to the sign-in session that created it, and the setup
request asks the app not to repeat the key.

### What the page can observe

The guide advances only on signals AKB already has:

- A connection key reports its first use through `last_used_at` in the key list.
  The guide re-checks every five seconds and when the window regains focus.
- An OAuth sign-in is not visible to the browser, so asking for the first Vault
  is the next step it shows. The Vault directory is re-read every five seconds,
  and on focus, after the Vault request is opened.

A server-side record of connected AI apps (planned separately) would let the
guide confirm an OAuth connection by app name. Until then, copy never claims a
connection the page has not observed.

### Commands keep one source

Install commands still come from `lib/mcp-snippets.ts`. This change adds the
Codex OAuth command (`codex mcp add akb --url …`, which signs in while adding
the server) and exposes `akbMcpArgs()` so the Claude Desktop request reuses the
same proxy arguments. The complete setup guide therefore also offers
"Sign in with AKB" for Codex.

## Constraints and risks

- `claude://code/new?q=` is the route the Claude desktop app uses for its own
  "New Claude Code Session" action; it is not documented. Every step therefore
  keeps a "Copy the requests instead" fallback.
- `claude mcp login` requires a terminal. The Claude Code request falls back to
  running it in a real terminal (`script` on macOS and Linux, a new window with
  `Start-Process` on Windows), and asks the person to finish the sign-in within
  the five-minute window.
- The guide is shown only when the auth config is available. Without it, Home
  keeps the previous empty state.

## Acceptance

- `src/lib/__tests__/ai-app-handoff.test.ts`: links, requests per app and
  credential, sign-in labels, Claude Desktop key-only setup.
- `src/components/__tests__/home-first-run-connect.test.tsx`: OAuth and key
  flows, in-place key creation and the key-use signal, Vault waiting, the first
  Vault view, and Other tools.
- `src/pages/__tests__/home-working-set.test.tsx`: Home leads a new account with
  the guide when setup is available, and keeps it in the same visit to show the
  first Vault.
