import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { useEffect, useState } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import {
  EditorContent,
  MarkdownToolbar,
  useMarkdownEditor,
} from '../src/index.js'

afterEach(() => cleanup())

function ToolbarHarness({ editable = true }: { editable?: boolean }) {
  const editor = useMarkdownEditor({
    initialMarkdown: 'text',
    editable,
  })

  return (
    <>
      <MarkdownToolbar editor={editor} />
      {editor && <EditorContent editor={editor} />}
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
    if (editor) editor.commands.setTextSelection({ from: 1, to: 5 })
  }, [editor])

  return (
    <>
      <MarkdownToolbar editor={editor} />
      {editor && <EditorContent editor={editor} />}
      <output data-testid="markdown">{markdown}</output>
    </>
  )
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
      'Blockquote',
      'Code block',
      'Horizontal rule',
      'Undo',
      'Redo',
    ]
    labels.forEach(label => expect(screen.getByRole('button', { name: label })).toBeInTheDocument())

    await user.tab()
    expect(screen.getByRole('button', { name: 'Paragraph' })).toHaveFocus()
    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('button', { name: 'Heading 1' })).toHaveFocus()
    await user.keyboard('{End}')
    expect(screen.getByRole('button', { name: 'Horizontal rule' })).toHaveFocus()
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
    expect(screen.getByRole('button', { name: 'Paragraph' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Undo' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Redo' })).toBeDisabled()
  })
})
