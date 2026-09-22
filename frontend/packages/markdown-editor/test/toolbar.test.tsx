import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { EditorContent } from '@tiptap/react'
import { useEffect, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  MarkdownToolbar,
  useMarkdownEditor,
  type MarkdownSearchAdapter,
  type MarkdownSearchResult,
} from '../src/index.js'
import { getMarkdownEditor } from '../src/react/editor-handle.js'

afterEach(() => cleanup())

function ToolbarHarness({ editable = true }: { editable?: boolean }) {
  const editor = useMarkdownEditor({
    initialMarkdown: 'text',
    editable,
  })

  return (
    <>
      <MarkdownToolbar editor={editor} />
      {editor && <EditorContent editor={getMarkdownEditor(editor)} />}
    </>
  )
}

function SelectionHarness() {
  const [markdown, setMarkdown] = useState('text')
  const editor = useMarkdownEditor({
    initialMarkdown: 'text',
    onChange: setMarkdown,
  })

  useEffect(() => {
    getMarkdownEditor(editor)?.commands.setTextSelection({ from: 1, to: 5 })
  }, [editor])

  return (
    <>
      <MarkdownToolbar editor={editor} />
      {editor && <EditorContent editor={getMarkdownEditor(editor)} />}
      <output data-testid="markdown">{markdown}</output>
    </>
  )
}

interface SearchHarnessProps {
  searchAdapter: MarkdownSearchAdapter
  vault?: string
  selectText?: boolean
}

function SearchHarness({ searchAdapter, vault = 'team', selectText = false }: SearchHarnessProps) {
  const [markdown, setMarkdown] = useState('text')
  const editor = useMarkdownEditor({ initialMarkdown: 'text', onChange: setMarkdown })

  useEffect(() => {
    getMarkdownEditor(editor)?.commands.setTextSelection(selectText ? { from: 1, to: 5 } : { from: 1, to: 1 })
  }, [editor, selectText])

  return (
    <>
      <MarkdownToolbar
        editor={editor}
        link={{
          searchAdapter,
          searchContext: { vault },
          searchLabels: { inputLabel: 'Search Vault resources' },
        }}
      />
      {editor && <EditorContent editor={getMarkdownEditor(editor)} />}
      <output data-testid="markdown">{markdown}</output>
    </>
  )
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const GUIDE: MarkdownSearchResult = {
  id: 'akb://team/coll/notes/doc/guide.md',
  title: 'Guide',
  target: 'akb://team/coll/notes/doc/guide.md',
  kind: 'document',
}

const DIAGRAM: MarkdownSearchResult = {
  id: 'akb://team/coll/notes/file/00000000-0000-4000-8000-000000000001',
  title: 'Diagram',
  target: 'akb://team/coll/notes/file/00000000-0000-4000-8000-000000000001',
  kind: 'file',
}

describe('MarkdownToolbar', () => {
  it('exposes the shared default formatting controls and roving keyboard focus', async () => {
    const user = userEvent.setup()
    render(<ToolbarHarness />)

    await waitFor(() =>
      expect(screen.getByRole('toolbar', { name: 'Text formatting' })).toBeInTheDocument(),
    )

    const labels = [
      'Paragraph',
      'Heading 1',
      'Heading 2',
      'Heading 3',
      'Bold',
      'Italic',
      'Strikethrough',
      'Inline code',
      'Bulleted list',
      'Numbered list',
      'Task list',
      'Blockquote',
      'Code block',
      'Horizontal rule',
      'Insert link',
      'Undo',
      'Redo',
    ]
    labels.forEach(label => expect(screen.getByRole('button', { name: label })).toBeInTheDocument())

    await user.tab()
    expect(screen.getByRole('button', { name: 'Paragraph' })).toHaveFocus()
    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('button', { name: 'Heading 1' })).toHaveFocus()
    await user.keyboard('{End}')
    expect(screen.getByRole('button', { name: 'Insert link' })).toHaveFocus()
  })

  it('inserts a link against the selection after the popup takes focus', async () => {
    const user = userEvent.setup()
    render(<SelectionHarness />)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Insert link' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    await user.type(screen.getByLabelText('URL'), 'https://example.com')
    await user.click(screen.getByRole('button', { name: 'Insert link' }))

    await waitFor(() => expect(screen.getByTestId('markdown')).toHaveTextContent('[text](https://example.com)'))
    await waitFor(() => expect(screen.getByRole('textbox')).toHaveFocus())
  })

  it('cancels without changing Markdown and restores the editor focus', async () => {
    const user = userEvent.setup()
    render(<SelectionHarness />)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Insert link' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    await user.type(screen.getByLabelText('URL'), 'https://example.com')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.getByTestId('markdown')).toHaveTextContent('text')
    await waitFor(() => expect(screen.getByRole('textbox')).toHaveFocus())
  })

  it('edits and removes a link through the shared command contract', async () => {
    const user = userEvent.setup()
    function ExistingLinkHarness() {
      const [markdown, setMarkdown] = useState('[text](https://old.example)')
      const editor = useMarkdownEditor({ initialMarkdown: markdown, onChange: setMarkdown })

      useEffect(() => {
        getMarkdownEditor(editor)?.commands.setTextSelection({ from: 1, to: 5 })
      }, [editor])

      return (
        <>
          <MarkdownToolbar editor={editor} />
          {editor && <EditorContent editor={getMarkdownEditor(editor)} />}
          <output data-testid="markdown">{markdown}</output>
        </>
      )
    }

    render(<ExistingLinkHarness />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit link' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Edit link' }))
    const url = screen.getByLabelText('URL')
    await user.clear(url)
    await user.type(url, 'https://new.example')
    await user.click(screen.getByRole('button', { name: 'Save link' }))
    await waitFor(() => expect(screen.getByTestId('markdown')).toHaveTextContent('[text](https://new.example)'))

    await user.click(screen.getByRole('button', { name: 'Edit link' }))
    await user.click(screen.getByRole('button', { name: 'Remove link' }))
    await waitFor(() => expect(screen.getByTestId('markdown')).toHaveTextContent('text'))
  })

  it('applies formatting to the existing selection and exposes active/history state', async () => {
    const user = userEvent.setup()
    render(<SelectionHarness />)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Bold' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Bold' }))

    await waitFor(() => {
      expect(screen.getByTestId('markdown')).toHaveTextContent('**text**')
      expect(screen.getByRole('button', { name: 'Bold' })).toHaveAttribute('aria-pressed', 'true')
      expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled()
    })

    await user.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() => {
      expect(screen.getByTestId('markdown')).toHaveTextContent('text')
      expect(screen.getByRole('button', { name: 'Redo' })).toBeEnabled()
    })
  })

  it('disables every formatting command for a read-only editor', async () => {
    render(<ToolbarHarness editable={false} />)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Bold' })).toBeDisabled())
    expect(screen.getByRole('button', { name: 'Insert link' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Paragraph' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Undo' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Redo' })).toBeDisabled()
  })

  it('keeps invalid URLs in the popup and returns focus to the URL field', async () => {
    const user = userEvent.setup()
    render(<SelectionHarness />)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Insert link' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    await user.type(screen.getByLabelText('URL'), 'javascript:alert(1)')
    await user.click(screen.getByRole('button', { name: 'Insert link' }))

    expect(screen.getByRole('alert')).toHaveTextContent(/http\(s\), email, phone/i)
    await waitFor(() => expect(screen.getByLabelText('URL')).toHaveFocus())
    expect(screen.getByTestId('markdown')).toHaveTextContent('text')
  })

  it.each([GUIDE, DIAGRAM])('searches, selects, and inserts canonical $kind targets', async (result) => {
    const user = userEvent.setup()
    const searchAdapter: MarkdownSearchAdapter = {
      search: vi.fn().mockResolvedValue([result]),
    }
    render(<SearchHarness searchAdapter={searchAdapter} />)

    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    const search = screen.getByRole('combobox', { name: 'Search Vault resources' })
    await user.type(search, 'guide')
    await user.click(await screen.findByRole('option', { name: `${result.title} (${result.kind})` }))

    expect(screen.getByLabelText('URL')).toHaveValue(result.target)
    expect(screen.getByLabelText('Text')).toHaveValue(result.title)
    expect(screen.getByTestId('markdown')).toHaveTextContent('text')

    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    await waitFor(() =>
      expect(screen.getByTestId('markdown')).toHaveTextContent(`[${result.title}](${result.target})`),
    )
    expect(screen.getByTestId('markdown')).not.toHaveTextContent('signed.example')
    await user.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(screen.getByTestId('markdown')).toHaveTextContent('text'))
    await user.click(screen.getByRole('button', { name: 'Redo' }))
    await waitFor(() =>
      expect(screen.getByTestId('markdown')).toHaveTextContent(`[${result.title}](${result.target})`),
    )
    expect(searchAdapter.search).toHaveBeenCalledWith(
      'guide',
      expect.objectContaining({ vault: 'team', signal: expect.any(AbortSignal) }),
    )
  })

  it('uses the selected editor text as the link label when applying a search result', async () => {
    const user = userEvent.setup()
    const searchAdapter: MarkdownSearchAdapter = {
      search: vi.fn().mockResolvedValue([GUIDE]),
    }
    render(<SearchHarness searchAdapter={searchAdapter} selectText />)

    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    const search = screen.getByRole('combobox', { name: 'Search Vault resources' })
    await user.type(search, 'guide')
    await user.click(await screen.findByRole('option', { name: 'Guide (document)' }))
    expect(screen.getByLabelText('Text')).toHaveValue('text')

    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    await waitFor(() =>
      expect(screen.getByTestId('markdown')).toHaveTextContent(`[text](${GUIDE.target})`),
    )
  })

  it('distinguishes loading, empty results, and search errors without changing Markdown', async () => {
    const pending = deferred<readonly MarkdownSearchResult[]>()
    const searchAdapter: MarkdownSearchAdapter = {
      search: vi.fn().mockReturnValueOnce(pending.promise).mockRejectedValueOnce(new Error('offline')),
    }
    const user = userEvent.setup()
    render(<SearchHarness searchAdapter={searchAdapter} />)

    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    const search = screen.getByRole('combobox', { name: 'Search Vault resources' })
    fireEvent.change(search, { target: { value: 'none' } })
    expect(await screen.findByRole('status')).toHaveTextContent('Searching…')

    pending.resolve([])
    expect(await screen.findByText('No documents or files found.')).toBeInTheDocument()
    fireEvent.change(search, { target: { value: 'offline' } })
    expect(await screen.findByRole('alert')).toHaveTextContent('Search failed. Try again.')
    expect(screen.getByTestId('markdown')).toHaveTextContent('text')
  })

  it('aborts on query changes and ignores responses that finish out of order', async () => {
    const oldRequest = deferred<readonly MarkdownSearchResult[]>()
    const currentRequest = deferred<readonly MarkdownSearchResult[]>()
    const searchAdapter: MarkdownSearchAdapter = {
      search: vi.fn((query) => query === 'old' ? oldRequest.promise : currentRequest.promise),
    }
    const user = userEvent.setup()
    render(<SearchHarness searchAdapter={searchAdapter} />)

    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    const search = screen.getByRole('combobox', { name: 'Search Vault resources' })
    fireEvent.change(search, { target: { value: 'old' } })
    await waitFor(() => expect(searchAdapter.search).toHaveBeenCalledWith(
      'old', expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ))
    const oldSignal = vi.mocked(searchAdapter.search).mock.calls[0]?.[1]?.signal

    fireEvent.change(search, { target: { value: 'new' } })
    await waitFor(() => expect(searchAdapter.search).toHaveBeenCalledWith(
      'new', expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ))
    expect(oldSignal?.aborted).toBe(true)

    currentRequest.resolve([{ ...GUIDE, id: 'new', title: 'Newest', target: GUIDE.target }])
    expect(await screen.findByRole('option', { name: 'Newest (document)' })).toBeInTheDocument()
    oldRequest.resolve([{ ...GUIDE, id: 'old', title: 'Stale', target: GUIDE.target }])
    await waitFor(() => expect(screen.queryByRole('option', { name: 'Stale (document)' })).not.toBeInTheDocument())
    expect(screen.getByTestId('markdown')).toHaveTextContent('text')
  })

  it('aborts a pending search on popup close and ignores a late success', async () => {
    const pending = deferred<readonly MarkdownSearchResult[]>()
    const searchAdapter: MarkdownSearchAdapter = {
      search: vi.fn().mockReturnValue(pending.promise),
    }
    const user = userEvent.setup()
    render(<SearchHarness searchAdapter={searchAdapter} />)

    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    const search = screen.getByRole('combobox', { name: 'Search Vault resources' })
    fireEvent.change(search, { target: { value: 'pending' } })
    expect(await screen.findByRole('status')).toHaveTextContent('Searching…')
    const signal = vi.mocked(searchAdapter.search).mock.calls[0]?.[1]?.signal

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(signal?.aborted).toBe(true)
    await act(async () => pending.resolve([GUIDE]))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Guide (document)' })).not.toBeInTheDocument()
    expect(screen.getByTestId('markdown')).toHaveTextContent('text')
  })

  it('uses the latest vault context and ignores a stale failure after context changes', async () => {
    const firstRequest = deferred<readonly MarkdownSearchResult[]>()
    const latestRequest = deferred<readonly MarkdownSearchResult[]>()
    const searchAdapter: MarkdownSearchAdapter = {
      search: vi.fn((_query, context) => context?.vault === 'first' ? firstRequest.promise : latestRequest.promise),
    }
    const user = userEvent.setup()
    const view = render(<SearchHarness searchAdapter={searchAdapter} vault="first" />)

    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    const search = screen.getByRole('combobox', { name: 'Search Vault resources' })
    fireEvent.change(search, { target: { value: 'guide' } })
    await waitFor(() => expect(searchAdapter.search).toHaveBeenCalledWith(
      'guide', expect.objectContaining({ vault: 'first' }),
    ))

    view.rerender(<SearchHarness searchAdapter={searchAdapter} vault="second" />)
    await waitFor(() => expect(searchAdapter.search).toHaveBeenCalledWith(
      'guide', expect.objectContaining({ vault: 'second' }),
    ))

    latestRequest.resolve([{ ...GUIDE, id: 'current', title: 'Current vault result' }])
    expect(await screen.findByRole('option', { name: 'Current vault result (document)' })).toBeInTheDocument()
    firstRequest.reject(new Error('stale permission failure'))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(screen.getByTestId('markdown')).toHaveTextContent('text')
  })

  it('supports arrow-key candidate selection and keeps cancellation non-mutating', async () => {
    const searchAdapter: MarkdownSearchAdapter = {
      search: vi.fn().mockResolvedValue([GUIDE, DIAGRAM]),
    }
    const user = userEvent.setup()
    render(<SearchHarness searchAdapter={searchAdapter} />)

    await user.click(screen.getByRole('button', { name: 'Insert link' }))
    const search = screen.getByRole('combobox', { name: 'Search Vault resources' })
    await user.type(search, 'reference')
    await screen.findByRole('option', { name: 'Guide (document)' })
    await user.keyboard('{ArrowDown}{Enter}')
    expect(screen.getByLabelText('URL')).toHaveValue(GUIDE.target)

    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByTestId('markdown')).toHaveTextContent('text')
    await waitFor(() => expect(screen.getByRole('textbox')).toHaveFocus())
  })
})
