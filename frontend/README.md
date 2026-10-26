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
pnpm run test:e2e:mock # Playwright + MSW, no backend required
pnpm run test:e2e:real # Playwright against AKB_FRONTEND_URL from the runtime descriptor
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

Playwright e2e specs live in `e2e/`. Choose `mock` or `real` explicitly:

- `pnpm run test:e2e:mock` starts Vite with the browser MSW worker, exposes
  mock readiness/discovery/reset at `/__akb_mock__/health`,
  `/__akb_mock__/discover`, and `POST /__akb_mock__/reset`, and fails
  unhandled `/api` requests with a 501 response. The public reset increments
  the Vite run's reset generation; the browser worker reads that generation
  before the next auth request and resets its in-memory fixture state, so an
  external HTTP client can reset the active browser run without reaching into
  the page. Once the mock listener is ready, Vite also prints the same
  schema-v2 descriptor as one JSON line on stdout for runtime helpers.
- `AKB_FRONTEND_URL=<services.web.origin> pnpm run test:e2e:real` consumes the
  common schema-v2 descriptor. The backend, fixture reset, and process shutdown
  remain owned by the repository runtime.
- `AKB_FE_E2E_SCENARIO=markdown-reference-adapters` exposes a source-neutral reference
  fixture in the schema-v2 descriptor. Its discovered state/expiry/failure controls exercise
  canonical document/file/attachment targets, unavailable resolution, and partial upload
  recovery through the same mock reset path.

For a manual mock session, run `VITE_AKB_TEST_MODE=mock pnpm run dev --host
127.0.0.1 --port 4173 --strictPort`. The browser worker exposes readiness and
discovery at `/__akb_mock__/health` and `/__akb_mock__/discover`, and reset at
`POST /__akb_mock__/reset`; stop the Vite process with Ctrl-C. Mock mode is
never enabled by the default application or production build.

## Adding routes

The canonical route/component/shell map lives in `src/app-route-contract.ts`.
`src/app-routes.tsx` renders that contract for both the real application and
Storybook's routed page scenarios. Update the contract test whenever a route,
page component, auth/public boundary, or shell owner changes.
