import type { Editor } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { NodeSelection, TextSelection } from '@tiptap/pm/state'
import { CellSelection } from '@tiptap/pm/tables'

export interface MarkdownTableInsertionOptions {
  rows?: number
  cols?: number
  withHeaderRow?: boolean
}

export interface MarkdownTableCommands {
  insertTable(options?: MarkdownTableInsertionOptions): boolean
  addTableRowAfter(): boolean
  addTableColumnAfter(): boolean
  deleteTableRow(): boolean
  deleteTableColumn(): boolean
  deleteTable(): boolean
  continueBelowTable(): boolean
}

export interface MarkdownTableState {
  active: boolean
  canInsert: boolean
  canAddRowAfter: boolean
  canAddColumnAfter: boolean
  canDeleteRow: boolean
  canDeleteColumn: boolean
  canDelete: boolean
  canContinueBelow: boolean
}

export const DEFAULT_MARKDOWN_TABLE_INSERTION: Required<MarkdownTableInsertionOptions> = {
  rows: 3,
  cols: 3,
  withHeaderRow: true,
}

interface SelectedTable {
  node: ProseMirrorNode
  position: number
}

function selectedTable(editor: Editor): SelectedTable | null {
  const { selection } = editor.state

  if (selection instanceof NodeSelection && selection.node.type.name === 'table') {
    return { node: selection.node, position: selection.from }
  }

  const { $from } = selection
  for (let depth = $from.depth; depth > 0; depth -= 1) {
    const node = $from.node(depth)
    if (node.type.name === 'table') {
      return { node, position: $from.before(depth) }
    }
  }

  return null
}

function isSingleCellSelection(editor: Editor): boolean {
  return !(editor.state.selection instanceof CellSelection)
}

function canPlaceParagraph(editor: Editor, position: number): boolean {
  try {
    const $position = editor.state.doc.resolve(position)
    const index = $position.index()
    if ($position.parent.maybeChild(index)?.type.name === 'paragraph') return true
    const paragraph = editor.schema.nodes.paragraph
    return Boolean(paragraph && $position.parent.canReplaceWith(index, index, paragraph))
  } catch {
    return false
  }
}

function moveToParagraphAt(editor: Editor, position: number, atEnd: boolean): boolean {
  if (!editor.isEditable || editor.isDestroyed) return false

  const boundedPosition = Math.max(0, Math.min(position, editor.state.doc.content.size))
  let $position
  try {
    $position = editor.state.doc.resolve(boundedPosition)
  } catch {
    return false
  }

  const index = $position.index()
  const next = $position.parent.maybeChild(index)
  if (next?.type.name === 'paragraph') {
    const start = boundedPosition + 1
    const cursor = atEnd ? start + next.content.size : start
    editor.commands.setTextSelection(cursor)
    editor.view.focus()
    return true
  }

  const paragraphType = editor.schema.nodes.paragraph
  if (!paragraphType || !$position.parent.canReplaceWith(index, index, paragraphType)) return false
  const paragraph = paragraphType.createAndFill()
  if (!paragraph) return false

  const transaction = editor.state.tr.insert(boundedPosition, paragraph)
  transaction.setSelection(TextSelection.near(transaction.doc.resolve(boundedPosition + 1), 1))
  editor.view.dispatch(transaction)
  editor.view.focus()
  return true
}

function selectedCellActionsAllowed(editor: Editor, table: SelectedTable): boolean {
  return (
    editor.isEditable &&
    !editor.isDestroyed &&
    isSingleCellSelection(editor) &&
    table.node.childCount > 0
  )
}

function canDeleteRow(editor: Editor, table: SelectedTable | null): boolean {
  return Boolean(
    table &&
      selectedCellActionsAllowed(editor, table) &&
      table.node.childCount > 1 &&
      editor.can().deleteRow(),
  )
}

function canDeleteColumn(editor: Editor, table: SelectedTable | null): boolean {
  return Boolean(
    table &&
      selectedCellActionsAllowed(editor, table) &&
      (table.node.firstChild?.childCount ?? 0) > 1 &&
      editor.can().deleteColumn(),
  )
}

export function isSelectedMarkdownTable(editor: Editor, table: HTMLTableElement): boolean {
  if (editor.isDestroyed) return false
  const selected = selectedTable(editor)
  if (!selected) return false
  const dom = editor.view.nodeDOM(selected.position)
  return dom === table || Boolean(dom?.contains(table))
}

export function markdownTableState(editor: Editor): MarkdownTableState {
  if (editor.isDestroyed) {
    return {
      active: false,
      canInsert: false,
      canAddRowAfter: false,
      canAddColumnAfter: false,
      canDeleteRow: false,
      canDeleteColumn: false,
      canDelete: false,
      canContinueBelow: false,
    }
  }

  const table = selectedTable(editor)
  const active = Boolean(table)
  const cellActionsAllowed = Boolean(table && selectedCellActionsAllowed(editor, table))
  const afterTable = table ? table.position + table.node.nodeSize : -1

  return {
    active,
    canInsert: editor.isEditable && editor.can().insertTable(DEFAULT_MARKDOWN_TABLE_INSERTION),
    canAddRowAfter: Boolean(cellActionsAllowed && editor.can().addRowAfter()),
    canAddColumnAfter: Boolean(cellActionsAllowed && editor.can().addColumnAfter()),
    canDeleteRow: canDeleteRow(editor, table),
    canDeleteColumn: canDeleteColumn(editor, table),
    canDelete: Boolean(editor.isEditable && table && editor.can().deleteTable()),
    canContinueBelow: Boolean(
      editor.isEditable && table && canPlaceParagraph(editor, afterTable),
    ),
  }
}

export function markdownTableCommands(editor: Editor): MarkdownTableCommands {
  const canEdit = () => editor.isEditable && !editor.isDestroyed
  const selected = () => (canEdit() ? selectedTable(editor) : null)

  return {
    insertTable: (options = DEFAULT_MARKDOWN_TABLE_INSERTION) => {
      if (!canEdit() || !editor.can().insertTable(options)) return false
      return editor.chain().focus().insertTable(options).run()
    },
    addTableRowAfter: () => {
      const table = selected()
      if (!table || !selectedCellActionsAllowed(editor, table) || !editor.can().addRowAfter()) {
        return false
      }
      return editor.chain().focus().addRowAfter().run()
    },
    addTableColumnAfter: () => {
      const table = selected()
      if (!table || !selectedCellActionsAllowed(editor, table) || !editor.can().addColumnAfter()) {
        return false
      }
      return editor.chain().focus().addColumnAfter().run()
    },
    deleteTableRow: () => {
      const table = selected()
      if (!canDeleteRow(editor, table)) return false
      return editor.chain().focus().deleteRow().run()
    },
    deleteTableColumn: () => {
      const table = selected()
      if (!canDeleteColumn(editor, table)) return false
      return editor.chain().focus().deleteColumn().run()
    },
    deleteTable: () => {
      const table = selected()
      if (!canEdit() || !table || !editor.can().deleteTable()) return false
      if (!editor.chain().focus().deleteTable().run()) return false
      moveToParagraphAt(editor, table.position, false)
      return true
    },
    continueBelowTable: () => {
      const table = selected()
      if (!canEdit() || !table) return false
      return moveToParagraphAt(editor, table.position + table.node.nodeSize, true)
    },
  }
}
