import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { useEffect, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Editor } from '@tiptap/core'
import { EditorContent } from '@tiptap/react'

import {
  MarkdownEditingSurface,
  MarkdownToolbar,
  useMarkdownCommands,
  useMarkdownEditor,
} from '../src/index.js'
import { serializeEditorMarkdown } from '../src/core.js'
import { getMarkdownEditor } from '../src/react/editor-handle.js'

const TABLE = [
  '| H1 | H2 | H3 |',
  '| --- | --- | --- |',
  '| R1A | R1B | R1C |',
  '| R2A | R2B | R2C |',
  '| R3A | R3B | R3C |',
].join('\n')

afterEach(() => cleanup())

interface TableHarnessProps {
  initialMarkdown: string
  readOnly?: boolean
  onPersist?: (markdown: string) => void
  onEditor?: (editor: Editor | null) => void
}

function TableHarness({ initialMarkdown, readOnly = false, onPersist, onEditor }: TableHarnessProps) {
  const [markdown, setMarkdown] = useState(initialMarkdown)
  const [lastCommand, setLastCommand] = useState('')
  const editor = useMarkdownEditor({
    initialMarkdown,
    editable: !readOnly,
    onChange: setMarkdown,
  })
  const commands = useMarkdownCommands(editor)

  useEffect(() => {
    onEditor?.(getMarkdownEditor(editor))
    return () => onEditor?.(null)
  }, [editor, onEditor])

  return (
    <>
      <output data-testid="markdown">{markdown}</output>
      <output data-testid="last-command">{lastCommand}</output>
      <MarkdownEditingSurface
        editor={editor}
        markdown={markdown}
        readOnly={readOnly}
        toolbar={<MarkdownToolbar editor={editor} />}
      >
        {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
      </MarkdownEditingSurface>
      <div>
        <button
          type="button"
          onClick={() => setLastCommand(String(commands.addTableRowAfter()))}
        >
          Try add row
        </button>
        <button
          type="button"
          onClick={() => setLastCommand(String(commands.deleteTable()))}
        >
          Try delete table
        </button>
        <button
          type="button"
          onClick={() => setLastCommand(String(commands.continueBelowTable()))}
        >
          Try continue below
        </button>
        <button
          type="button"
          onClick={() => setLastCommand(String(commands.insertTable()))}
        >
          Try insert table
        </button>
        <button
          type="button"
          onClick={() => {
            const rawEditor = getMarkdownEditor(editor)
            if (rawEditor) onPersist?.(serializeEditorMarkdown(rawEditor))
          }}
        >
          Save Markdown
        </button>
      </div>
    </>
  )
}

function tableRows(table: HTMLTableElement): string[][] {
  return Array.from(table.rows, row =>
    Array.from(row.cells, cell => cell.textContent?.trim() ?? ''),
  )
}

function textPosition(editor: Editor, text: string): number {
  let position = -1
  editor.state.doc.descendants((node, nodePosition) => {
    if (node.isText && node.text?.includes(text)) {
      position = nodePosition + node.text.indexOf(text)
      return false
    }
    return true
  })
  if (position < 0) throw new Error(`Could not find text in the Markdown editor: ${text}`)
  return position
}

function selectText(editor: Editor, text: string): void {
  editor.commands.setTextSelection(textPosition(editor, text))
}

function tableActions(table: HTMLTableElement) {
  return within(table).getByRole('toolbar', { name: 'Table actions' })
}

describe('shared GFM table editing', () => {
  it('inserts a table through the public toolbar and serializes it as GFM', async () => {
    const user = userEvent.setup()
    render(<TableHarness initialMarkdown="before\n\nafter" />)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Insert table' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Insert table' }))

    await waitFor(() => expect(screen.getByRole('table', { name: 'Editable table' })).toBeVisible())
    expect(screen.getByTestId('markdown').textContent).toContain('| --- | --- | --- |')
  })

  it('adds a row after and a column to the right of the selected non-first cell', async () => {
    const user = userEvent.setup()
    let editor: Editor | null = null
    render(<TableHarness initialMarkdown={TABLE} onEditor={current => { editor = current }} />)
    const table = await screen.findByRole('table', { name: 'Editable table' })

    act(() => selectText(editor!, 'R2B'))
    const addRow = within(tableActions(table as HTMLTableElement)).getByRole('button', { name: 'Add row after selected row' })
    await waitFor(() => expect(addRow).toBeEnabled())
    await user.click(addRow)

    expect(tableRows(screen.getByRole('table', { name: 'Editable table' }) as HTMLTableElement)).toEqual([
      ['H1', 'H2', 'H3'],
      ['R1A', 'R1B', 'R1C'],
      ['R2A', 'R2B', 'R2C'],
      ['', '', ''],
      ['R3A', 'R3B', 'R3C'],
    ])

    act(() => selectText(editor!, 'R2B'))
    const addColumn = within(tableActions(screen.getByRole('table', { name: 'Editable table' }) as HTMLTableElement)).getByRole('button', { name: 'Add column right of selected column' })
    await waitFor(() => expect(addColumn).toBeEnabled())
    await user.click(addColumn)

    expect(tableRows(screen.getByRole('table', { name: 'Editable table' }) as HTMLTableElement)).toEqual([
      ['H1', 'H2', '', 'H3'],
      ['R1A', 'R1B', '', 'R1C'],
      ['R2A', 'R2B', '', 'R2C'],
      ['', '', '', ''],
      ['R3A', 'R3B', '', 'R3C'],
    ])
  })

  it('exposes selection-aware row edits through useMarkdownCommands', async () => {
    const user = userEvent.setup()
    let editor: Editor | null = null
    render(<TableHarness initialMarkdown={TABLE} onEditor={current => { editor = current }} />)
    await screen.findByRole('table', { name: 'Editable table' })

    act(() => selectText(editor!, 'R2B'))
    await user.click(screen.getByRole('button', { name: 'Try add row' }))

    expect(screen.getByTestId('last-command')).toHaveTextContent('true')
    expect(tableRows(screen.getByRole('table', { name: 'Editable table' }) as HTMLTableElement)).toEqual([
      ['H1', 'H2', 'H3'],
      ['R1A', 'R1B', 'R1C'],
      ['R2A', 'R2B', 'R2C'],
      ['', '', ''],
      ['R3A', 'R3B', 'R3C'],
    ])
  })

  it('deletes the selected row and column without disturbing other cell contents', async () => {
    const user = userEvent.setup()
    let editor: Editor | null = null
    render(<TableHarness initialMarkdown={TABLE} onEditor={current => { editor = current }} />)
    const table = await screen.findByRole('table', { name: 'Editable table' })

    act(() => selectText(editor!, 'R2B'))
    const removeRow = within(tableActions(table as HTMLTableElement)).getByRole('button', { name: 'Remove selected row' })
    await waitFor(() => expect(removeRow).toBeEnabled())
    await user.click(removeRow)
    expect(tableRows(screen.getByRole('table', { name: 'Editable table' }) as HTMLTableElement)).toEqual([
      ['H1', 'H2', 'H3'],
      ['R1A', 'R1B', 'R1C'],
      ['R3A', 'R3B', 'R3C'],
    ])

    act(() => selectText(editor!, 'R1B'))
    const removeColumn = within(tableActions(screen.getByRole('table', { name: 'Editable table' }) as HTMLTableElement)).getByRole('button', { name: 'Remove selected column' })
    await waitFor(() => expect(removeColumn).toBeEnabled())
    await user.click(removeColumn)
    expect(tableRows(screen.getByRole('table', { name: 'Editable table' }) as HTMLTableElement)).toEqual([
      ['H1', 'H3'],
      ['R1A', 'R1C'],
      ['R3A', 'R3C'],
    ])
  })

  it('continues in the paragraph immediately below the selected table', async () => {
    const user = userEvent.setup()
    const markdown = `before\n\n${TABLE}\n\nbetween tables\n\n| Other | Table |\n| --- | --- |\n| keep | this |\n\nafter`
    let editor: Editor | null = null
    render(<TableHarness initialMarkdown={markdown} onEditor={current => { editor = current }} />)
    const [firstTable] = await screen.findAllByRole('table', { name: 'Editable table' })

    await user.click(editor!.view.dom)
    act(() => selectText(editor!, 'R2B'))
    const continueBelow = within(tableActions(firstTable as HTMLTableElement)).getByRole('button', { name: 'Continue below' })
    await waitFor(() => expect(continueBelow).toBeEnabled())
    await user.click(continueBelow)
    await user.keyboard(' appended')

    expect(screen.getByText('between tables appended')).toBeInTheDocument()
    expect(screen.getAllByRole('table', { name: 'Editable table' })).toHaveLength(2)
    expect(screen.getByTestId('markdown').textContent).toContain('| Other | Table |')
    expect(screen.getByTestId('markdown').textContent).toContain('\nafter')
  })

  it('creates a paragraph between adjacent tables and preserves their order', async () => {
    const user = userEvent.setup()
    const markdown = `${TABLE}\n\n| Other | Table |\n| --- | --- |\n| keep | this |`
    let editor: Editor | null = null
    render(<TableHarness initialMarkdown={markdown} onEditor={current => { editor = current }} />)
    const [firstTable] = await screen.findAllByRole('table', { name: 'Editable table' })

    await user.click(editor!.view.dom)
    act(() => selectText(editor!, 'R2B'))
    const continueButton = within(tableActions(firstTable as HTMLTableElement)).getByRole('button', { name: 'Continue below' })
    await waitFor(() => expect(continueButton).toBeEnabled())
    continueButton.focus()
    await user.keyboard('{Enter}')
    await user.keyboard('New paragraph')

    const tables = screen.getAllByRole('table', { name: 'Editable table' })
    expect(tables).toHaveLength(2)
    expect(screen.getByText('New paragraph')).toBeInTheDocument()
    expect(tables[0]!.compareDocumentPosition(screen.getByText('New paragraph')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByTestId('markdown').textContent).toContain('| Other | Table |')
  })

  it('deletes and undoes a table, then saves and reopens the same GFM meaning', async () => {
    const user = userEvent.setup()
    let savedMarkdown = ''
    let editor: Editor | null = null
    const original = `before\n\n${TABLE}\n\nafter`
    const view = render(
      <TableHarness initialMarkdown={original} onPersist={markdown => { savedMarkdown = markdown }} onEditor={current => { editor = current }} />,
    )
    const table = await screen.findByRole('table', { name: 'Editable table' })

    act(() => selectText(editor!, 'R2B'))
    const deleteTable = within(tableActions(table as HTMLTableElement)).getByRole('button', { name: 'Delete table' })
    await waitFor(() => expect(deleteTable).toBeEnabled())
    await user.click(deleteTable)
    await waitFor(() => expect(screen.queryByRole('table', { name: 'Editable table' })).not.toBeInTheDocument())
    expect(screen.getByText('after')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(screen.getByRole('table', { name: 'Editable table' })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Save Markdown' }))
    expect(savedMarkdown).toContain('| H1')
    expect(savedMarkdown).toContain('| R2A | R2B | R2C |')

    view.unmount()
    render(<TableHarness initialMarkdown={savedMarkdown} />)
    expect(tableRows(await screen.findByRole('table', { name: 'Editable table' }) as HTMLTableElement)).toEqual([
      ['H1', 'H2', 'H3'],
      ['R1A', 'R1B', 'R1C'],
      ['R2A', 'R2B', 'R2C'],
      ['R3A', 'R3B', 'R3C'],
    ])
  })

  it('uses the selected table after keyboard focus moves and leaves Escape non-mutating', async () => {
    const user = userEvent.setup()
    let editor: Editor | null = null
    render(<TableHarness initialMarkdown={`${TABLE}\n\n| Other | Table |\n| --- | --- |\n| keep | this |`} onEditor={current => { editor = current }} />)
    const [firstTable, secondTable] = await screen.findAllByRole('table', { name: 'Editable table' })
    await user.click(editor!.view.dom)
    act(() => selectText(editor!, 'R2B'))
    const markdownBefore = serializeEditorMarkdown(editor!)
    const secondMenu = tableActions(secondTable as HTMLTableElement)
    expect(within(secondMenu).getByRole('button', { name: 'Add row after selected row' })).toBeDisabled()
    const rowButton = within(tableActions(firstTable as HTMLTableElement)).getByRole('button', { name: 'Add row after selected row' })
    rowButton.focus()
    await user.keyboard('{Escape}')
    expect(serializeEditorMarkdown(editor!)).toBe(markdownBefore)
    await user.keyboard('{Enter}')
    expect(tableRows(firstTable as HTMLTableElement)).toHaveLength(5)
    expect(tableRows(secondTable as HTMLTableElement)).toEqual([['Other', 'Table'], ['keep', 'this']])
  })

  it('returns false without an applicable selection or in read-only mode', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const view = render(<TableHarness initialMarkdown="plain text" />)

    await user.click(screen.getByRole('button', { name: 'Try add row' }))
    expect(screen.getByTestId('last-command')).toHaveTextContent('false')
    await user.click(screen.getByRole('button', { name: 'Try continue below' }))
    expect(screen.getByTestId('last-command')).toHaveTextContent('false')
    expect(screen.getByTestId('markdown')).toHaveTextContent('plain text')

    view.unmount()
    render(<TableHarness initialMarkdown={TABLE} readOnly onPersist={onChange} />)
    await user.click(screen.getByRole('button', { name: 'Try delete table' }))
    await user.click(screen.getByRole('button', { name: 'Try insert table' }))

    expect(screen.getByTestId('last-command')).toHaveTextContent('false')
    expect(screen.getByRole('table', { name: 'Table' })).toBeVisible()
    expect(screen.queryByRole('toolbar', { name: 'Table actions' })).not.toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
  })
})
