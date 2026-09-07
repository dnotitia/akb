# @akb/markdown-editor

`@akb/markdown-editor` is the product-neutral Tiptap Markdown core shared by AKB and Reef.
It owns the document schema, Markdown parsing/serialization, editor/viewer surfaces, commands,
state hooks, and the conformance contract. It does not own product UI, storage, search, uploads,
permissions, Plate, or collaboration.

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

Upload, search, and target resolution are adapter interfaces only. Products provide their own
implementations when they integrate the package.

## Contributor commands

```sh
pnpm install --frozen-lockfile
pnpm run build
pnpm run typecheck
pnpm run lint
pnpm run test
pnpm run test:browser
pnpm run test:consumer
```

`test:browser` allocates a free local port and a fresh temporary Vite/Playwright directory for
each run. Its composition test exercises browser composition events and scripted Korean text;
it is not a substitute for validation with a physical OS IME.

## Versioning

The initial public contract is `0.1.0`. Changes to the exported schema, Markdown profile, or
serialized meaning follow semver and must include a migration note. See [MIGRATION.md](MIGRATION.md).
