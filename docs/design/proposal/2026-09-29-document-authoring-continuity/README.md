# Document authoring continuity

Status: implemented and locally verified · 2026-09-29

## Approved interaction contract

Reading and editing keep their current presentation: search previews edit inside
the same dialog, while full-page readers edit in place. Only Open in vault
promotes a preview to a page. Create and Edit share the authoring layout and
details fields, with one save for title, body and changed metadata. Existing
Collection is read-only; moving remains a separate operation.

The writing area uses container-responsive insets. At 52rem of available width,
details occupy 18rem on the right (20rem at 72rem); otherwise they follow the
body. Read-mode context remains an overlay, not a permanent form sidebar.

Edit dismissal, navigation and browser Back protect unsaved work. Native file
picker focus changes never dismiss authoring; Escape first dismisses editor
menus. Saving/uploading blocks navigation. Cancel restores the reader; explicit
discard clears only this draft and its unclaimed assets. Local drafts preserve
details alongside title/body with backward-compatible recovery. Search query,
scope and result context survive preview/edit/return without stacked dialogs.

## Implementation plan

Execution uses the existing isolated `feat/composer-settings-workspaces`
worktree. Preserve prior viewer/Home/E2E changes. No backend, production, commit,
push or PR mutation is included.

- [x] Add failing tests for preview Edit preserving route state, details-only
  dirty/save/cancel/recovery, modal dismissal and browser Back protection.
- [x] Extract `DocumentAuthoringLayout` and `DocumentDetailsFields`; adopt them
  in creation without changing its validation or upload contract.
- [x] Extend document edit drafts with optional validated `baseDetails` and
  `details`; preserve old drafts and malformed-record copy recovery.
- [x] Use shared authoring in `document.tsx`, one conditional PATCH with
  `expected_commit`, and preserve details across revision conflict/recovery.
- [x] Add preview lifecycle ownership and guarded Back/close behavior; restore
  the source search session and focus without keeping two modal focus traps.
- [x] Test keyboard, cancel, failed save, image picker, content/details scrolling,
  mobile/dark layouts and query/scope preservation using browser-owned fixtures.
- [x] Run design check, TypeScript, lint, unit and focused browser gates. Review
  the diff, update the design-system contract, and verify local frontend only.

## Acceptance checks

Clicking Edit in either search preview leaves `documentPreview` history state
intact and does not mount Vault navigation. Cancel/save leaves the reader open.
Changing only Summary enables Save and participates in the same exit warning.
The PATCH includes only changed values and the loaded revision. Reload recovery
preserves metadata as well as text. Back, X, Escape and Open in vault cannot
silently lose changes or interrupt a save/upload. Read and authoring surfaces
stay inside the same dialog bounds; 375px layouts have no horizontal overflow.

## Verification evidence

- Design guard, TypeScript and production build passed. Lint reported no
  errors; the existing 104 warnings remain outside this change.
- Unit tests: 169 files, 1,616 tests passed.
- Browser coverage on the rebuilt local frontend: 146 distinct cases across
  authoring continuity, quick-search return, edit-title layout, reading workspace
  and composer/settings layout. The first run passed 144; two legacy assertions
  expected immediate editor focus instead of the newly restored search dialog.
  After updating those assertions to verify search restoration followed by
  preserved draft content, all four dirty-editor search cases passed.
- Browser tests use intercepted API fixtures, not writes to user vaults. They
  exercise the real file chooser, pending image upload, failed/pending save,
  browser Back, draft recovery, rendered/raw return, and responsive layouts.
  This is frontend interaction coverage, not a live-backend persistence audit.
- Reviewed desktop light, wide dark and mobile screenshots. Independent code
  review found no P1/P2 issues in the named authoring/lifecycle changes.
- Rebuilt and replaced only the local frontend container on port 3000.
  Backend, worker and production deployments were not changed.

## Implementation notes

The browser-history dispatcher is installed before `BrowserRouter`: a listener
added later cannot reliably precede the router's existing window listener in
Chromium. Guards replay a confirmed in-app history transition only once. External
or unindexed history exits retain the existing `beforeunload` protection.

Search is suspended, not stacked beneath the document focus scope. Its return
token is consumed only after the router commits the original background entry,
and only by the matching live search session. Explicit Open in vault never
reopens the old search dialog.
