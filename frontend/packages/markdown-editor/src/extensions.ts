import {
  Extension,
  Node,
  type AnyExtension,
  type MarkdownToken,
} from '@tiptap/core'
import { Mathematics } from '@tiptap/extension-mathematics'
import { Markdown } from '@tiptap/markdown'
import StarterKit from '@tiptap/starter-kit'
import { Table } from '@tiptap/extension-table'
import TableCell from '@tiptap/extension-table-cell'
import TableHeader from '@tiptap/extension-table-header'
import TableRow from '@tiptap/extension-table-row'
import TaskItem from '@tiptap/extension-task-item'
import TaskList from '@tiptap/extension-task-list'

import type {
  MarkdownProfile,
  MarkdownSlashContext,
} from './types.js'

type RawMarkdownKind = 'html' | 'mdx'

interface RawMarkdownToken extends MarkdownToken {
  source?: string
  kind?: RawMarkdownKind
}

interface MarkdownImageToken extends MarkdownToken {
  href?: string
  title?: string | null
}

function escapeImageLabel(value: string): string {
  return value.replace(/([\\\]])/g, '\\$1')
}

function imageDestination(target: string): string {
  return /[\s()]/.test(target) ? `<${target.replace(/[<>]/g, '')}>` : target
}

/**
 * Image is intentionally part of the shared schema rather than a product
 * renderer. Its `target` attribute is the canonical Markdown value; runtime
 * adapters may change only the rendered DOM `src` and never this attribute.
 */
export const MarkdownImage = Node.create({
  name: 'image',
  inline: true,
  group: 'inline',
  atom: true,
  selectable: true,
  draggable: true,

  addAttributes() {
    return {
      target: { default: '' },
      alt: { default: '' },
      title: { default: null },
    }
  },

  parseHTML() {
    return [{ tag: 'img[src]' }]
  },

  renderHTML({ node }) {
    const target = String(node.attrs.target || node.attrs.src || '')
    return [
      'img',
      {
        src: target,
        alt: String(node.attrs.alt ?? ''),
        ...(node.attrs.title ? { title: String(node.attrs.title) } : {}),
        'data-markdown-target': target,
      },
    ]
  },

  parseMarkdown: (token: MarkdownImageToken, helpers) =>
    helpers.createNode('image', {
      target: token.href ?? '',
      alt: token.text ?? '',
      title: token.title ?? null,
    }),

  renderMarkdown: node => {
    const attrs = node.attrs ?? {}
    const target = String(attrs.target || attrs.src || '')
    const alt = String(attrs.alt ?? '')
    const title = attrs.title ? ` "${String(attrs.title).replace(/"/g, '\\"')}"` : ''
    return `![${escapeImageLabel(alt)}](${imageDestination(target)}${title})`
  },
})

const rawBlockStart = /^(?:<!--|<!\[CDATA\[|<>|<\/?[A-Za-z][A-Za-z0-9:._-]*(?:[\s/>]|$))/
const rawInlineStart = /^(?:<!--[\s\S]*?-->|<>|<\/>|<\/?[A-Za-z][A-Za-z0-9:._-]*(?:\s[^<>]*?)?\/?>)/

function classifyRaw(source: string): RawMarkdownKind {
  const tagName = source.match(/^<([A-Za-z][A-Za-z0-9:._-]*)/)?.[1]
  return tagName && /^[A-Z]/.test(tagName) ? 'mdx' : 'html'
}

function takeRawBlock(source: string): { raw: string; kind: RawMarkdownKind } | undefined {
  if (!rawBlockStart.test(source)) {
    return undefined
  }

  if (source.startsWith('<!--')) {
    const end = source.indexOf('-->')
    if (end < 0) {
      return undefined
    }

    const raw = source.slice(0, end + 3)
    return { raw, kind: 'html' }
  }

  if (source.startsWith('<![CDATA[')) {
    const end = source.indexOf(']]>')
    if (end < 0) {
      return undefined
    }

    const raw = source.slice(0, end + 3)
    return { raw, kind: 'html' }
  }

  const firstLineEnd = source.search(/\r?\n/)
  const firstLine = firstLineEnd < 0 ? source : source.slice(0, firstLineEnd)
  const fragment = firstLine.match(/^<>/) ? '</>' : undefined
  const tagName = firstLine.match(/^<([A-Za-z][A-Za-z0-9:._-]*)\b/)?.[1]
  const closingTag = tagName ? `</${tagName}>` : fragment

  if (closingTag) {
    const closeIndex = source.indexOf(closingTag, firstLine.length)
    if (closeIndex >= 0) {
      const raw = source.slice(0, closeIndex + closingTag.length)
      return { raw, kind: classifyRaw(raw) }
    }
  }

  if (firstLine.includes('/>') || firstLine === '<>') {
    return { raw: firstLine, kind: classifyRaw(firstLine) }
  }

  const paragraphEnd = source.search(/\r?\n\s*\r?\n/)
  const raw = paragraphEnd < 0 ? source : source.slice(0, paragraphEnd)
  return { raw: raw.replace(/\r?\n$/, ''), kind: classifyRaw(raw) }
}

const RawMarkdownBlock = Node.create({
  name: 'rawMarkdownBlock',
  group: 'block',
  atom: true,
  selectable: true,
  isolating: true,

  addAttributes() {
    return {
      source: { default: '' },
      kind: { default: 'html' },
    }
  },

  parseHTML() {
    return [{ tag: 'pre[data-markdown-raw]' }]
  },

  renderHTML({ node }) {
    return [
      'pre',
      {
        'data-markdown-raw': node.attrs.kind,
      },
      ['code', {}, node.attrs.source],
    ]
  },

  parseMarkdown: (token: RawMarkdownToken) => ({
    type: 'rawMarkdownBlock',
    attrs: {
      source: token.source ?? token.raw ?? '',
      kind: token.kind ?? 'html',
    },
  }),

  renderMarkdown: node => String(node.attrs?.source ?? ''),

  markdownTokenizer: {
    name: 'rawMarkdownBlock',
    level: 'block',
    start: (source: string) => {
      const match = source.match(/(?:^|\n)(?:<!--|<!\[CDATA\[|<>|<\/?[A-Za-z])/)
      return match?.index ?? -1
    },
    tokenize: (source: string) => {
      const match = takeRawBlock(source)
      if (!match) {
        return undefined
      }

      return {
        type: 'rawMarkdownBlock',
        raw: match.raw,
        source: match.raw,
        kind: match.kind,
      } satisfies RawMarkdownToken
    },
  },
})

const RawMarkdownInline = Node.create({
  name: 'rawMarkdownInline',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    return {
      source: { default: '' },
      kind: { default: 'html' },
    }
  },

  parseHTML() {
    return [{ tag: 'span[data-markdown-raw-inline]' }]
  },

  renderHTML({ node }) {
    return [
      'span',
      {
        'data-markdown-raw-inline': node.attrs.kind,
        'data-markdown-source': node.attrs.source,
      },
      node.attrs.source,
    ]
  },

  parseMarkdown: (token: RawMarkdownToken) => ({
    type: 'rawMarkdownInline',
    attrs: {
      source: token.source ?? token.raw ?? '',
      kind: token.kind ?? 'html',
    },
  }),

  renderMarkdown: node => String(node.attrs?.source ?? ''),

  markdownTokenizer: {
    name: 'rawMarkdownInline',
    level: 'inline',
    start: (source: string) => source.indexOf('<'),
    tokenize: (source: string) => {
      const match = source.match(rawInlineStart)
      if (!match) {
        return undefined
      }

      return {
        type: 'rawMarkdownInline',
        raw: match[0],
        source: match[0],
        kind: classifyRaw(match[0]),
      } satisfies RawMarkdownToken
    },
  },
})

interface MarkdownSlashOptions {
  onSlash?: (context: MarkdownSlashContext) => void
}

const MarkdownSlash = Extension.create<MarkdownSlashOptions>({
  name: 'markdownSlash',

  addOptions() {
    return { onSlash: undefined }
  },

  addKeyboardShortcuts() {
    return {
      '/': () => {
        this.options.onSlash?.({
          editor: this.editor,
          position: this.editor.state.selection.from,
        })
        return false
      },
    }
  },
})

export interface MarkdownExtensionsOptions {
  profile?: MarkdownProfile
  onSlash?: (context: MarkdownSlashContext) => void
}

export function createMarkdownExtensions({
  profile = 'preserve',
  onSlash,
}: MarkdownExtensionsOptions = {}): AnyExtension[] {
  const extensions: AnyExtension[] = [
    StarterKit.configure({
      // AKB/consumer adapters resolve these durable references to a runtime
      // URL after parsing. `akb` is only accepted as a data scheme here; no
      // adapter or network behavior belongs in the shared schema.
      link: { protocols: ['akb'] },
    }),
    MarkdownImage,
    Table.configure({ resizable: false }),
    TableRow,
    TableHeader,
    TableCell,
    TaskList,
    TaskItem.configure({ nested: true }),
    Mathematics.configure({
      katexOptions: { throwOnError: false },
    }),
  ]

  if (profile === 'preserve') {
    extensions.push(RawMarkdownBlock, RawMarkdownInline)
  }

  extensions.push(
    MarkdownSlash.configure({ onSlash }),
    Markdown.configure({
      markedOptions: {
        gfm: true,
        breaks: false,
        pedantic: false,
      },
    }),
  )

  return extensions
}

export { RawMarkdownBlock, RawMarkdownInline }
