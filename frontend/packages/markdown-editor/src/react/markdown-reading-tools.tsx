import { Fragment, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import type { Editor } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

export interface ReadingTextMatch { from: number; to: number }

/** Literal, Unicode-aware matching. Positions are UTF-16 offsets, like DOM and editor positions. */
export function findReadingTextMatches(text: string, query: string, limit = 1000): { matches: ReadingTextMatch[]; limited: boolean } {
  if (!query) return { matches: [], limited: false }
  const expression = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu')
  const matches: ReadingTextMatch[] = []
  for (const match of text.matchAll(expression)) {
    if (matches.length >= limit) return { matches, limited: true }
    matches.push({ from: match.index, to: match.index + match[0].length })
  }
  return { matches, limited: false }
}

export interface MarkdownViewerCodeBlock {
  index: number
  language: string
  code: string
  collapsed: boolean
}

export interface MarkdownViewerReadingOptions {
  renderCodeToolbar?: (block: MarkdownViewerCodeBlock) => ReactNode
  renderImageToolbar?: (image: { index: number; alt: string }) => ReactNode
  collapseCodeBlocks?: boolean
  collapsedCodeBlocks?: readonly number[]
  expandedCodeBlocks?: readonly number[]
  search?: { query: string; current: number }
  onSearchResults?: (result: { count: number; limited: boolean }) => void
}

type CodeSlot = MarkdownViewerCodeBlock & { pos: number; size: number; host: HTMLElement }

/** Viewer-only decorations. Neither toolbar widgets, folds nor matches enter the document model. */
export function MarkdownViewerReadingTools({ editor, options }: { editor: Editor | null; options?: MarkdownViewerReadingOptions }) {
  const [slots, setSlots] = useState<CodeSlot[]>([])
  const [imageSlots, setImageSlots] = useState<{ index: number; alt: string }[]>([])
  const latest = useRef(options)
  const refresh = useRef<(() => void) | null>(null)
  const enabled = Boolean(options)
  useLayoutEffect(() => { latest.current = options }, [options])
  useLayoutEffect(() => {
    if (!editor || !enabled) return
    const key = new PluginKey('markdownViewerReadingTools')
    let blocks: CodeSlot[] = []
    let matches: ReadingTextMatch[] = []
    let images: { index: number; alt: string }[] = []
    let lastScroll = ''
    const hosts = new Map<number, HTMLElement>()
    const sync = () => {
      if (editor.isDestroyed) return
      const settings = latest.current
      matches = []
      let limited = false
      const codeBlocks: CodeSlot[] = []
      images = []
      let imageIndex = 0
      editor.state.doc.descendants((node, pos) => {
        if (node.type.name === 'image') {
          const index = imageIndex++
          if (!node.marks.some(mark => mark.type.name === 'link')) {
            images.push({ index, alt: String(node.attrs.alt || '') })
          }
        }
        if (settings?.search?.query && node.isTextblock && !limited) {
          const result = findReadingTextMatches(node.textBetween(0, node.content.size, '', '\ufffc'), settings.search.query, 1000 - matches.length)
          matches.push(...result.matches.map(match => ({ from: pos + 1 + match.from, to: pos + 1 + match.to })))
          limited = result.limited
        }
        if (node.type.name === 'codeBlock') {
          const index = codeBlocks.length
          let host = hosts.get(index)
          if (!host) {
            host = editor.view.dom.ownerDocument.createElement('div')
            host.dataset.markdownCodeTools = 'true'
            host.setAttribute('contenteditable', 'false')
            hosts.set(index, host)
          }
          const active = matches[settings?.search?.current ?? 0]
          const revealed = active && active.from > pos && active.to < pos + node.nodeSize
          const collapsed = !revealed && (settings?.collapsedCodeBlocks?.includes(index)
            || (settings?.collapseCodeBlocks && !settings.expandedCodeBlocks?.includes(index)))
          codeBlocks.push({ index, pos, size: node.nodeSize, host, code: node.textContent, language: String(node.attrs.language || 'text'), collapsed: Boolean(collapsed) })
        }
      })
      blocks = codeBlocks
      for (const index of hosts.keys()) if (index >= blocks.length) hosts.delete(index)
      setSlots(blocks)
      setImageSlots(images)
      editor.view.updateState(editor.state)
      editor.view.dom.dispatchEvent(new Event('markdown-reading-presentation'))
      settings?.onSearchResults?.({ count: matches.length, limited })
      const active = matches[settings?.search?.current ?? 0]
      const scrollKey = active ? `${settings?.search?.query}:${active.from}` : ''
      if (scrollKey && scrollKey !== lastScroll) {
        editor.view.dom.querySelector('[data-reading-match="current"]')?.scrollIntoView?.({ block: 'center', inline: 'nearest' })
      }
      lastScroll = scrollKey
    }
    editor.registerPlugin(new Plugin({ key, props: {
      decorations(state) {
        const decorations: Decoration[] = []
        const settings = latest.current
        for (const block of blocks) {
          if (block.pos + block.size > state.doc.content.size) continue
          if (settings?.renderCodeToolbar) decorations.push(Decoration.widget(block.pos, block.host, {
            key: `reading-code-${block.index}`, side: -1, ignoreSelection: true, stopEvent: () => true,
          }))
          if (block.collapsed) decorations.push(Decoration.node(block.pos, block.pos + block.size, { 'data-reading-folded': 'true' }))
        }
        matches.forEach((match, index) => {
          if (match.to <= state.doc.content.size) decorations.push(Decoration.inline(match.from, match.to, {
            class: 'markdown-reading-match', 'data-reading-match': index === (settings?.search?.current ?? 0) ? 'current' : 'other',
          }))
        })
        return DecorationSet.create(state.doc, decorations)
      },
    } }))
    refresh.current = sync
    editor.on('update', sync)
    sync()
    return () => { refresh.current = null; editor.off('update', sync); editor.unregisterPlugin(key) }
  }, [editor, enabled])
  useLayoutEffect(() => { refresh.current?.() }, [options])
  return <>{slots.map(slot => createPortal(options?.renderCodeToolbar?.(slot), slot.host, `code-${slot.index}`))}{imageSlots.map(slot => <Fragment key={`image-${slot.index}`}>{options?.renderImageToolbar?.(slot)}</Fragment>)}</>
}
