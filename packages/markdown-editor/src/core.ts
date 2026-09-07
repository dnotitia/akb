import { Editor, type JSONContent } from '@tiptap/core'
import { MarkdownManager } from '@tiptap/markdown'

import { createMarkdownExtensions } from './extensions.js'
import type {
  MarkdownCommands,
  MarkdownDocument,
  MarkdownEditorConfig,
  MarkdownParseOptions,
} from './types.js'

function managerFor(options: MarkdownParseOptions = {}): MarkdownManager {
  return new MarkdownManager({
    extensions: createMarkdownExtensions({ profile: options.profile }),
    markedOptions: options.markedOptions,
  })
}

export function parseMarkdown(
  markdown: string,
  options: MarkdownParseOptions = {},
): MarkdownDocument {
  return managerFor(options).parse(markdown) as MarkdownDocument
}

export function serializeMarkdown(
  document: JSONContent,
  options: MarkdownParseOptions = {},
): string {
  return managerFor(options).serialize(document)
}

export function canonicalizeMarkdown(
  markdown: string,
  options: MarkdownParseOptions = {},
): string {
  return serializeMarkdown(parseMarkdown(markdown, options), options)
}

export function createMarkdownEditor(options: MarkdownEditorConfig = {}): Editor {
  const {
    initialMarkdown = '',
    profile = 'preserve',
    element,
    editable = true,
    onChange,
    onSlash,
  } = options

  const resolvedElement =
    element ?? (typeof document !== 'undefined' ? document.createElement('div') : undefined)

  return new Editor({
    element: resolvedElement,
    extensions: createMarkdownExtensions({ profile, onSlash }),
    content: initialMarkdown,
    contentType: 'markdown',
    editable,
    onUpdate: ({ editor }) => onChange?.(editor.getMarkdown(), editor),
  })
}

export function markdownCommands(editor: Editor): MarkdownCommands {
  return {
    setMarkdown: markdown => editor.commands.setContent(markdown, { contentType: 'markdown' }),
    insertMarkdown: markdown =>
      editor.commands.insertContent(markdown, { contentType: 'markdown' }),
    toggleBold: () => editor.commands.toggleBold(),
    toggleItalic: () => editor.commands.toggleItalic(),
    toggleBulletList: () => editor.commands.toggleBulletList(),
    toggleOrderedList: () => editor.commands.toggleOrderedList(),
    undo: () => editor.commands.undo(),
    redo: () => editor.commands.redo(),
    focus: position => editor.commands.focus(position),
  }
}

export function editorMarkdown(editor: Editor): string {
  return editor.getMarkdown()
}
