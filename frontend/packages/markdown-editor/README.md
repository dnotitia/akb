# @akb/markdown-editor

`@akb/markdown-editor` is the product-neutral Tiptap Markdown core shared by AKB and Reef.
It owns the document schema, Markdown parsing/serialization, editor/viewer surfaces, commands,
state hooks, and the conformance contract. It does not own product UI, storage, search, uploads,
permissions, product-specific UI, or collaboration.

The package is built against the exact Tiptap `3.31.3` package set and React 19.

## Public surface

```tsx
import {
  MarkdownEditor,
  MarkdownViewer,
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

The `0.2.0` public contract adds canonical resource targets. Consumers should pin one exact package version, store the
Markdown returned by `onChange` or `editor.getMarkdown()` as the canonical representation, and
choose `profile="structured"` when unknown HTML/MDX should be rejected or the default `preserve`
profile when those constructs must survive edit/serialize cycles. Changes to the exported schema,
Markdown profile, or serialized meaning follow semver and must include a migration note.

Products provide their own upload, search, and target-resolver implementations through the
exported adapter types. The package does not silently migrate an existing editor or storage format;
AKB and Reef integrations are separate upgrade steps.
