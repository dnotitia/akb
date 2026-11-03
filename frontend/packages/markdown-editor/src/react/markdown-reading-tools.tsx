import { Fragment, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import type { Editor } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'

export function revealMarkdownTarget(target: HTMLElement): void {
  target.dispatchEvent(new CustomEvent('markdown-reading-reveal', { bubbles: true }))
}

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
  searchMatch?: boolean
}

export interface MarkdownViewerSection { index: number; title: string; level: number; collapsed: boolean }

export interface MarkdownViewerReadingOptions {
  renderCodeToolbar?: (block: MarkdownViewerCodeBlock) => ReactNode
  renderImageToolbar?: (image: { index: number; alt: string }) => ReactNode
  renderSectionToggle?: (section: MarkdownViewerSection) => ReactNode
  collapsedSections?: readonly number[]
  onRevealSections?: (indices: readonly number[]) => void
  collapseCodeBlocks?: boolean
  collapsedCodeBlocks?: readonly number[]
  expandedCodeBlocks?: readonly number[]
  search?: { query: string; current: number }
  onSearchResults?: (result: { count: number; limited: boolean }) => void
}

type CodeSlot = MarkdownViewerCodeBlock & { pos: number; size: number; host: HTMLElement }
type SectionSlot = MarkdownViewerSection & { pos: number; end: number; headingSize: number }

/** Viewer-only decorations. Neither toolbar widgets, folds nor matches enter the document model. */
export function MarkdownViewerReadingTools({ editor, options }: { editor: Editor | null; options?: MarkdownViewerReadingOptions }) {
  const [slots, setSlots] = useState<CodeSlot[]>([])
  const [imageSlots, setImageSlots] = useState<{ index: number; alt: string }[]>([])
  const [sectionSlots, setSectionSlots] = useState<SectionSlot[]>([])
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
    let sections: SectionSlot[] = []
    let hidden: { from: number; to: number }[] = []
    let roots: { node: ProseMirrorNode; pos: number }[] = []
    let presentationDoc: ProseMirrorNode | null = null
    const opened = new Set<number>()
    let previousCollapsed = latest.current?.collapsedSections
    let lastScroll = ''
    const hosts = new Map<number, HTMLElement>()
    const sync = () => {
      if (editor.isDestroyed) return
      const settings = latest.current
      presentationDoc = editor.state.doc
      if (previousCollapsed !== settings?.collapsedSections) { opened.clear(); previousCollapsed = settings?.collapsedSections }
      roots = []; editor.state.doc.forEach((node, pos) => roots.push({ node, pos }))
      sections = []
      const stack: SectionSlot[] = []
      for (const [index, { node, pos }] of roots.entries()) {
        if (node.type.name !== 'heading') continue
        const level = Number(node.attrs.level)
        while (stack.length && stack[stack.length - 1]!.level >= level) stack.pop()!.end = pos
        const section = { index, title: node.textContent, level, collapsed: Boolean(settings?.collapsedSections?.includes(index)) && !opened.has(index), pos, end: editor.state.doc.content.size, headingSize: node.nodeSize }
        sections.push(section); stack.push(section)
      }
      sections = sections.filter(section => section.end > section.pos + section.headingSize)
      matches = []
      let limited = false
      const codeBlocks: CodeSlot[] = []
      images = []
      let imageIndex = 0
      const imagePositions = new Map<number, number>()
      editor.state.doc.descendants((node, pos) => {
        if (node.type.name === 'image') {
          const index = imageIndex++
          imagePositions.set(index, pos)
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
      const activeMatch = matches[settings?.search?.current ?? 0]
      const revealedSections = sections.filter(section => section.collapsed && activeMatch && activeMatch.from >= section.pos + section.headingSize && activeMatch.from < section.end)
      for (const section of revealedSections) { section.collapsed = false; opened.add(section.index) }
      if (revealedSections.length) settings?.onRevealSections?.(revealedSections.map(section => section.index))
      hidden = sections.filter(section => section.collapsed).map(section => ({ from: section.pos + section.headingSize, to: section.end }))
      const isHidden = (position: number) => hidden.some(range => position >= range.from && position < range.to)
      for (const block of codeBlocks) {
        block.searchMatch = Boolean(activeMatch && activeMatch.from > block.pos && activeMatch.to < block.pos + block.size)
        if (block.searchMatch) block.collapsed = false
      }
      blocks = codeBlocks
      for (const index of hosts.keys()) if (index >= blocks.length) hosts.delete(index)
      setSlots(blocks.filter(block => !isHidden(block.pos)))
      setImageSlots(images.filter(image => !isHidden(imagePositions.get(image.index) ?? -1)))
      setSectionSlots(sections.filter(section => !isHidden(section.pos)))
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
        if (state.doc !== presentationDoc) return DecorationSet.empty
        const decorations: Decoration[] = []
        const settings = latest.current
        const isHidden = (position: number) => hidden.some(range => position >= range.from && position < range.to)
        for (const [index, root] of roots.entries()) {
          const attributes: Record<string, string> = { 'data-reading-block-position': String(root.pos) }
          if (isHidden(root.pos)) { attributes.hidden = ''; attributes['data-reading-section-hidden'] = 'true' }
          if (root.node.type.name === 'heading') attributes['data-reading-section-index'] = String(index)
          decorations.push(Decoration.node(root.pos, root.pos + root.node.nodeSize, attributes))
        }
        for (const block of blocks) {
          if (isHidden(block.pos)) continue
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
    const reveal = (target: HTMLElement) => {
      let position: number
      const block = target.closest<HTMLElement>('[data-reading-block-position]')
      try { position = block ? Number(block.dataset.readingBlockPosition) : editor.view.posAtDOM(target, 0) } catch { return }
      const ancestors = sections.filter(section => section.collapsed && position >= section.pos + section.headingSize && position < section.end)
      if (!ancestors.length) return
      ancestors.forEach(section => opened.add(section.index))
      latest.current?.onRevealSections?.(ancestors.map(section => section.index)); sync()
    }
    const onReveal = (event: Event) => { if (event.target instanceof HTMLElement) reveal(event.target) }
    const findTarget = (hash: string) => {
      let id: string
      try { id = decodeURIComponent(hash.replace(/^#/, '')) } catch { return }
      const target = [...editor.view.dom.querySelectorAll<HTMLElement>('[id]')].find(element => element.id === id)
      if (target) reveal(target)
    }
    const onClick = (event: MouseEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || event.button) return
      const href = event.target instanceof Element ? event.target.closest('a')?.getAttribute('href') : null
      if (href?.startsWith('#')) findTarget(href)
    }
    const onHash = () => findTarget(window.location.hash)
    editor.view.dom.addEventListener('markdown-reading-reveal', onReveal)
    editor.view.dom.addEventListener('beforematch', onReveal)
    editor.view.dom.addEventListener('click', onClick, true)
    window.addEventListener('hashchange', onHash)
    const onTransaction = ({ transaction }: { transaction: { docChanged: boolean } }) => { if (transaction.docChanged) sync() }
    editor.on('transaction', onTransaction)
    sync()
    const frame = requestAnimationFrame(onHash)
    return () => { cancelAnimationFrame(frame); refresh.current = null; editor.off('transaction', onTransaction); editor.unregisterPlugin(key); editor.view.dom.removeEventListener('markdown-reading-reveal', onReveal); editor.view.dom.removeEventListener('beforematch', onReveal); editor.view.dom.removeEventListener('click', onClick, true); window.removeEventListener('hashchange', onHash) }
  }, [editor, enabled])
  useLayoutEffect(() => { refresh.current?.() }, [options])
  return <>{slots.map(slot => createPortal(options?.renderCodeToolbar?.(slot), slot.host, `code-${slot.index}`))}{sectionSlots.map(slot => <Fragment key={`section-${slot.index}`}>{options?.renderSectionToggle?.(slot)}</Fragment>)}{imageSlots.map(slot => <Fragment key={`image-${slot.index}`}>{options?.renderImageToolbar?.(slot)}</Fragment>)}</>
}
