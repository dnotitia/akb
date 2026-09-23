import { Editor, type JSONContent } from '@tiptap/core'
import { MarkdownManager } from '@tiptap/markdown'
import { closeHistory } from '@tiptap/pm/history'

import { createMarkdownExtensions, parseMarkdownReferenceToken } from './extensions.js'
import { createMarkdownEditorHandle } from './react/editor-handle.js'
import { markdownTableCommands } from './table.js'
import type {
  MarkdownCommands,
  MarkdownDocument,
  MarkdownEditorConfig,
  MarkdownHeadingLevel,
  MarkdownNode,
  MarkdownParseOptions,
  MarkdownReferenceToken,
  MarkdownTarget,
  MarkdownTargetKind,
} from './types.js'

export { parseMarkdownReferenceToken }

function managerFor(options: MarkdownParseOptions = {}): MarkdownManager {
  return new MarkdownManager({
    extensions: createMarkdownExtensions({ profile: options.profile }),
  })
}

export function parseMarkdown(
  markdown: string,
  options: MarkdownParseOptions = {},
): MarkdownDocument {
  const document = managerFor(options).parse(markdown) as unknown as MarkdownDocument
  stripExcludedMarkdownReferences(document)
  return document
}

export function serializeMarkdown(
  document: MarkdownDocument,
  options: MarkdownParseOptions = {},
): string {
  return managerFor(options).serialize(document as JSONContent)
}

/**
 * Serialize the current editor document without persisting the empty paragraph
 * that Tiptap keeps after a terminal atomic block for keyboard continuation.
 */
export function serializeEditorMarkdown(
  editor: Editor,
  options: MarkdownParseOptions = {},
): string {
  const document = editor.getJSON()
  const content = document.content
  const last = content?.at(-1)
  if (
    content &&
    content.length > 1 &&
    last?.type === 'paragraph' &&
    (!last.content || last.content.length === 0)
  ) {
    document.content = content.slice(0, -1)
  }
  return serializeMarkdown(document, options)
}

export function canonicalizeMarkdown(
  markdown: string,
  options: MarkdownParseOptions = {},
): string {
  return serializeMarkdown(parseMarkdown(markdown, options), options)
}

function stripExcludedMarkdownReferences(document: MarkdownDocument): void {
  const visit = (node: MarkdownNode, excluded = false) => {
    const nodeExcluded =
      excluded ||
      node.type === 'codeBlock' ||
      node.type === 'rawMarkdownBlock' ||
      node.marks?.some(mark => mark.type === 'link' || mark.type === 'code') === true

    if (nodeExcluded && node.marks) {
      node.marks = node.marks.filter(
        mark => mark.type !== 'markdownReference' || mark.attrs?.escaped === true,
      )
    }
    for (const child of node.content ?? []) visit(child, nodeExcluded)
  }

  for (const node of document.content ?? []) visit(node)
}

/**
 * Find the distinct person and issue tokens represented by the shared schema.
 * Excluded Markdown regions are removed during parsing, so this function never
 * asks a product adapter to resolve a link label or code sample.
 */
export function extractMarkdownReferences(markdown: string): MarkdownReferenceToken[] {
  const document = parseMarkdown(markdown)
  const references: MarkdownReferenceToken[] = []
  const seen = new Set<string>()

  const visit = (node: MarkdownNode) => {
    if (node.type === 'text') {
      const referenceMark = node.marks?.find(mark => mark.type === 'markdownReference')
      if (referenceMark && referenceMark.attrs?.escaped !== true) {
        const value = typeof node.text === 'string' ? node.text : ''
        const reference = parseMarkdownReferenceToken(value)
        if (reference) {
          const key = markdownReferenceKey(reference)
          if (!seen.has(key)) {
            seen.add(key)
            references.push(reference)
          }
        }
      }
    }
    for (const child of node.content ?? []) visit(child)
  }

  for (const node of document.content ?? []) visit(node)
  return references
}

export function markdownReferenceKey(reference: Pick<MarkdownReferenceToken, 'kind' | 'value'>): string {
  return `${reference.kind}\u0000${reference.value}`
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

  const visit = (node: MarkdownNode) => {
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
    editable = true,
    image,
    onChange,
  } = options

  const resolvedElement =
    typeof document !== 'undefined' ? document.createElement('div') : undefined

  return new Editor({
    element: resolvedElement,
    extensions: createMarkdownExtensions({ profile, image }),
    content: initialMarkdown,
    contentType: 'markdown',
    editable,
    onUpdate: ({ editor }) =>
      onChange?.(
        serializeEditorMarkdown(editor, { profile }),
        createMarkdownEditorHandle(editor),
      ),
  })
}

export function markdownCommands(editor: Editor): MarkdownCommands {
  return {
    ...markdownTableCommands(editor),
    setMarkdown: markdown => editor.commands.setContent(markdown, { contentType: 'markdown' }),
    insertMarkdown: markdown =>
      editor.commands.insertContent(markdown, { contentType: 'markdown' }),
    insertImage: (target, alt = '', title) =>
      editor.commands.insertContent({
        type: 'image',
        attrs: { target, alt, title: title ?? null },
      }),
    replaceImageAt: (position, target, alt = '', title) => {
      if (
        !editor.isEditable ||
        !Number.isInteger(position) ||
        position < 0 ||
        typeof target !== 'string' ||
        !target ||
        editor.state.doc.nodeAt(position)?.type.name !== 'image'
      ) {
        return false
      }

      return editor
        .chain()
        .command(({ tr }) => {
          closeHistory(tr)
          return true
        })
        .setNodeSelection(position)
        .updateAttributes('image', { target, alt, title: title ?? null })
        .focus()
        .run()
    },
    setImageAltAt: (position, alt) => {
      if (
        !editor.isEditable ||
        !Number.isInteger(position) ||
        position < 0 ||
        typeof alt !== 'string' ||
        editor.state.doc.nodeAt(position)?.type.name !== 'image'
      ) {
        return false
      }

      return editor
        .chain()
        .command(({ tr }) => {
          closeHistory(tr)
          return true
        })
        .setNodeSelection(position)
        .updateAttributes('image', { alt })
        .focus()
        .run()
    },
    deleteImageAt: position => {
      if (
        !editor.isEditable ||
        !Number.isInteger(position) ||
        position < 0 ||
        editor.state.doc.nodeAt(position)?.type.name !== 'image'
      ) {
        return false
      }

      return editor
        .chain()
        .command(({ tr }) => {
          closeHistory(tr)
          return true
        })
        .setNodeSelection(position)
        .deleteSelection()
        .focus()
        .run()
    },
    setLink: href =>
      editor.chain().focus().extendMarkRange('link').setLink({ href }).run(),
    insertLink: (text, href) =>
      editor
        .chain()
        .focus()
        .insertContent({
          type: 'text',
          text,
          marks: [{ type: 'link', attrs: { href } }],
        })
        .run(),
    unsetLink: () => editor.chain().focus().extendMarkRange('link').unsetLink().run(),
    setParagraph: () => editor.commands.setParagraph(),
    toggleHeading: (level: MarkdownHeadingLevel) =>
      editor.commands.toggleHeading({ level }),
    toggleBold: () => editor.commands.toggleBold(),
    toggleItalic: () => editor.commands.toggleItalic(),
    toggleStrike: () => editor.commands.toggleStrike(),
    toggleCode: () => editor.commands.toggleCode(),
    toggleBulletList: () => editor.commands.toggleBulletList(),
    toggleOrderedList: () => editor.commands.toggleOrderedList(),
    toggleTaskList: () => editor.commands.toggleTaskList(),
    toggleBlockquote: () => editor.commands.toggleBlockquote(),
    toggleCodeBlock: () => editor.commands.toggleCodeBlock(),
    setHorizontalRule: () => editor.commands.setHorizontalRule(),
    undo: () => editor.commands.undo(),
    redo: () => editor.commands.redo(),
    focus: position => editor.commands.focus(position),
  }
}

export function editorMarkdown(editor: Editor): string {
  return editor.getMarkdown()
}
