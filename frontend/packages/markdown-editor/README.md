# @akb/markdown-editor

`@akb/markdown-editor` is the product-neutral Tiptap Markdown core shared by AKB and Reef.
It owns the document schema, Markdown parsing/serialization, editor/viewer surfaces, commands,
state hooks, the default formatting/link/table controls, shared document/file search UI, and the
conformance contract. Products still own storage, permissions, search adapters and context, copy,
and product-specific URL policy.

The package is built against the exact Tiptap `3.31.3` package set and React 19.

## Public surface

```tsx
import {
  MarkdownEditor,
  MarkdownEditingSurface,
  MarkdownViewer,
  MarkdownToolbar,
  type MarkdownTableOptions,
  canonicalizeMarkdown,
  parseMarkdown,
  serializeEditorMarkdown,
  serializeMarkdown,
  useMarkdownCommands,
  useMarkdownState,
} from '@akb/markdown-editor'

const canonical = canonicalizeMarkdown('# Hello\n\n- [x] shared syntax')

function Document({ markdown, onChange }) {
  return (
    <>
      <MarkdownEditor markdown={markdown} onChange={onChange} />
      <MarkdownViewer markdown={markdown} />
    </>
  )
}
```

`MarkdownEditor` includes a `WYSIWYG` / `Source` switch for the same Markdown
draft. Source opens from the current editor body, and Source edits emit raw
Markdown through `onChange`; the shared editor stays mounted, and formatting,
link search, and slash handlers remain attached to the WYSIWYG surface.
`serializeEditorMarkdown(editor, { profile: 'preserve' })` reads the current
editor body and omits the terminal empty paragraph that Tiptap keeps as an
editing caret after an atomic block.

Products with a custom editor instance and toolbar can compose the same public
surface from the `/react` entry point:

```tsx
import {
  EditorContent,
  MarkdownEditingSurface,
  MarkdownToolbar,
  type MarkdownTableOptions,
  useMarkdownEditor,
} from '@akb/markdown-editor/react'

function ProductEditor({ markdown, onChange, readOnly }) {
  const tableOptions: MarkdownTableOptions = {
    labels: { insertTable: 'Insert a data table' },
    tableClassName: 'min-w-[36rem]',
  }
  const editor = useMarkdownEditor({
    initialMarkdown: markdown,
    editable: !readOnly,
    onChange,
  })

  return (
    <MarkdownEditingSurface
      editor={editor}
      markdown={markdown}
      readOnly={readOnly}
      table={tableOptions}
      toolbar={<MarkdownToolbar editor={editor} table={tableOptions} />}
      onSourceChange={onChange}
      modeLabels={{ group: 'Editor mode', source: 'Source' }}
    >
      <EditorContent editor={editor} />
    </MarkdownEditingSurface>
  )
}
```

`MarkdownEditingSurface` owns mode switching, source input, external-value
synchronization, and focus handoff. Its `toolbar` slot appears only in
WYSIWYG mode; `modeLabels`, `sourceClassName`, and the source label props adapt
copy, theme, and accessible names. `modeSwitchDisabled` can lock mode changes
during an active product operation such as an upload. The optional
`onWysiwygDragOverCapture` / `onWysiwygDropCapture` handlers attach product
drag-and-drop behavior to the visual editor and its toolbar without affecting
Source input. `onMarkdownApplied` is an optional product hook for schema-specific
normalization after external or Source Markdown is applied. Products continue
to own persistence, draft/OCC behavior, and asset-reference policy.

`MarkdownToolbar` provides the shared table insertion control. Passing the same
`MarkdownTableOptions` to the toolbar and editing surface adapts table labels
and styling. The editing surface attaches a menu to each table; its row and
column commands use the currently selected cell in that table, and commands for
other tables stay disabled. “Continue below” focuses the paragraph immediately
after that table, creating one before the next block when needed. Table deletion
leaves an editable cursor position and is part of the editor undo history.

`MarkdownEditingSurface` and `MarkdownEditor` accept `imageMenu` to attach the
shared description and body-removal controls to each eligible image occurrence.
The menu selects by editor document position, so repeated occurrences of the
same target remain independent. `MarkdownCommands.setImageAltAt(position, alt)`
and `deleteImageAt(position)` expose the same undoable operations. Removing an
image edits Markdown only; the product still decides whether an uploaded asset
is discarded, retained, or claimed.

Products pass their target eligibility rule, replacement upload callback,
accessible labels, and theme classes through `MarkdownImageMenuOptions`. The
callback receives the selected image's ProseMirror document position. The
package defaults use the shared `surface`, `border`, `foreground`, and
`destructive` theme tokens; pass `classNames` to adapt them to another product.
Read-only surfaces do not render image controls. Closing or cancelling the
description dialog returns focus to the editor.

`MarkdownToolbar` owns the default Paragraph, Heading 1–3, bold, italic,
strikethrough, inline code, list, blockquote, code block, horizontal rule,
table insertion, link, and undo/redo controls. It reads the same editor state
and commands as the editor, preserves the current selection while the link
popup receives focus, restores the selection on cancel, validates before
mutating, and exposes roving keyboard focus. Product-specific controls can be
appended as children; use `MarkdownToolbarGroup` and `MarkdownToolbarButton` so
they participate in the same toolbar navigation.

```tsx
<MarkdownToolbar
  editor={editor}
  link={{
    normalizeUrl: productNormalizeUrl,
    searchAdapter: productSearchAdapter,
    searchContext: { vault: activeVault },
    searchLabels: {
      inputLabel: 'Search Vault resources',
      empty: 'No matching documents or files found.',
    },
    searchClassName: 'space-y-2',
  }}
>
  <MarkdownToolbarGroup label="Insert">
    {/* Product-specific upload controls can stay here. */}
  </MarkdownToolbarGroup>
</MarkdownToolbar>
```

`MarkdownCommands` exposes `insertTable`, `addTableRowAfter`,
`addTableColumnAfter`, `deleteTableRow`, `deleteTableColumn`, `deleteTable`, and
`continueBelowTable`, alongside the Markdown and link commands. Each table
operation returns `false` when the editor is read-only, the selection is not in
a table, or the operation cannot run safely. `MarkdownState.table` reports the
active table and command availability. The editing surface’s table menu invokes
these public commands without moving selection to a default cell.

`MarkdownCommands` exposes `setLink`, `insertLink`, and `unsetLink`, while
`MarkdownState.active.link` and `MarkdownState.link.href` expose the current
link state. `MarkdownLinkPopup` is also public for consumers that need a
different toolbar composition. `normalizeUrl` is the product seam for
canonical targets and link policy; returning `null` leaves the popup open,
shows the configured error, and keeps focus in the URL field. Provide a
`MarkdownSearchAdapter` to enable shared document/file search. The popup calls
`search(query, context)` as the query changes, passing the product context and
an `AbortSignal`; cancellation, popup close, query changes, and context changes
discard late results. Loading, empty, and error states are distinct. Candidates
can be selected with the pointer or ArrowUp/ArrowDown + Enter; selection fills
the existing link text and URL fields, and the popup's existing Apply action
performs the Markdown edit. Product-specific text is provided through
`searchLabels`, and `searchClassName` can tune the search region within the
product's design system. Search results must return canonical
`MarkdownSearchResult.target` values, never signed or runtime URLs.

Use `profile="structured"` for CommonMark + GFM structure editing. The default `preserve` profile
adds explicit raw HTML/MDX nodes while keeping math and Mermaid fences as semantic nodes. Both
surfaces use the same extension set for a stable viewer/editor meaning model.

Upload, search, and target resolution are adapter interfaces. A `MarkdownAsset` returns the
canonical `target` that is safe to store in Markdown; `runtimeUrl` exists only on a resolver
result and is never serialized. `uploadMarkdownBatch` keeps success, failure, and cancellation
per file so a partial batch can be recovered without losing successful attachments.

```ts
import {
  MarkdownViewer,
  uploadMarkdownBatch,
  type MarkdownTargetResolution,
} from '@akb/markdown-editor'

const result = await uploadMarkdownBatch(uploadAdapter, files, { vault: 'team' })
// result.items contains one success/failed/cancelled outcome per file.

const resolver = {
  async resolve(target: string): Promise<MarkdownTargetResolution> {
    return { target, status: 'available', runtimeUrl: await makeRuntimeUrl(target) }
  },
}

<MarkdownViewer
  markdown={markdown}
  adapters={{ targetResolver: resolver }}
  resolverContext={{ vault: 'team' }}
/>
```

The viewer/editor apply resolver results to their rendered DOM only. The editor model and
`getMarkdown()` continue to contain the original canonical target, including when a resource is
unavailable.

## Contributor commands

```sh
pnpm install --frozen-lockfile
pnpm run build
pnpm run typecheck
pnpm run lint
pnpm run test
```

Browser conformance and packed React 19 consumer smoke are release evidence run in a temporary
directory outside this package. The scripted composition check is not a substitute for validation
with a physical OS IME.

## Versioning

The `0.7.0` public contract adds shared image description editing and body
removal by document position, with per-occurrence menus, product target and
replacement adapters, read-only handling, and independent undo steps. It also
normalizes top-level images in the shared editing surface while preserving the
serialized Markdown meaning.

The `0.6.0` public contract adds shared GFM table insertion, selection-aware
row/column commands, table-local controls, and continuation immediately below
the selected table. `MarkdownTableOptions` lets products provide labels and
styling. It also includes the shared WYSIWYG / Markdown Source surface and
`serializeEditorMarkdown`.

For a Git consumer, pin both the full commit SHA and the package subdirectory:

```sh
pnpm add 'git+https://github.com/dnotitia/akb.git#<full-40-character-sha>&path:/frontend/packages/markdown-editor'
```

The Git dependency runs `prepare` to build its public `dist` exports. With pnpm
11, add the exact resolved package selector reported by pnpm to the consumer's
`allowBuilds` map in `pnpm-workspace.yaml`, then commit that configuration and
the lockfile. Keep this approval scoped to this package.

The `0.5.0` public contract adds common document/file search UI to
`MarkdownToolbar.link`, including `searchAdapter`, `searchContext`,
`searchLabels`, and `searchClassName`. It removes the former `searchSlot` API;
products provide a `MarkdownSearchAdapter` and context instead of implementing
search controls inside the popup. Consumers continue to provide their URL
policy through `MarkdownToolbar.link.normalizeUrl`.

The `0.4.0` public contract adds the shared link command/state contract and
`MarkdownLinkPopup`.

The `0.2.0` public contract adds canonical resource targets. Consumers should pin one exact package version, store the
Markdown returned by `onChange` or `editor.getMarkdown()` as the canonical representation, and
choose `profile="structured"` when unknown HTML/MDX should be rejected or the default `preserve`
profile when those constructs must survive edit/serialize cycles. Changes to the exported schema,
Markdown profile, or serialized meaning follow semver and must include a migration note.

Products provide their own upload, search, and target-resolver implementations through the
exported adapter types. The package does not silently migrate an existing editor or storage format;
AKB and Reef integrations are separate upgrade steps.
