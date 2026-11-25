# AKB Frontend

React 19 + TypeScript + Vite + Radix UI + Tailwind CSS v4. The web UI for
[AKB](../README.md), the agent knowledgebase.

## Scripts

```sh
pnpm install  # install frontend + the sibling markdown-editor workspace package
pnpm dev              # vite dev server on :5173 (proxies /api to :8000)
pnpm typecheck        # tsc --noEmit
pnpm lint             # eslint src
pnpm test             # vitest run
pnpm build            # tsc && vite build
pnpm preview          # serve dist locally
pnpm run dev:e2e:mock    # start the mock frontend on 127.0.0.1:4173
pnpm run test:e2e:mock    # Playwright + MSW, no backend required
pnpm run test:e2e:fixture # Playwright against a production build; specs answer their own HTTP
pnpm run test:e2e:real    # Playwright against AKB_FRONTEND_URL from the runtime descriptor
```

## Editing documents

Two write surfaces live in the SPA — both reuse the same lazy-loaded
`@akb/markdown-editor` Tiptap `MarkdownEditor`:

- **New document** — `+ NEW DOC` link on the vault page, or
  `/vault/:name/doc/new`. Visible to writers/admins/owners. The form
  asks for title, collection (lowercase path, `engineering/specs`-style),
  optional type/domain/tags/summary, and a body in markdown. New
  collections are created on the fly by the backend.
- **Edit document** — the document editing workspace combines title, details
  (including type/tags) and Markdown body. Saves send only changed fields to
  `PATCH /documents/:vault/:id`, together with `expected_commit` for optimistic
  concurrency. A missing or stale revision must be resolved before saving.

The editor warns about unsaved changes when leaving and supports recoverable
local drafts. Draft recovery keeps incompatible or expired text available for
copying; an expired image attachment is not guaranteed to remain recoverable.
Cancel/discard is confirmed before losing unsaved work.

Link URLs in user-authored markdown pass through `sanitizeLinkUrl()`
on both the editor and the rendered view — `javascript:`, `data:`,
`vbscript:`, and protocol-relative `//host` schemes round-trip to
`#` so a malicious doc can't embed a clickable XSS.

AKB resource links use their canonical `akb://…/doc|file` target in Markdown. The editor and
viewer resolve those targets only for the current session, showing an unavailable placeholder
when access or the resource is gone. Signed download URLs and private image blob URLs never
enter the document body. Editor image uploads return the server's unclaimed expiry metadata so
recoverable drafts do not promise a longer attachment lifetime than the configured TTL.

## Architecture quick map

```
src/
  pages/                       route components
    document.tsx               document reading, unified editing and revision diff
    document-new.tsx           create page with form + shared Markdown body
    vault.tsx                  vault home; `+ NEW DOC` entry point
    ...
  components/
    markdown-editor.tsx        shared Tiptap editor wrapper (lazy chunk)
    markdown-editor-fallback.tsx  Suspense placeholder (main bundle)
    document-view.tsx          document rendering and reading controls
    ui/tag-input.tsx           shared tag chip input
    ui/...
  lib/
    api.ts                     fetch helpers + ApiError
    utils.ts                   `cn`, `timeAgo`, `sanitizeLinkUrl`, ...
    doc-constants.ts           DOC_TYPES / DOC_STATUSES
    markdown.ts                heading parsing for the outline
```

The shared editor chunk only loads when the user enters `EDIT` or `/doc/new`,
so the read-only path is unaffected.

## Testing

Unit tests live under `src/**/__tests__/`. Vitest runs against jsdom +
`@testing-library/react`. Critical security helpers (link sanitizing)
and interactive components (`TagInput`) have explicit coverage. Add a
test alongside any change to those code paths.

Playwright e2e specs live in `e2e/`. Choose `mock`, `fixture` or `real`
explicitly. Each mode selects tests for the server it uses.

- `pnpm run test:e2e:mock` starts `pnpm run dev:e2e:mock` and waits for
  `/__akb_mock__/health`. Vite exposes the runtime descriptor at
  `/__akb_mock__/discover` and prints it as one schema-v2 JSON line on stdout
  when the mock listener is ready. `POST /__akb_mock__/reset` advances the
  reset generation. The browser MSW worker reads that generation before the
  next auth request and resets its in-memory fixture state. The worker returns
  501 for unhandled `/api` requests. Tests that depend on `page.route` skip in
  mock mode.
- `pnpm run test:e2e:fixture` builds the app, serves it with `vite preview`,
  and runs every spec that answers its requests through `page.route`. No
  backend runs: `/api` is proxied to a closed port, so a request a spec does
  not answer gets a 502 and a `http proxy error` line on stderr. These specs do
  not share state, so CI splits them across jobs with `--shard`; locally they
  run on one worker unless you pass `--workers`. Run one fixture run per host
  at a time. The port (4174) and build directory are fixed.
- `AKB_FRONTEND_URL=<services.web.origin> pnpm run test:e2e:real` runs the two
  cases in `e2e/smoke.spec.ts` through the real frontend and backend. Read the
  frontend origin from the descriptor printed by the repository runtime.
- `pnpm run test:e2e:real:optional` selects the `chromium-live-optional` project.
  Provide the same `AKB_FRONTEND_URL` and name a spec to run it on an isolated
  runtime. Give new optional live specs the `-live.spec.ts` suffix. Each spec
  needs its own opt-in or credentials.
- Set `AKB_FE_E2E_SCENARIO` with `pnpm run test:e2e:mock` to run one of the
  mock-only scenarios. `document-edit-recovery`, `markdown-image-rendering`,
  and `markdown-reference-adapters` each select their corresponding spec.

| Optional spec | Required environment |
| --- | --- |
| `search-live.spec.ts` | `AKB_SEARCH_LIVE=1` |
| `mcp-monitoring-access.spec.ts` | `AKB_MONITORING_E2E_APP_ORIGIN`, `AKB_E2E_USERNAME`, and `AKB_E2E_PASSWORD` |
| `pat-issuance-live.spec.ts` | `AKB_PAT_ISSUANCE_E2E=1` |
| `account-security-live.spec.ts` | `AKB_ACCOUNT_SELF_SERVICE_E2E=1` |
| `profile-photo-live.spec.ts` | `AKB_PROFILE_PHOTO_LIVE=1` |

Enable `account_self_service_enabled` on the disposable backend for the PAT,
account security, and profile photo cases. These cases delete the accounts they
create.

For a manual mock session, run `pnpm run dev:e2e:mock` and open
`http://127.0.0.1:4173`. Stop Vite with Ctrl-C.

## Adding routes

The canonical route/component/shell map lives in `src/app-route-contract.ts`.
`src/app-routes.tsx` renders that contract for both the real application and
Storybook's routed page scenarios. Update the contract test whenever a route,
page component, auth/public boundary, or shell owner changes.
