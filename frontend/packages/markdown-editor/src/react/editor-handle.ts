import type { Editor } from '@tiptap/core'

import type { MarkdownEditorHandle } from '../types.js'

const handles = new WeakMap<Editor, MarkdownEditorHandle>()
const editors = new WeakMap<MarkdownEditorHandle, Editor>()

export function createMarkdownEditorHandle(editor: Editor): MarkdownEditorHandle {
  const existing = handles.get(editor)
  if (existing) return existing

  const handle = {} as MarkdownEditorHandle
  handles.set(editor, handle)
  editors.set(handle, editor)
  return handle
}

export function getMarkdownEditor(
  handle: MarkdownEditorHandle | null | undefined,
): Editor | null {
  return handle ? editors.get(handle) ?? null : null
}
