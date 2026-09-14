import { Editor, type JSONContent } from '@tiptap/core'
import { MarkdownManager } from '@tiptap/markdown'

import { createMarkdownExtensions } from './extensions.js'
import type {
  MarkdownCommands,
  MarkdownDocument,
  MarkdownEditorConfig,
  MarkdownParseOptions,
  MarkdownTarget,
  MarkdownTargetKind,
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

function inferTargetKind(target: string, nodeType: string): MarkdownTargetKind | null {
  if (nodeType === 'image') {
    return /^\/api\/assets\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\/?$/i.test(target)
      ? 'attachment'
      : null
  }
  if (!target.startsWith('akb://')) return null
  if (/\/file\/[^/]+$/.test(target)) return 'file'
  if (/\/doc\/[^/]+$/.test(target)) return 'document'
  return null
}

/**
 * Return the canonical resource targets represented by Markdown links and
 * images. Code blocks/spans are absent from the parsed document and therefore
 * cannot accidentally become runtime fetches.
 */
export function extractMarkdownTargets(markdown: string): MarkdownTarget[] {
  const document = parseMarkdown(markdown)
  const targets: MarkdownTarget[] = []
  const seen = new Set<string>()

  const visit = (node: JSONContent) => {
    if (node.type === 'image') {
      const target = typeof node.attrs?.target === 'string' ? node.attrs.target : ''
      const kind = target ? inferTargetKind(target, 'image') : null
      if (kind && !seen.has(target)) {
        seen.add(target)
        targets.push({ kind, target })
      }
    }
    for (const mark of node.marks ?? []) {
      if (mark.type !== 'link') continue
      const target = typeof mark.attrs?.href === 'string' ? mark.attrs.href : ''
      const kind = target ? inferTargetKind(target, 'link') : null
      if (kind && !seen.has(target)) {
        seen.add(target)
        targets.push({ kind, target })
      }
    }
    for (const child of node.content ?? []) visit(child)
  }

  for (const node of document.content ?? []) visit(node)
  return targets
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
    insertImage: (target, alt = '', title) =>
      editor.commands.insertContent({
        type: 'image',
        attrs: { target, alt, title: title ?? null },
      }),
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
