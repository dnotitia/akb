# @akb/markdown-editor

`@akb/markdown-editor` is the product-neutral Tiptap Markdown core shared by AKB and Reef.
It owns the document schema, Markdown parsing/serialization, editor/viewer surfaces, commands,
state hooks, the default formatting/link/table/slash controls, shared document/file search UI, and
the conformance contract. Products still own storage, permissions, search adapters and context,
copy, and product-specific URL policy.

The package is built against the exact Tiptap `3.31.3` package set and React 19.

## Public surface

```tsx
import {
  MarkdownEditor,
  MarkdownEditingSurface,
  MarkdownSurface,
  MarkdownViewer,
  MarkdownToolbar,
  DEFAULT_MARKDOWN_SLASH_COMMAND_MESSAGES,
  type MarkdownTableOptions,
  type MarkdownSlashCommandMessages,
  type MarkdownCodeOptions,
  type MarkdownContentAttributes,
  type MarkdownHeadingOptions,
  type MarkdownImageOptions,
  type MarkdownTableLayoutOptions,
  canonicalizeMarkdown,
  parseMarkdown,
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
draft. Source opens from the current editor body. WYSIWYG changes emit
canonical Markdown through `onChange`; Source changes emit the exact source
text so products can preserve authored Markdown while the shared editor stays mounted, and formatting,
link search, and the slash command menu remain attached to the WYSIWYG surface.
The editor instance returned by `useMarkdownEditor` is an opaque package handle;
it can only be passed to the package-owned surfaces, commands, and state hooks.
Products persist the Markdown delivered by `onChange` or `onSourceChange`.

`MarkdownSurface` owns the editor content element and accepts
`contentClassName` / `contentAttributes` for product styling and accessible
semantics. `MarkdownViewer` additionally accepts presentation-only
`headings`, `tableLayout`, and `image.referrerPolicy` options. Heading offsets
and ids, table scroll wrappers, runtime image attributes, and content DOM
updates are applied by the package and never become serialized Markdown:

```tsx
<MarkdownViewer
  markdown={markdown}
  contentClassName="document-content"
  contentAttributes={{ 'aria-label': 'Document body' }}
  headings={{ levelOffset: 1, ids: ['introduction'] }}
  image={{ referrerPolicy: 'no-referrer' }}
  tableLayout={{
    className: 'w-max min-w-full',
    wrapperClassName: 'overflow-x-auto',
    ariaLabel: 'Scrollable table',
  }}
/>
```

The WYSIWYG editor enables one common slash menu by default. It owns these ten
commands: Heading 1–3, Quote, Bullet list, Numbered list, Task list, Table,
Code block, and Divider. The menu filters its localized labels, descriptions,
and keywords without an extra search input. It opens only at an empty paragraph
line start, never inside inline text, code, or IME composition; selecting a
command replaces the slash/query range and leaves the cursor ready for editing.
Escape closes only the menu and preserves the draft. The menu exposes a
listbox/option ARIA relationship, keeps the active option synchronized across
keyboard and pointer input, scrolls the active option into view, and flips or
clamps to the viewport and clipping ancestors.

Products provide copy with `slash.messages` and may observe its lifecycle with
`slash.onOpenChange`. Styling is product-owned through these stable classes:
`markdown-slash-command-popup`, `markdown-slash-command-menu`,
`markdown-slash-command-options`, `markdown-slash-command-option`, and
`markdown-slash-command-option-selected`. Pass `slash={false}` for a read-only
or non-WYSIWYG surface such as `MarkdownViewer`.

The WYSIWYG editor also accepts an optional common `@` reference menu through
`reference`. Products own search, permissions, and context; the package owns
the menu lifecycle and insertion contract:

```tsx
import {
  MarkdownEditor,
  type MarkdownReferenceAdapter,
} from '@akb/markdown-editor'

const referenceAdapter: MarkdownReferenceAdapter = {
  async search(query, context) {
    return productSearch(query, context?.vault, context?.signal)
  },
}

<MarkdownEditor
  markdown={markdown}
  onChange={onChange}
  reference={{
    adapter: referenceAdapter,
    context: { vault: activeVault, document: currentDocument, commit },
    labels: {
      header: 'Insert reference',
      empty: 'No accessible references found.',
    },
  }}
/>
```

`MarkdownReferenceCandidate` uses `kind: 'person' | 'issue' | 'document' |
'file'`. People and issues use the adapter's exact `value` (falling back to
`@id` and `id`); documents and files require a durable `target` and insert the
candidate title as a Markdown link. The adapter must return canonical targets,
never signed or runtime URLs. Invalid document/file candidates are omitted from
the selectable list. Results are grouped under `data-reference-section`, and
the stable styling hooks are `markdown-reference-popup`,
`markdown-reference-menu`, `markdown-reference-options`,
`markdown-reference-option`, and `markdown-reference-option-selected`.
The menu exposes distinct loading, empty, and error states, keeps late or
cancelled responses from changing the current result, and consumes Escape
without closing a surrounding product dialog. It is disabled automatically in
read-only, Source, code, link, and IME-composition contexts. Omit `reference`
when a product does not provide a search adapter.

The same adapter can resolve references already stored in a document. Parsing
recognizes the existing compact and braced person forms (`@alice` and
`@{Ada Lovelace}`) plus plain issue IDs such as `REEF-123`; the adapter decides
whether each token exists and supplies its display name and optional runtime
route. Resolution is presentation-only, so neither the display name nor the
runtime URL is serialized:

```tsx
import {
  MarkdownEditor,
  MarkdownViewer,
  extractMarkdownReferences,
  type MarkdownReferenceAdapter,
} from '@akb/markdown-editor'

const referenceAdapter: MarkdownReferenceAdapter = {
  async search(query, context) {
    return productReferenceSearch(query, context)
  },
  async resolve(reference, context) {
    const result = await productResolveReference(reference, context)
    return result
      ? {
          ...reference,
          status: 'available',
          title: result.title,
          runtimeUrl: result.url,
        }
      : { ...reference, status: 'unavailable', reason: 'inaccessible' }
  },
}

const stored = extractMarkdownReferences(markdown)

<MarkdownEditor markdown={markdown} reference={{ adapter: referenceAdapter }} />
<MarkdownViewer markdown={markdown} reference={{ adapter: referenceAdapter }} />
```

Unknown tokens stay ordinary text. Reference discovery excludes link labels,
inline/fenced code, and escaped tokens; the public `MarkdownReferenceToken`
keeps the exact canonical value while `MarkdownReferenceResolution` carries
only ephemeral product display data. A product can also put the resolver on
`MarkdownAdapters.reference` when composing the lower-level `/react` surface.
AKB keeps its existing document/file search adapter and does not enable person
mentions or issue suggestions.

Products with a custom editor instance and toolbar can compose the same public
surface from the `/react` entry point:

```tsx
import {
  MarkdownEditingSurface,
  MarkdownSurface,
  MarkdownToolbar,
  DEFAULT_MARKDOWN_SLASH_COMMAND_MESSAGES,
  DEFAULT_MARKDOWN_REFERENCE_LABELS,
  type MarkdownReferenceAdapter,
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
    slash: { messages: DEFAULT_MARKDOWN_SLASH_COMMAND_MESSAGES },
    reference: {
      adapter: referenceAdapter,
      context: { vault: activeVault },
      labels: DEFAULT_MARKDOWN_REFERENCE_LABELS,
    },
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
      <MarkdownSurface editor={editor} editable={!readOnly} />
    </MarkdownEditingSurface>
  )
}
```

`MarkdownEditingSurface` owns mode switching, source input, external-value
synchronization, and focus handoff. Its `toolbar` slot appears only in
WYSIWYG mode; `modeLabels`, `sourceClassName`, and the source label props adapt
copy, theme, and accessible names. `modeSwitchDisabled` can lock mode changes
during an active product operation such as an upload. The optional
`autoFocus` focuses the WYSIWYG editor after it mounts when the surface is
editable. The optional
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
strikethrough, inline code, bullet/numbered/task lists, blockquote, code block,
horizontal rule, table insertion, link, and undo/redo controls. It reads the same editor state
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

The shared surfaces render the same headings, paragraphs, blockquotes, ordinary
and nested lists, task lists, and fenced code. Common languages are highlighted
with `lowlight`; an unknown language remains readable as plain code while its
fence language and content stay in the canonical Markdown model. Code blocks
are bounded scrolling regions with a keyboard-focusable `pre` and a product
provided accessible name:

```tsx
const code: MarkdownCodeOptions = {
  labels: {
    region: language =>
      language ? `Scrollable ${language} code block` : 'Scrollable code block',
  },
}

<MarkdownEditor markdown={markdown} code={code} />
<MarkdownViewer markdown={markdown} code={code} />
```

Nested task checkboxes are independently editable, preserve their checked
state through save/reopen, and are disabled on read-only surfaces. The
checkbox labels and code-region semantics are presentation-only; toggling a
task updates the editor Markdown through the existing `onChange` contract,
while highlighting and accessibility attributes never become serialized
Markdown.

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
result and is never serialized. A resolver may return `expiresAt` for a short-lived runtime URL;
the shared `useMarkdownTargetResolutions` hook re-resolves before expiry and discards results from
an obsolete document context. `uploadMarkdownBatch` keeps success, failure, and cancellation per
file so a partial batch can be recovered without losing successful attachments.

`MarkdownEditingSurface` can own the complete image flow when a product supplies
`imageUpload`. The shared surface handles file selection, standalone image paste, drop,
serial progress, cancellation, partial failure, retry, and image replacement. It stores an
editor selection bookmark before the asynchronous work begins and maps that bookmark through
later edits, so a successful upload returns to its original logical location. The product keeps
validation, storage, authorization, canonical Attachment targets, and claim/preserve/discard
policy in the adapter callbacks.

```tsx
<MarkdownEditingSurface
  editor={editor}
  markdown={markdown}
  imageUpload={{
    adapter: uploadAdapter,
    context: { vault: 'team', document: 'notes/guide.md', draftId },
    onAssetUploaded: (asset, file) => trackUnclaimed(asset, file),
    onAssetReplaced: previousTarget => discardUnclaimed(previousTarget),
  }}
  imageMenu={{ isEditableTarget: targetPolicy }}
  toolbar={<MarkdownToolbar editor={editor} />}
>
  <MarkdownSurface editor={editor} editable />
</MarkdownEditingSurface>
```

`MarkdownImageUploadOptions.onAssetUploaded` runs for every completed remote asset,
including a result that finishes after cancellation, so the product can retain or discard it
according to its draft policy. `onAssetReplaced` runs only after the first replacement target
was updated successfully. Failed or cancelled items are never inserted into Markdown, and the
shared UI retries only retryable items.

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

The viewer/editor apply resolver results to their rendered DOM only. Change
callbacks and `MarkdownState.markdown` continue to contain the original
canonical target, including when a resource is unavailable.

`MarkdownSurface` is the shared image renderer for products that own an editor instance. It keeps
the canonical target, `alt`, and `title` in the Tiptap node, while a resolver supplies only the
ephemeral `runtimeUrl`. Available images use `max-width: 100%` and `height: auto`; loading,
inaccessible, and browser decode failures are rendered as accessible text states in both editor
and viewer surfaces. A resolution may provide `release()` for object URLs and `refresh()` for a
grant-bearing URL that failed to load. Both callbacks are cancelled and cleaned up when the
document, resource context, or surface changes.

```tsx
import {
  MarkdownSurface,
  useMarkdownEditor,
  useMarkdownTargetResolutions,
  type MarkdownImageOptions,
} from '@akb/markdown-editor/react'

function ProductSurface({ markdown, resolver }: Props) {
  const editor = useMarkdownEditor({ initialMarkdown: markdown })
  const resolutions = useMarkdownTargetResolutions(markdown, resolver, {
    vault: 'team',
    document: 'notes/guide.md',
  })
  const image: MarkdownImageOptions = {
    labels: {
      loading: alt => alt ? `Loading ${alt}` : 'Loading image',
      unavailable: alt => alt ? `Unavailable: ${alt}` : 'Image unavailable',
    },
  }

  return (
    <MarkdownSurface
      editor={editor}
      editable
      image={image}
      resolutions={resolutions}
      resolvingTargets={Boolean(resolver)}
    />
  )
}
```

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

The `0.15.0` public contract closes the editor boundary around the package-owned
`MarkdownEditorHandle`. Raw Tiptap editor values, `EditorContent`, editor
factories, and engine serialization helpers are no longer public; lower-level
React surfaces continue to compose through the opaque handle.

The `0.14.0` public contract adds canonical WYSIWYG `onChange` serialization and moves
editor content attributes, viewer heading/table presentation, image request
policy, and autofocus into the shared public surface contract. These options
are presentation-only and do not change canonical Markdown.

The `0.13.0` public contract adds shared block rendering for editor/viewer
surfaces, common-language code highlighting, keyboard-focusable code scroll
regions, nested task checkbox behavior, and `MarkdownCodeOptions` for product
accessible copy. Code decoration and accessibility attributes are
presentation-only; canonical Markdown content, fence languages, and checked
states remain unchanged.

The `0.12.0` public contract adds shared stored-reference resolution for
`MarkdownEditor` and `MarkdownViewer`, preserving canonical Markdown while
products supply display names, runtime routes, and access policy.

The `0.11.0` public contract adds the common image rendering surface and
resource lifecycle hooks. `MarkdownSurface`, `MarkdownEditor`, and
`MarkdownViewer` share canonical target preservation, aspect-ratio-safe sizing,
loading/access/decode failure states, cancellation, object-URL release, and
grant refresh. Products still own authentication, publication policy, storage,
upload, and copy.

The `0.10.0` public contract adds the common `@` reference menu, the
`MarkdownReferenceAdapter`/`MarkdownReferenceCandidate` contract, canonical
document/file link insertion, grouped person/issue/document/file rendering,
distinct async states, cancellation and stale-response handling, and the live
React context bridge. It does not add product-specific people, issue, search,
permission, or persistence behavior.

The `0.9.0` public contract adds the shared ten-command slash menu, localized
filtering, keyboard/pointer selection, ARIA state, viewport/clipping-boundary
placement, IME/read-only guards, and the shared task-list command used by the
toolbar and slash menu. It removes the former `onSlash` callback in favor of
the menu contract described above.

The `0.8.0` public contract adds the shared image upload surface, mapped
insertion/replacement targets, per-file retry/cancellation state, and the
product-owned asset lifecycle callbacks described above.

The `0.7.0` public contract adds shared image description editing and body
removal by document position, with per-occurrence menus, product target and
replacement adapters, read-only handling, and independent undo steps. It also
normalizes top-level images in the shared editing surface while preserving the
serialized Markdown meaning.

The `0.6.0` public contract adds shared GFM table insertion, selection-aware
row/column commands, table-local controls, and continuation immediately below
the selected table. `MarkdownTableOptions` lets products provide labels and
styling. It also includes the shared WYSIWYG / Markdown Source surface.

For a Git consumer, pin both the full commit SHA and the package subdirectory:

```sh
pnpm --allow-build='@akb/markdown-editor@https://codeload.github.com/dnotitia/akb/tar.gz/<full-40-character-sha>#path:/frontend/packages/markdown-editor' \
  add 'git+https://github.com/dnotitia/akb.git#<full-40-character-sha>&path:/frontend/packages/markdown-editor'
```

The Git dependency runs `prepare` to build its public `dist` exports. With pnpm
11, pass the exact resolved package selector to `--allow-build` on the first
`pnpm add`; pnpm writes the scoped `allowBuilds` entry to
`pnpm-workspace.yaml`. Do this before any install or lockfile-only resolution,
then commit that configuration and the lockfile. Keep this approval scoped to
this package. Finish a cold consumer setup with `pnpm install --frozen-lockfile`
before importing either public entry point.

The `0.5.0` public contract adds common document/file search UI to
`MarkdownToolbar.link`, including `searchAdapter`, `searchContext`,
`searchLabels`, and `searchClassName`. It removes the former `searchSlot` API;
products provide a `MarkdownSearchAdapter` and context instead of implementing
search controls inside the popup. Consumers continue to provide their URL
policy through `MarkdownToolbar.link.normalizeUrl`.

The `0.4.0` public contract adds the shared link command/state contract and
`MarkdownLinkPopup`.

The `0.2.0` public contract adds canonical resource targets. Consumers should pin one exact package version, store the
Markdown returned by `onChange` or `onSourceChange` as the canonical representation, and
choose `profile="structured"` when unknown HTML/MDX should be rejected or the default `preserve`
profile when those constructs must survive edit/serialize cycles. Changes to the exported schema,
Markdown profile, or serialized meaning follow semver and must include a migration note.

Products provide their own upload, search, and target-resolver implementations through the
exported adapter types. The package does not silently migrate an existing editor or storage format;
AKB and Reef integrations are separate upgrade steps.
