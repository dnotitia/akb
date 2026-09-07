# Migration guide

## 0.1.0

This is the first independent package release. Consumers should:

1. Pin one exact `@akb/markdown-editor` version alongside the exact Tiptap package set resolved by
   the package.
2. Store the Markdown returned by `onChange` or `editor.getMarkdown()` as the canonical document
   representation.
3. Use `profile="structured"` when unknown HTML/MDX should be rejected by the product contract;
   use the default `preserve` profile when those constructs must survive edit/serialize cycles.
4. Supply product-owned upload, search, and target resolver implementations through adapter types;
   the package never performs those operations itself.

The AKB product editor and Reef upgrade are separate integration steps. This package does not
silently migrate an existing editor or storage format.
