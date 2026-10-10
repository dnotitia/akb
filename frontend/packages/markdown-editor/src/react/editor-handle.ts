import type { Editor } from '@tiptap/core'

import type { MarkdownEditorHandle, MarkdownProfile } from '../types.js'

const handles = new WeakMap<Editor, MarkdownEditorHandle>()
const editors = new WeakMap<MarkdownEditorHandle, Editor>()
const profiles = new WeakMap<MarkdownEditorHandle, MarkdownProfile>()

export function createMarkdownEditorHandle(
  editor: Editor,
  profile: MarkdownProfile = 'preserve',
): MarkdownEditorHandle {
  const existing = handles.get(editor)
  if (existing) {
    profiles.set(existing, profile)
    return existing
  }

  const handle = {} as MarkdownEditorHandle
  handles.set(editor, handle)
  editors.set(handle, editor)
  profiles.set(handle, profile)
  return handle
}

export function getMarkdownEditor(
  handle: MarkdownEditorHandle | null | undefined,
): Editor | null {
  return handle ? editors.get(handle) ?? null : null
}

export function getMarkdownEditorProfile(
  handle: MarkdownEditorHandle | null | undefined,
): MarkdownProfile {
  return handle ? profiles.get(handle) ?? 'preserve' : 'preserve'
}
