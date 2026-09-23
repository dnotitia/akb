import {
  getRenderedAttributes,
  Mark,
  Node,
  mergeAttributes,
  type AnyExtension,
  type MarkdownToken,
} from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { Link } from '@tiptap/extension-link'
import { Mathematics } from '@tiptap/extension-mathematics'
import { Markdown } from '@tiptap/markdown'
import StarterKit from '@tiptap/starter-kit'
import CodeBlockLowlight from '@tiptap/extension-code-block-lowlight'
import type { CodeBlockLowlightOptions } from '@tiptap/extension-code-block-lowlight'
import { Table } from '@tiptap/extension-table'
import TableCell from '@tiptap/extension-table-cell'
import TableHeader from '@tiptap/extension-table-header'
import TableRow from '@tiptap/extension-table-row'
import TaskItem from '@tiptap/extension-task-item'
import TaskList from '@tiptap/extension-task-list'
import { common, createLowlight } from 'lowlight'

import type {
  MarkdownCodeLabels,
  MarkdownCodeOptions,
  MarkdownImageOptions,
  MarkdownInlineReferenceKind,
  MarkdownProfile,
  MarkdownReferenceToken,
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

const MARKDOWN_REFERENCE_MARK = 'markdownReference'

export const DEFAULT_MARKDOWN_CODE_LABELS: MarkdownCodeLabels = {
  region: (language?: string) =>
    language ? `Scrollable ${language} code block` : 'Scrollable code block',
}

// Keep one registry for every editor instance. CodeBlockLowlight decorates the
// rendered code without changing the ProseMirror document or its Markdown.
const markdownLowlight = createLowlight(common)

interface MarkdownCodeBlockOptions extends Partial<CodeBlockLowlightOptions> {
  labels: MarkdownCodeLabels
}

const MarkdownCodeBlock = CodeBlockLowlight.extend<MarkdownCodeBlockOptions>({
  addOptions() {
    return {
      ...this.parent?.(),
      labels: DEFAULT_MARKDOWN_CODE_LABELS,
    }
  },

  renderHTML({ node, HTMLAttributes }) {
    const parent = this.parent?.({ node, HTMLAttributes })
    if (!parent || !Array.isArray(parent) || typeof parent[0] !== 'string') {
      return ['pre', HTMLAttributes, ['code', {}, 0]]
    }

    const attributes = parent[1] && typeof parent[1] === 'object' && !Array.isArray(parent[1])
      ? parent[1]
      : {}
    const children = Array.isArray(parent[1]) || typeof parent[1] === 'string'
      ? parent.slice(1)
      : parent.slice(2)
    const language = typeof node.attrs.language === 'string' && node.attrs.language
      ? node.attrs.language
      : undefined

    return [
      parent[0],
      mergeAttributes(attributes, {
        'data-markdown-code': 'true',
        'data-markdown-code-language': language,
        role: 'region',
        tabindex: '0',
        'aria-label': this.options.labels.region(language),
      }),
      ...children,
    ]
  },
})

const MarkdownTaskItem = TaskItem.extend({
  addNodeView() {
    return ({ node, HTMLAttributes, getPos, editor }) => {
      const listItem = document.createElement('li')
      const checkboxWrapper = document.createElement('label')
      const checkboxStyler = document.createElement('span')
      const checkbox = document.createElement('input')
      const content = document.createElement('div')

      checkboxStyler.style.cssText =
        'position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0'

      const updateA11y = (currentNode: ProseMirrorNode) => {
        const label = this.options.a11y?.checkboxLabel?.(
          currentNode,
          currentNode.attrs.checked,
        ) || `Task item checkbox for ${currentNode.textContent || 'empty task item'}`

        checkbox.setAttribute('aria-label', label)
        checkboxStyler.textContent = label
      }

      updateA11y(node)

      checkboxWrapper.contentEditable = 'false'
      checkbox.type = 'checkbox'
      checkbox.addEventListener('mousedown', event => event.preventDefault())
      const handleCheckboxChange = (event: Event) => {
        event.stopImmediatePropagation()

        const position = getPos()
        if (typeof position !== 'number') return

        const currentNode = editor.state.doc.nodeAt(position)
        if (!editor.isEditable) {
          checkbox.checked = Boolean(currentNode?.attrs.checked)
          return
        }

        if (!currentNode) return

        editor.view.dispatch(
          editor.state.tr.setNodeMarkup(position, undefined, {
            ...currentNode.attrs,
            checked: checkbox.checked,
          }),
        )

        const focusUpdatedCheckbox = () => {
          const nextDom = editor.view.nodeDOM(position)
          const nextCheckbox = nextDom instanceof HTMLElement
            ? nextDom.querySelector<HTMLInputElement>('input[type="checkbox"]')
            : null
          if (nextCheckbox && !nextCheckbox.disabled) nextCheckbox.focus()
        }
        const defaultView = editor.view.dom.ownerDocument.defaultView
        if (defaultView) defaultView.setTimeout(focusUpdatedCheckbox, 0)
        else focusUpdatedCheckbox()
      }
      checkbox.addEventListener('change', handleCheckboxChange, true)

      Object.entries(this.options.HTMLAttributes).forEach(([key, value]) => {
        listItem.setAttribute(key, value)
      })

      listItem.dataset.checked = node.attrs.checked
      checkbox.checked = node.attrs.checked

      checkboxWrapper.append(checkbox, checkboxStyler)
      listItem.append(checkboxWrapper, content)

      Object.entries(HTMLAttributes).forEach(([key, value]) => {
        listItem.setAttribute(key, value)
      })

      let previousHTMLAttributeKeys = new Set(Object.keys(HTMLAttributes))

      return {
        dom: listItem,
        contentDOM: content,
        update: updatedNode => {
          if (updatedNode.type !== this.type) return false

          listItem.dataset.checked = updatedNode.attrs.checked
          checkbox.checked = updatedNode.attrs.checked
          updateA11y(updatedNode)

          const nextHTMLAttributes = getRenderedAttributes(
            updatedNode,
            editor.extensionManager.attributes,
          )
          const nextKeys = new Set(Object.keys(nextHTMLAttributes))
          previousHTMLAttributeKeys.forEach(key => {
            if (nextKeys.has(key) || key in this.options.HTMLAttributes) return
            listItem.removeAttribute(key)
          })
          Object.entries(nextHTMLAttributes).forEach(([key, value]) => {
            if (value === null || value === undefined) {
              if (key in this.options.HTMLAttributes) listItem.setAttribute(key, this.options.HTMLAttributes[key])
              else listItem.removeAttribute(key)
            } else {
              listItem.setAttribute(key, value)
            }
          })
          previousHTMLAttributeKeys = nextKeys

          return true
        },
        destroy() {
          checkbox.removeEventListener('change', handleCheckboxChange, true)
        },
      }
    }
  },
})

const RUNTIME_REFERENCE_ATTRIBUTES = new Set([
  'aria-disabled',
  'aria-label',
  'data-markdown-reference-id',
  'data-markdown-reference-kind',
  'data-markdown-reference-label',
  'data-markdown-reference-resolution',
  'data-markdown-reference-runtime-url',
  'data-markdown-reference-title',
  'data-markdown-reference-value',
  'href',
  'rel',
  'target',
  'title',
])

function codePointAt(value: string, index: number): string | undefined {
  const codePoint = value.codePointAt(index)
  return codePoint === undefined ? undefined : String.fromCodePoint(codePoint)
}

function codePointWidth(value: string, index: number): number {
  const codePoint = value.codePointAt(index)
  return codePoint !== undefined && codePoint > 0xffff ? 2 : 1
}

function previousCodePoint(value: string, index: number): string | undefined {
  if (index <= 0) return undefined
  const previousIndex = index - 1
  const codePoint = value.codePointAt(previousIndex)
  if (codePoint === undefined) return undefined
  const start = previousIndex - (codePoint > 0xffff ? 1 : 0)
  return value.slice(start, index)
}

function isLetterOrNumber(value: string | undefined): boolean {
  return value !== undefined && /[\p{L}\p{N}]/u.test(value)
}

function isReferenceBoundary(value: string | undefined): boolean {
  return value === undefined || (!isLetterOrNumber(value) && !/[_-]/u.test(value))
}

function isEscaped(value: string, index: number): boolean {
  let slashes = 0
  for (let cursor = index - 1; cursor >= 0 && value[cursor] === '\\'; cursor -= 1) {
    slashes += 1
  }
  return slashes % 2 === 1
}

function findUnescaped(value: string, character: string, start: number): number {
  for (let cursor = start; cursor < value.length; cursor += 1) {
    if (value[cursor] === character && !isEscaped(value, cursor)) return cursor
  }
  return -1
}

function parseReferenceAt(
  value: string,
  start: number,
  allowEscaped = false,
): MarkdownReferenceToken | null {
  const character = value[start]

  if (character === '@') {
    if (!allowEscaped && isEscaped(value, start)) return null
    const previous = previousCodePoint(value, start)
    if (previous === '@' || isLetterOrNumber(previous)) return null

    if (value[start + 1] === '{') {
      const close = findUnescaped(value, '}', start + 2)
      if (close === -1) return null
      const id = value.slice(start + 2, close)
      if (!id || /[\r\n]/u.test(id)) return null
      return {
        kind: 'person',
        id,
        value: value.slice(start, close + 1),
      }
    }

    if (!isLetterOrNumber(codePointAt(value, start + 1))) return null
    let end = start + 1
    while (isLetterOrNumber(codePointAt(value, end))) {
      end += codePointWidth(value, end)
    }
    if (value[end] === '\\' || value[end] === '@') return null
    const id = value.slice(start + 1, end)
    return { kind: 'person', id, value: value.slice(start, end) }
  }

  if (!character || !/[A-Za-z]/u.test(character) || (!allowEscaped && isEscaped(value, start))) {
    return null
  }
  const previous = previousCodePoint(value, start)
  if (!isReferenceBoundary(previous)) return null
  const match = value.slice(start).match(/^[A-Za-z][A-Za-z0-9_]*-\d+/u)
  if (!match) return null
  const token = match[0]
  if (!isReferenceBoundary(value[start + token.length])) return null
  return { kind: 'issue', id: token, value: token }
}

function findMarkdownReference(value: string): {
  token: MarkdownReferenceToken
  index: number
  escaped: boolean
} | null {
  for (let cursor = 0; cursor < value.length; cursor += 1) {
    if (value[cursor] === '\\') {
      const escapedToken = parseReferenceAt(value, cursor + 1, true)
      if (escapedToken && isEscaped(value, cursor + 1)) {
        return { token: escapedToken, index: cursor, escaped: true }
      }
      continue
    }
    if (value[cursor] !== '@' && !/[A-Za-z]/u.test(value[cursor] ?? '')) continue
    const token = parseReferenceAt(value, cursor)
    if (token) return { token, index: cursor, escaped: false }
  }
  return null
}

/** Parse one complete canonical person or issue token. */
export function parseMarkdownReferenceToken(value: string): MarkdownReferenceToken | null {
  const match = parseReferenceAt(value, 0)
  return match && match.value === value ? match : null
}

function createMarkdownReferenceTokenizer() {
  return {
    name: MARKDOWN_REFERENCE_MARK,
    level: 'inline' as const,
    start(source: string) {
      // Marked asks `start` about the input after its first code unit. A
      // leading slash there may be the second slash of `\\\\TOKEN`; letting
      // the default escape/text tokenizer consume that prefix avoids starting
      // a partial match at the second character of an issue ID.
      if (source.startsWith('\\')) return -1
      return findMarkdownReference(source)?.index ?? -1
    },
    tokenize(source: string, tokens: MarkdownToken[]): MarkdownToken | undefined {
      const match = findMarkdownReference(source)
      if (!match || match.index !== 0) return undefined
      const previous = tokens.at(-1)
      if (previous?.type === 'text' && previous.raw?.endsWith('\\')) {
        return undefined
      }
      if (match.escaped && previous?.type === 'escape') {
        return undefined
      }
      if (previous?.type === 'text' && match.token.kind === 'issue') {
        const escapedPrefix = previous.raw?.match(/\\([A-Za-z]+)$/u)?.[1]
        if (
          escapedPrefix &&
          parseMarkdownReferenceToken(`${escapedPrefix}${match.token.value}`)?.kind === 'issue'
        ) {
          return undefined
        }
      }
      return {
        type: MARKDOWN_REFERENCE_MARK,
        raw: match.escaped ? `\\${match.token.value}` : match.token.value,
        text: match.token.value,
        attributes: { ...match.token, escaped: match.escaped },
      }
    },
  }
}

function parseMarkdownReference(
  token: MarkdownToken,
  helpers: Parameters<NonNullable<AnyExtension['config']['parseMarkdown']>>[1],
) {
  const value = typeof token.text === 'string' ? token.text : token.raw ?? ''
  const parsed = parseMarkdownReferenceToken(value)
  const attributes = token.attributes as (Partial<MarkdownReferenceToken> & { escaped?: boolean }) | undefined
  const kind: MarkdownInlineReferenceKind =
    parsed?.kind ?? (attributes?.kind === 'issue' ? 'issue' : 'person')
  const id = parsed?.id ?? (typeof attributes?.id === 'string' ? attributes.id : value)
  return helpers.applyMark(
    MARKDOWN_REFERENCE_MARK,
    [helpers.createTextNode(value)],
    { kind, id, value, escaped: attributes?.escaped === true },
  )
}

/**
 * A reference is a mark over the authored token, not an atom. This keeps the
 * token editable by normal ProseMirror text input while the React surface adds
 * product-only labels and destinations around the same text node.
 */
export const MarkdownReference = Mark.create({
  name: MARKDOWN_REFERENCE_MARK,
  inclusive: false,

  addAttributes() {
    return {
      kind: { default: 'person' },
      id: { default: '' },
      value: { default: '' },
      escaped: { default: false },
    }
  },

  parseHTML() {
    return [{ tag: 'a[data-markdown-reference]' }]
  },

  addMarkView() {
    return ({ HTMLAttributes }) => {
      const dom = document.createElement('a')
      const token = document.createElement('span')
      dom.dataset.markdownReference = 'true'
      dom.dataset.markdownReferenceKind = String(HTMLAttributes.kind ?? '')
      dom.dataset.markdownReferenceId = String(HTMLAttributes.id ?? '')
      dom.dataset.markdownReferenceValue = String(HTMLAttributes.value ?? '')
      dom.dataset.markdownReferenceEscaped = String(HTMLAttributes.escaped ?? false)
      token.dataset.markdownReferenceToken = 'true'
      dom.append(token)
      for (const [name, value] of Object.entries(HTMLAttributes)) {
        if (value === null || value === undefined || value === false) continue
        if (name === 'kind' || name === 'id' || name === 'value' || name === 'escaped') continue
        dom.setAttribute(name, value === true ? '' : String(value))
      }

      return {
        dom,
        contentDOM: token,
        ignoreMutation: mutation => {
          if (
            mutation.type === 'attributes' &&
            RUNTIME_REFERENCE_ATTRIBUTES.has(mutation.attributeName ?? '')
          ) {
            return true
          }
          return mutation.type === 'childList' && mutation.target !== token
        },
      }
    }
  },

  markdownTokenizer: createMarkdownReferenceTokenizer(),
  parseMarkdown: parseMarkdownReference,
  renderMarkdown: (
    node: { attrs?: { escaped?: boolean }; content?: Array<{ text?: string }> },
    helpers: { renderChildren: (content: Array<{ text?: string }>) => string },
  ) => `${node.attrs?.escaped ? '\\' : ''}${helpers.renderChildren(node.content ?? [])}`,
  renderHTML({ mark, HTMLAttributes }) {
    const attributes = Object.fromEntries(
      Object.entries(HTMLAttributes).filter(
        ([name]) => !['kind', 'id', 'value', 'escaped'].includes(name),
      ),
    )
    return [
      'a',
      mergeAttributes(attributes, {
        'data-markdown-reference': 'true',
        'data-markdown-reference-kind': String(mark.attrs.kind ?? ''),
        'data-markdown-reference-id': String(mark.attrs.id ?? ''),
        'data-markdown-reference-value': String(mark.attrs.value ?? ''),
        'data-markdown-reference-escaped': String(mark.attrs.escaped ?? false),
      }),
      0,
    ]
  },
})

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
export const MarkdownImage = Node.create<{ referrerPolicy?: string }>({
  name: 'image',
  inline: true,
  group: 'inline',
  atom: true,
  selectable: true,
  draggable: true,

  addOptions() {
    return {
      referrerPolicy: undefined,
    }
  },

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
      'span',
      {
        'data-markdown-image-frame': 'true',
        'data-markdown-target': target,
      },
      [
        'img',
        {
          src: target,
          ...(this.options.referrerPolicy
            ? { referrerpolicy: this.options.referrerPolicy }
            : {}),
          alt: String(node.attrs.alt ?? ''),
          ...(node.attrs.title ? { title: String(node.attrs.title) } : {}),
          'data-markdown-image': 'true',
          'data-markdown-target': target,
        },
      ],
      [
        'span',
        {
          'data-markdown-image-message': 'true',
          'aria-hidden': 'true',
        },
      ],
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

const RUNTIME_LINK_ATTRIBUTES = new Set([
  'aria-disabled',
  'aria-label',
  'data-markdown-resolution',
  'data-markdown-target',
  'href',
  'title',
])

/**
 * Product adapters add runtime-only link attributes to the editable DOM. Keep
 * those writes out of ProseMirror's document parser; the mark model and
 * Markdown continue to own the canonical href.
 */
const MarkdownLink = Link.extend({
  addMarkView() {
    return ({ HTMLAttributes }) => {
      const dom = document.createElement('a')
      for (const [name, value] of Object.entries(HTMLAttributes)) {
        if (value === null || value === undefined || value === false) continue
        dom.setAttribute(name, value === true ? '' : String(value))
      }

      return {
        dom,
        contentDOM: dom,
        ignoreMutation: mutation =>
          mutation.type === 'attributes' &&
          RUNTIME_LINK_ATTRIBUTES.has(mutation.attributeName ?? ''),
      }
    }
  },
})

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

export interface MarkdownExtensionsOptions {
  profile?: MarkdownProfile
  code?: MarkdownCodeOptions
  image?: Pick<MarkdownImageOptions, 'referrerPolicy'>
}

export function createMarkdownExtensions({
  profile = 'preserve',
  code,
  image,
}: MarkdownExtensionsOptions = {}): AnyExtension[] {
  const extensions: AnyExtension[] = [
    StarterKit.configure({
      // AKB/consumer adapters resolve these durable references to a runtime
      // URL after parsing. `akb` is only accepted as a data scheme here; no
      // adapter or network behavior belongs in the shared schema.
      link: false,
      codeBlock: false,
    }),
    MarkdownCodeBlock.configure({
      lowlight: markdownLowlight,
      labels: { ...DEFAULT_MARKDOWN_CODE_LABELS, ...code?.labels },
    }),
    MarkdownLink.configure({ protocols: ['akb'] }),
    MarkdownReference,
    MarkdownImage.configure({ referrerPolicy: image?.referrerPolicy }),
    Table.configure({ resizable: false }),
    TableRow,
    TableHeader,
    TableCell,
    TaskList,
    MarkdownTaskItem.configure({
      nested: true,
      HTMLAttributes: { 'data-markdown-task-item': 'true' },
      a11y: {
        checkboxLabel: node =>
          `Task item checkbox for ${node.firstChild?.textContent || 'empty task item'}`,
      },
    }),
    Mathematics.configure({
      katexOptions: { throwOnError: false },
    }),
  ]

  if (profile === 'preserve') {
    extensions.push(RawMarkdownBlock, RawMarkdownInline)
  }

  extensions.push(
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
