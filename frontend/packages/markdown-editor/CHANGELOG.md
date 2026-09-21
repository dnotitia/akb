# Changelog

## 0.11.0

- Add the public `MarkdownSurface` image renderer shared by editor and viewer
  consumers, with canonical target/alt/title preservation and aspect-ratio-safe
  runtime sizing.
- Add common loading, inaccessible, and browser decode-failure states with
  product-provided labels and class names.
- Add resolver `release()` and `refresh()` lifecycle hooks so object URLs and
  grant-bearing image URLs are cleaned up or retried without changing Markdown.
- Normalize image-only viewer bodies and keep the terminal editor paragraph out
  of serialized Markdown.

## 0.10.0

- Add the common `@` reference menu with product-owned person, issue, document,
  and file candidates, grouped rendering, keyboard/pointer selection, ARIA
  state, loading/empty/error states, cancellation, stale-response handling,
  and IME/read-only/code/link guards.
- Insert people and issues as product-provided plain text while inserting
  document/file candidates as canonical Markdown links; runtime or signed URLs
  are never introduced by the package.
- Add the public reference adapter/options contract and the React hook bridge
  for changing product context without replacing the editor instance.
- Document the pnpm 11 exact `allowBuilds` bootstrap required before the first
  full-SHA Git subdirectory install, so `prepare` can produce the public
  `dist` exports for frozen consumer installs.

## 0.9.0

- Add the shared ten-command slash menu with localized filtering, keyboard and
  pointer selection, ARIA state, viewport/clipping-boundary placement, active
  option scrolling, Escape handling, and IME/read-only guards.
- Add the shared task-list command to `MarkdownCommands`, state, toolbar, and
  slash menu while keeping formatting ownership in the package.
- Replace the obsolete `onSlash` callback with the public slash menu options.

## 0.8.0

- Add the shared image upload surface for file selection, standalone image
  paste, drop, serial progress, cancellation, partial outcomes, retry, and
  read-only handling.
- Map insertion and replacement through ProseMirror selection bookmarks so
  edits and cursor movement during an upload do not move the target.
- Add product-owned upload context and asset lifecycle callbacks while keeping
  validation, storage, retention, and draft policy outside the package.
- Add `MarkdownCommands.replaceImageAt` and progress callbacks to
  `uploadMarkdownBatch`.

## 0.7.0

- Add position-based image alt and body-delete commands. Each operation targets
  one Markdown image occurrence and forms its own undo step.
- Add shared per-image edit, optional replace-callback, and remove controls to
  `MarkdownEditingSurface` and `MarkdownEditor`, with product-provided labels,
  target policy, and styling.
- Normalize parsed top-level images and retain an editable paragraph in the
  shared editing surface; image removal leaves the referenced bytes to the
  product's attachment policy.
- Ignore product-owned runtime link attributes in the editable DOM so target
  resolution does not turn into Markdown document changes.

## 0.6.0

- Add the shared WYSIWYG / Markdown Source editing surface. It keeps one editor
  mounted across mode switches, preserves unchanged selection and undo state,
  and reports Source edits through the existing Markdown change contract.
- Export `serializeEditorMarkdown` so products can persist the same canonical
  Markdown body that the Source surface synchronizes.
- Add public table commands and command state for GFM insertion, selected-cell
  row/column edits, table deletion, and continuing in the paragraph below a
  specific table.
- Add the common toolbar insertion control and table-local editing controls to
  `MarkdownEditingSurface`, with product-provided labels and styling.
- Keep table edits, undo, and Markdown serialization on the existing schema and
  editor history so headers, cell contents, and row/column order round-trip.

## 0.5.0

- Move document/file search UI, async states, result selection, and cancellation
  into the shared link popup and toolbar.
- Replace the product-owned `searchSlot` with `searchAdapter`, `searchContext`,
  and product-provided search labels and styling.

## 0.4.0

- Add the shared link command/state contract and accessible React link popup.
- Preserve selection and editor focus through link insert, update, remove,
  cancel, and URL validation flows; accept product URL policies and existing
  search adapters through explicit options.

## 0.3.0

- Add the shared default Markdown formatting toolbar with active/disabled state,
  selection-preserving pointer controls, and roving keyboard navigation.
- Expand the public command/state contract for headings, marks, lists, blocks,
  horizontal rules, and history controls.

## 0.2.0

- Preserve canonical document, standalone File, and document Attachment targets through image/link Markdown editing.
- Add product-neutral upload batch outcomes and runtime-only target resolution for editor/viewer surfaces.

## 0.1.0

- Add the shared Tiptap Markdown editor/viewer core and conformance contract.
- Add CommonMark/GFM, math, Mermaid fence, raw HTML, and MDX preservation coverage.
- Document React 19 consumer compatibility and external browser/conformance verification.
