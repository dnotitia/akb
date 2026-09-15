---
status: accepted
stage: implemented
created: 2026-09-14
updated: 2026-09-15
---

# Resource reading workspace — one location, one command row

## Scope and recommendation

AKB is a knowledge workspace for people and agents. On a document route the
primary job is to read the selected resource, understand its location, and
optionally edit it. Vault administration and Git internals are secondary.

Replace the stacked resource title/context/viewer cards with one human-readable
location trail, one connected command row, and a continuous reading surface.
Keep the existing Workspace, Vaults, and Collections rails. Do not change Home,
account settings, authentication, indexing, API permissions, or document data.

Implemented in the frontend on 2026-09-14. The affected resource-shell rules in
`frontend/DESIGN_SYSTEM.md` are updated with the same location/command contract.
Existing backend changes in the working tree belong to separate work; this
implementation adds no endpoint or migration. No deployment is included.

## Baseline evidence before implementation

Inspected a signed-in document reader, its detail panel, and the same Vault's
Overview. Source review was independent of that
visual inspection. The document's actual body is not a title duplicate: an
authored heading differs from the document's metadata title. Preserve it.

| Current layer | Evidence / problem |
| --- | --- |
| App header, 56px | Desktop shows `Vault / Document`, omitting collection and actual title. |
| Vault navigation, 40px | A separate mostly empty band; Overview incorrectly receives the current-page state on resource routes. |
| Document identity, 64px | Repeats Vault/Collection and adds title, state, updated time, Edit and overflow. |
| Context card, at least 44px plus 12px gap | Repeats Collection and updated time; mixes author, hash, Watch, Pin, History, inspector. |
| File toolbar, approximately 45px | Adds another band for statistics, summary, copy and read modes. |
| Reading inset | 36px top padding on desktop, then the Markdown heading's own margin. |

The source-class budget is approximately **309 CSS px before Markdown's own
top margin**, including the app header and excluding browser chrome. This is
not a measured DOM bounding box; wrapping, zoom, banners and content change it.

Relevant code:

- [Desktop route identity](../../../../frontend/src/components/app-page-location.tsx)
  derives `Document` from route type, not resolved resource metadata.
- [Vault shell](../../../../frontend/src/components/vault-shell.tsx) separately
  derives mobile crumbs from the URL ref and falls through to Overview on
  document/file/table routes. URL refs may be filenames, short IDs or UUIDs.
- [Document page](../../../../frontend/src/pages/document.tsx) owns the identity
  header, context card, editing state and overlay inspector.
- [Document view](../../../../frontend/src/components/document-view.tsx) owns a
  second toolbar, outer padding and a forced minimum body height.
- [Reader CSS](../../../../frontend/src/index.css) targets direct children of
  `.document-reading-flow`, while the current
  [Markdown renderer](../../../../frontend/src/components/markdown-render.tsx)
  nests content through `EditorContent` / `.ProseMirror`. The intended common
  reading measure therefore appears disconnected from current rendered blocks.
  Verify actual computed widths before fixing the selector; do not report this
  as a measured browser result yet.
- Edit and History in the document page hide their only label below `sm`, while
  their glyphs are aria-hidden and the buttons have no persistent aria-label.

## Considered alternatives

1. Shrink every existing band: low implementation risk, but preserves duplicate
   context and several similar containers. Reject as the final direction.
2. Move everything into an oversized document hero: restores title emphasis,
   but contradicts the user's compact Git-like reading preference. Reject.
3. One location trail and one command row: recommended. It changes information
   priority rather than shrinking reading text. Vault navigation continuity was
   subsequently refined by the [working-page navigation design](../2026-09-15-vault-navigation-continuity/README.md).

## Location and navigation contract

Desktop example, using illustrative labels:

```text
Team / Guides / Getting started (Document)                        Search · Account
Overview · Search · Graph · Public links                       Members · Settings
Optional summary / statistics          [Rendered | Raw] · Copy · Edit · Publish · ⋯
───────────────────────────────────────────────────────────────────────────────────
Document body
```

This sketch shows roles, not a promise that all secondary text fits at every width.

- The trail is `Vault / Collection ancestry / human resource title (kind)`.
  Kind means Document, Table or File, not draft/active and not a filename suffix.
  Document subtypes such as note/guide remain metadata in Info.
- Do not add Home, `coll`, `doc`, `.md`, UUIDs or `akb://` as routine crumbs.
- Vault name is a direct Overview link. Collections remains a dedicated content
  explorer without Vault page menus. Overview and operational pages show a shared
  navigation row directly above their content; full resource readers retain it.
  Returning through the Vault breadcrumb or reselecting the Vault list entry
  opens Overview without moving or hiding the shared navigation. The global header
  keeps the trail, and Collections preserves its explicit collapse preference.
  Previews pair their local trail with Open in vault and Close, without copying
  the section navigator. Each Collection is a link to the
  existing collection destination (`/vault/{vault}?collection={path}`), revealing
  and focusing that collection. The final title is passive current-location text,
  not another navigation disclosure.
- Use resolved canonical resource data; never infer the visible title from a
  UUID-shaped URL. Resolve collection names from current authorized directory
  data, falling back to human path segments when no display name is available.
- If no Collection exists, omit that segment; do not invent a Root collection.
- Deep trails collapse middle ancestors into a keyboard-operable disclosure.
  Retain Vault, nearest Collection, title and kind when space permits. On narrow
  screens retain the navigation-drawer trigger and title, with all Collection
  destinations available through the ancestry menu even when the parent is hidden.
- Long titles remain available through an overflow-only hover/focus tooltip and
  the wrapping title in Document info for touch access. Do not squeeze Search,
  indexing or profile to make a long trail fit.
- Preserve a single semantic `h1#doc-title` for the article; it can be visually
  hidden when the visible title is in the location trail. Keep authored body
  headings and their existing anchors; never delete content by string matching.
- Document, Table and File routes keep the shared Vault navigation below the header
  and the resource command row below it. Vault destinations are not hidden inside
  the location trail or resource commands. Do not combine
  this with the document's mutation overflow or the switch-to-another-Vault menu.
- Vault Overview and operational pages retain their visible section navigation;
  no resource route claims `aria-current=page` on Overview.
- Preserve desktop 56px identity / 40px management alignment with the navigation
  rails. Mobile can reflow into two compact rows with larger touch targets;
  preserving usable controls takes priority over an absolute height target.

## Resource commands and metadata priority

| Priority | Placement |
| --- | --- |
| Location and resource identity | Breadcrumb; one kind label; no second visible title card. |
| Read / Raw, Copy, Edit, Publish | One command row, with segmented read modes right-aligned and visually distinct from Vault navigation. Copy and Edit share labelled icon styling. Edit remains an action, not a third mode tab. |
| Line count / UTF-8 size | Quiet leading statistics when space permits; available in Info on narrow widths. |
| Summary | Optional single-line flexible toolbar slot; explicit full disclosure. No new vertical summary band. |
| Author, updated time, tags, commit/hash | Info, with timestamps labelled accurately. Do not imply author is the latest editor without supporting data. |
| Document info, Table of contents, Watch, Pin, History | Named entries in overflow; preserve Watching/Pinned state and direct inspector-tab opening. They are not deleted. |
| Publish / Public link | Visible toolbar control opens explicit publishing options or existing-link management. Never publish on disclosure. Preserve permission reasons, confirmation before unpublishing, and manual copy recovery. |
| Move, archive/restore, delete | Grouped existing overflow actions, preserving exact role and unsupported-server reasons. |
| Archived/historical/diff/unsaved/conflict state | Keep explicit visible state and recovery controls; never bury safety notices solely in Info. |

On narrow workspaces collapse summary and statistics before hiding command text.
Use Read/Raw as a labelled compact selector and retain Copy, Edit, Publish and
overflow. Document info is no longer a permanent button. Essential actions have stable accessible
names even when visual labels collapse. Do not shrink the text to fit everything.

Edit uses the same resource shell: Save changes / Cancel replace read commands;
title and body remain a single form. Remove redundant `Editing document` chrome
only when its unsaved/upload state has a retained visible status slot. Preserve
draft recovery, duplicate-title handling, upload failures and cancellation guards.

## Reading surface and visual direction

The signature is the human resource path directly above a flat document surface,
not an ornamental card, avatar cluster, gradient or another large heading.

- Keep the outer workspace full-width, with no grey moat or nested rounded file
  card. Use a single structural divider below commands. Tables/code may still
  have their own useful block boundaries.
- Remove the forced `min-h-80` on a short rendered body. The canvas can fill
  remaining viewport height without pretending the content itself is long.
- Use a 16–24px first-content inset and eliminate the first block's extra top
  margin. Do not collapse spacing between all authored headings and paragraphs.
- Keep rendered text at 16px with approximately 1.65 line-height; UI labels
  remain 14px and metadata 12px. Existing Pretendard is the UI/reading face;
  JetBrains Mono is only for Raw, code and technical identifiers.
- Keep one shared measure for prose headings, paragraphs, lists and ordinary
  tables. Start with the project's 64rem technical-reading measure and validate
  Korean/English paragraphs against actual DOM, rather than applying separate
  `ch` widths at each heading font size. The work surface remains full-width.
- Expose an on-demand Reading width choice in overflow: Standard / Wide. Wide
  removes the reading measure for users who want reclaimed rail space. No
  additional permanent layout button or automatic rail collapsing.
- Code and genuinely wide tables scroll within their own labelled/focusable
  regions. Images retain aspect ratio; small images are not forcibly stretched.
  Raw/Diff retain full available work width. No page-level horizontal overflow.
- Preserve the right overlay inspector; it does not reserve reading width.
  `Document info`, `Table of contents` and `History` open their inspector views
  from overflow. Transfer focus on each such selection, including when the
  inspector is already open; ordinary menu dismissal restores the Actions
  trigger. Info, Outline, Relations and
  History remain peer panel views; remove redundant panel explanatory prose if
  the labels already explain those views.

Existing palette tokens only, shown here as light-theme reference values:

| Token | Value | Role |
| --- | --- | --- |
| surface | #ffffff | Reading surface and quiet command shell |
| foreground | #1d1d1f | Title/body |
| foreground-muted | #5e6068 | Ancestry and metadata |
| border | #dfe3e8 | Structural divider |
| primary | #004059 | Primary action and active mode |
| accent-strong | #c44a1e | Existing save/create emphasis, not read-state decoration |

Dark mode uses the existing semantic token overrides, not these literal values.
Use token radii and focus treatments. No new shadow/blur on the reading surface.
Motion stays limited to existing menu/inspector transitions and reduced-motion
support. Styling is deliberately subordinate to typography and the resource path.

## Frontend implementation boundaries

1. A shared resource-location model supplies desktop, mobile and preview crumbs.
   It consumes the existing document/file/table queries and authorized collection
   data; it must not perform per-ancestor fetches or cache a prior resource's title
   after a route/account change. Loading and denied states must not leak old labels.
2. Extract a resource command-row contract shared by `DocumentPage` and its
   preview. Avoid duplicating a new toolbar inside `DocumentView`; that component
   should support body-only composition. Keep the new-document composer intact.
3. The preview does not have its own VaultShell. Give it a local trail, explicit
   Close and Open in vault, and preserve query/filter/scroll/Back/focus restoration.
   Do not rewrite the background page's global breadcrumb to the preview title.
   Edit still promotes to a full-page editor.
4. Scope reading CSS to the real current renderer DOM, not old Plate wrappers or
   all `.prose` consumers. Fix the narrow-button accessible names in the same pass.
5. Extend the shared location/command grammar to File and Table without removing
   Download, Add row, Schema, filter/pagination or role restrictions. Do not change
   their underlying API behavior or redesign the data grid in this task.
6. Update design-system and loading-shell rules together after visual acceptance.
   No new backend endpoint or migration is required by this design.

## Acceptance plan

The implementation uses this acceptance checklist. Executed checks and remaining
limits are recorded below; baseline observations above are retained for context.

- Verify normal reading starts roughly 120–160 CSS px below app content top on a
  roomy desktop, instead of the current approximately 309px structural budget.
  This target excludes authored heading height and exceptional policy banners.
- Capture 375, 768, 1440 and 2560 widths, 200% zoom, light/dark, rails expanded
  and folded. No secondary root scrollbar, clipped actions or title/account jitter.
- Test long Korean/English titles, deep collections, same-title resources, root
  resources, reserved guides, UUID/short-ID routes, moved documents and absent
  optional metadata. Full names and ancestry remain recoverable without hover.
- Verify breadcrumb semantics (`nav`, ordered list, current item), menu keyboard
  operation, persistent button names, one H1, body heading links and focus return.
- Exercise read-only/archived/historical/diff/edit states, unsaved Cancel, image
  upload failures, title conflicts, Copy, Watch/Pin, Move and History.
- Check a one-line document, long paragraphs, lists, nested blocks, wide tables,
  code, images and outlines. Measure actual prose/table alignment and first-block
  top margin in the current renderer, including the Standard/Wide switch.
- Test both full-page and search/Home preview, navigation out to Vault/Collection,
  and Back restoring the launch context. Do not treat screenshot success as API
  or permission verification.
- Run design check, typecheck, lint, unit tests and focused browser regressions
  when implementing. Deploy only locally if requested; production is out of scope.

## Guidance and limits

The frontend-design skill informed the intentional, content-first direction;
ui-ux-pro-max searches specifically matched hierarchical breadcrumbs, readable
line length and line-height. Exact dimensions, command priorities and the
Standard/Wide compromise are AKB design decisions, not external mandates or
validated usability-study results.

- [WAI breadcrumb pattern](https://www.w3.org/WAI/ARIA/apg/patterns/breadcrumb/):
  ordered ancestry, labelled navigation landmark, current-page semantics.
- [Atlassian breadcrumbs](https://atlassian.design/components/breadcrumbs/breadcrumbs):
  location within an app, rather than an unrelated route-type label.
- [Carbon global header](https://carbondesignsystem.com/patterns/global-header/):
  stable orientation/navigation and distinction between menu disclosure and links.

The initial design inspection used the signed-in local desktop. Implementation
acceptance uses isolated browser-owned HTTP fixtures, not real customer content.

## Implementation and verification — 2026-09-14

- Added route/account/access-scoped resource location, canonical collection
  breadcrumbs, full-name disclosure and shared Vault-pages command row.
- Removed document identity/context cards and nested reader toolbar; retained
  semantic H1, authored headings and visible safety/recovery states. File/Table
  adopt the same shell while retaining their existing data and mutation contracts.
- Moved Watch/Pin/History into overflow, added Standard/Wide, and kept summary,
  statistics and manual-copy recovery accessible without another metadata band.
- Fixed actual ProseMirror/table-wrapper alignment. Ordinary tables share the
  prose measure, long code/wide tables scroll locally, and small images retain
  their natural dimensions. Closed Info drawers no longer cast a canvas shadow.
- Review caught a foreground access-refresh editor remount that could restore
  the original body over a live draft. Remount now seeds the current draft;
  account switches reset editor state. A delayed-query browser test exercises
  the unmount, restoration and subsequent typing. Vault permissions refresh in
  the same access scope; a writer-to-reader change disables saving while keeping
  the draft recoverable.
- Frontend unit suite: 135 files / 1,102 tests passed. Design check, TypeScript,
  lint and production build passed. Lint has existing non-blocking warnings;
  the production build retains its bundle-size advisory.
- 37 focused browser checks cover 375/768/1440/2560 CSS px, light/dark, expanded/folded
  rails, actual block geometry, menu/panel focus, preview promotion/restoration,
  watch/unwatch, archive/restore, File/Table commands and metadata, and editor
  recovery/conflict/image retry. Wide-table local scrolling also passes at 200%
  CSS zoom; one-line prose has no artificial minimum content height.
  Roomy desktop fixture heading starts at approximately 123 CSS px below the
  viewport top. Mobile commands use two touch-sized rows.
- These are frontend contract and layout checks, not new backend integration
  certification or a usability study. CSS zoom checks do not certify native
  browser/OS magnification or physical-device assistive technology. No local
  Docker or production image was replaced.
