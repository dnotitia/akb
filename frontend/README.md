# AKB Frontend

React 19 + TypeScript + Vite + Radix UI + Tailwind CSS v4. The web UI for
[AKB](../README.md), the agent knowledgebase.

## Scripts

```sh
pnpm install          # install deps
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
[Plate](https://platejs.org)-based `MarkdownEditor`:

- **New document** — `+ NEW DOC` link on the vault page, or
  `/vault/:name/doc/new`. Visible to writers/admins/owners. The form
  asks for title, collection (lowercase path, `engineering/specs`-style),
  optional type/domain/tags/summary, and a body in markdown. New
  collections are created on the fly by the backend.
- **Edit body** — `EDIT` tab on the document page (writers and above,
  hidden in historical view). The editor reclaims the full outlet width
  while open (right rail hides). Saves go through
  `PATCH /documents/:vault/:id` with only the `content` field; metadata
  is preserved by the backend's frontmatter merge logic. Use the
  `Edit details` button in the right rail for title/type/tags/etc.

Both flows guard against accidental data loss:

- `beforeunload` warning while the editor is dirty.
- Confirmation when switching tabs from `EDIT` with unsaved changes.
- `EDIT*` star marker + a `UNSAVED CHANGES` line while dirty.
- Cancel resets the editor to the last server state.

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
    document.tsx               read + edit body (Rendered / Raw / Agent / Edit)
    document-new.tsx           create page with form + Plate body
    vault.tsx                  vault home; `+ NEW DOC` entry point
    ...
  components/
    markdown-editor.tsx        Plate editor (lazy chunk)
    markdown-editor-fallback.tsx  Suspense placeholder (main bundle)
    document-view.tsx          Rendered/Raw/Agent + WAI-ARIA tab strip
    frontmatter-edit-dialog.tsx  metadata + optional body edit
    ui/tag-input.tsx           shared tag chip input
    ui/...
  lib/
    api.ts                     fetch helpers + ApiError
    utils.ts                   `cn`, `timeAgo`, `sanitizeLinkUrl`, ...
    doc-constants.ts           DOC_TYPES / DOC_STATUSES
    markdown.ts                heading parsing for the outline
```

The Plate chunk is ~260 KB gzipped and only loads when the user enters
`EDIT` or `/doc/new`, so the read-only path is unaffected.

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
