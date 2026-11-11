import type { Editor } from '@tiptap/core'
import { DOMSerializer, type Node as ProseMirrorNode } from '@tiptap/pm/model'
import { Plugin, PluginKey } from '@tiptap/pm/state'

/** Keep the editor in charge of heading DOM, including live reading widgets. */
export function installHeadingPresentation(editor: Editor, options: { levelOffset?: number; levels?: readonly number[]; ids?: readonly string[] }) {
  const key = new PluginKey('markdownHeadingPresentation')
  let indexedDoc: ProseMirrorNode | null = null
  const indices = new Map<number, number>()
  const indexAt = (doc: ProseMirrorNode, position: number | undefined) => {
    if (doc !== indexedDoc) {
      indices.clear(); let index = 0
      doc.descendants((node, pos) => { if (node.type.name === 'heading') indices.set(pos, index++) })
      indexedDoc = doc
    }
    return position === undefined ? 0 : indices.get(position) ?? 0
  }
  editor.registerPlugin(new Plugin({ key, props: { nodeViews: {
    heading(node, view, getPos) {
      const presentation = (current: ProseMirrorNode) => {
        const index = indexAt(view.state.doc, getPos())
        const level = Math.max(1, Math.min(6, Math.trunc(options.levels?.[index] ?? (Number(current.attrs.level) + (options.levelOffset ?? 0)))))
        return { tag: `h${level}`, id: options.ids?.[index] }
      }
      const initial = presentation(node)
      const template = DOMSerializer.renderSpec(view.dom.ownerDocument, node.type.spec.toDOM!(node))
      const original = template.dom as HTMLElement
      const dom = view.dom.ownerDocument.createElement(initial.tag)
      for (const attribute of Array.from(original.attributes)) dom.setAttribute(attribute.name, attribute.value)
      while (original.firstChild) dom.appendChild(original.firstChild)
      if (initial.id) dom.id = initial.id
      return { dom, contentDOM: !template.contentDOM || template.contentDOM === original ? dom : template.contentDOM,
        update(next) {
          if (next.type !== node.type) return false
          const value = presentation(next)
          if (dom.tagName.toLowerCase() !== value.tag) return false
          if (value.id) dom.id = value.id
          else if (original.hasAttribute('id')) dom.id = original.id
          else dom.removeAttribute('id')
          node = next
          return true
        },
      }
    },
  } } }))
  return () => { if (!editor.isDestroyed) editor.unregisterPlugin(key) }
}
