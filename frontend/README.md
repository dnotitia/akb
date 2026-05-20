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
pnpm test:e2e         # playwright (requires backend reachable at AKB_URL)
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

Playwright e2e specs live in `e2e/`. `pnpm test:e2e` expects the AKB
backend reachable at `AKB_URL` (defaults to `http://localhost:8000`).

## Adding routes

Routes are declared in `src/main.tsx` inside the `<Routes>` block.
Doc routes are nested under `VaultShell`; the doc-create route
(`/vault/:name/doc/new`) sits *before* `/vault/:name/doc/:id` so the
literal `new` segment matches first.
