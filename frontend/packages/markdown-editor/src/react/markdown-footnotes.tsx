import { useId, useLayoutEffect } from 'react'
import type { Editor } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'

const labelKey = (value: string) => value.normalize('NFC').toLocaleLowerCase()

/** Runtime identifiers are local to this viewer; none enter the schema or Markdown. */
export function MarkdownViewerFootnotes({ editor }: { editor: Editor | null }) {
  const prefix = useId().replace(/[^a-z0-9_-]/gi, '')
  useLayoutEffect(() => {
    if (!editor) return
    const key = new PluginKey('markdownViewerFootnotes')
    let decorations = DecorationSet.empty
    let presentationDoc: ProseMirrorNode | null = null
    let updateReferences = () => {}
    const sync = () => {
      if (editor.isDestroyed) return
      presentationDoc = editor.state.doc
      const definitions = new Map<string, { pos: number; size: number; id: string; label: string }>()
      const references = new Map<string, { pos: number; id: string }[]>()
      editor.state.doc.descendants((node, pos) => {
        const label = String(node.attrs.label ?? ''), identity = labelKey(label)
        if (node.type.name === 'footnoteDefinition' && !definitions.has(identity)) definitions.set(identity, { pos, size: node.nodeSize, id: `fn-${prefix}-${pos}`, label })
        if (node.type.name === 'footnoteReference') {
          const rows = references.get(identity) ?? []
          rows.push({ pos, id: `fnref-${prefix}-${pos}` }); references.set(identity, rows)
        }
      })
      const numbered = [...new Set([...references.keys()].filter(identity => definitions.has(identity)).concat([...definitions.keys()]))]
      const result: Decoration[] = []
      updateReferences = () => { for (const [identity, rows] of references) {
        const definition = definitions.get(identity), number = numbered.indexOf(identity) + 1
        for (const row of rows) {
          const dom = editor.view.nodeDOM(row.pos)
          const link = dom instanceof HTMLElement ? dom.querySelector('a') : null
          if (!link) continue
          link.id = row.id
          if (definition) {
            link.href = `#${definition.id}`; link.dataset.footnoteNumber = String(number)
            link.setAttribute('role', 'doc-noteref'); link.setAttribute('aria-label', `Footnote ${number}`)
            link.textContent = String(number)
          } else {
            link.removeAttribute('href'); link.removeAttribute('role')
            link.textContent = `[^${link.dataset.footnoteLink ?? identity}]`; link.setAttribute('aria-label', `Undefined footnote: ${identity}`)
          }
        }
      } }
      for (const [identity, definition] of definitions) {
        const number = numbered.indexOf(identity) + 1
        result.push(Decoration.node(definition.pos, definition.pos + definition.size, { id: definition.id, 'data-footnote-number': String(number), tabindex: '-1', role: 'doc-endnote', 'aria-label': `Footnote ${number}` }))
        const links = document.createElement('span'); links.dataset.footnoteBacklinks = 'true'
        for (const [index, row] of (references.get(identity) ?? []).entries()) {
          const link = document.createElement('a'); link.href = `#${row.id}`; link.textContent = index ? `↩ ${index + 1}` : '↩'
          link.setAttribute('role', 'doc-backlink'); link.setAttribute('aria-label', `Back to footnote ${number} reference ${index + 1}`); links.append(link)
        }
        result.push(Decoration.widget(definition.pos + definition.size - 1, links, { side: 1, key: `footnote-back-${identity}`, ignoreSelection: true }))
      }
      decorations = DecorationSet.create(editor.state.doc, result)
      editor.view.updateState(editor.state)
      updateReferences()
    }
    editor.registerPlugin(new Plugin({ key, props: { decorations: state => state.doc === presentationDoc ? decorations : DecorationSet.empty } }))
    const onPresentation = () => updateReferences()
    editor.view.dom.addEventListener('markdown-reading-presentation', onPresentation)
    const onTransaction = ({ transaction }: { transaction: { docChanged: boolean } }) => { if (transaction.docChanged) sync() }
    editor.on('transaction', onTransaction); sync()
    return () => { editor.view.dom.removeEventListener('markdown-reading-presentation', onPresentation); editor.off('transaction', onTransaction); editor.unregisterPlugin(key) }
  }, [editor, prefix])
  return null
}
