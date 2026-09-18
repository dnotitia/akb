import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { useEffect, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import {
  createMarkdownEditor,
  EditorContent,
  MarkdownEditor,
  MarkdownEditingSurface,
  MarkdownViewer,
  useMarkdownCommands,
  useMarkdownEditor,
  useMarkdownState,
} from '../src/index.js'

function HookProbe() {
  const editor = useMarkdownEditor({ initialMarkdown: '초안' })
  const state = useMarkdownState(editor)
  const commands = useMarkdownCommands(editor)

  return (
    <>
      <output data-testid="hook-state">{state?.markdown ?? 'loading'}</output>
      <button type="button" onClick={() => commands.setMarkdown('명령')}>set</button>
    </>
  )
}

describe('React surfaces', () => {
  it('renders viewer and editor with the same semantic HTML', async () => {
    const markdown = '# 제목\n\n본문'
    const { container } = render(
      <>
        <MarkdownEditor markdown={markdown} />
        <MarkdownViewer markdown={markdown} />
      </>,
    )

    await waitFor(() => {
      expect(container.querySelectorAll('.ProseMirror')).toHaveLength(2)
    })

    const surfaces = container.querySelectorAll('.ProseMirror')
    expect(surfaces[0]?.innerHTML).toBe(surfaces[1]?.innerHTML)
    expect(surfaces[0]).toHaveAttribute('contenteditable', 'true')
    expect(surfaces[1]).toHaveAttribute('contenteditable', 'false')
  })

  it('opens the shared slash menu while preserving the trigger query', async () => {
    const user = userEvent.setup()
    const { container } = render(<MarkdownEditor markdown="" />)

    await waitFor(() => expect(container.querySelector('.ProseMirror')).toBeInTheDocument())
    const editor = container.querySelector('.ProseMirror') as HTMLElement
    editor.focus()
    await user.keyboard('/')

    expect(await screen.findByTestId('slash-command-menu')).toBeInTheDocument()
    expect(editor).toHaveTextContent('/')
  })

  it('updates state hooks and commands through the same editor instance', async () => {
    const { getByRole, getByTestId } = render(<HookProbe />)

    await waitFor(() => expect(getByTestId('hook-state')).toHaveTextContent('초안'))
    await userEvent.setup().click(getByRole('button', { name: 'set' }))
    await waitFor(() => expect(getByTestId('hook-state')).toHaveTextContent('명령'))
  })

  it('keeps runtime link attributes out of the canonical Markdown model', async () => {
    const target = 'akb://fixture/doc/available.md'
    const markdown = `[Available document](${target})`
    const onChange = vi.fn()
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null

    function LinkSurface() {
      const editor = useMarkdownEditor({ initialMarkdown: markdown, onChange })
      useEffect(() => {
        activeEditor = editor
      }, [editor])
      return editor ? <EditorContent editor={editor} /> : null
    }

    const { container } = render(<LinkSurface />)
    await waitFor(() => expect(activeEditor?.view).toBeTruthy())
    const link = container.querySelector<HTMLAnchorElement>('.ProseMirror a[href]')!

    await act(async () => {
      link.setAttribute('data-markdown-target', target)
      link.setAttribute('data-markdown-resolution', 'pending')
      link.setAttribute('aria-disabled', 'true')
      link.setAttribute('href', '#')
      await new Promise(resolve => setTimeout(resolve, 20))
    })

    expect(link).toHaveAttribute('href', '#')
    expect(link).toHaveAttribute('data-markdown-resolution', 'pending')
    expect(activeEditor!.getMarkdown()).toBe(markdown)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('supports controlled Markdown updates without replacing an unchanged editor', async () => {
    function Controlled() {
      const [markdown, setMarkdown] = useState('처음')
      return (
        <>
          <button type="button" onClick={() => setMarkdown('외부 갱신')}>update</button>
          <MarkdownEditor markdown={markdown} onChange={setMarkdown} />
        </>
      )
    }

    const { getByRole, container } = render(<Controlled />)
    await waitFor(() => expect(container.querySelector('.ProseMirror')).toHaveTextContent('처음'))
    await userEvent.setup().click(getByRole('button', { name: 'update' }))
    await waitFor(() => expect(container.querySelector('.ProseMirror')).toHaveTextContent('외부 갱신'))
  })

  it('preserves selection and undo history across an unchanged mode roundtrip', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null

    function ControlledSurface() {
      const [markdown, setMarkdown] = useState('One two')
      const editor = useMarkdownEditor({
        initialMarkdown: 'One two',
        onChange: (next, editor) => {
          onChange(next, editor)
          setMarkdown(next)
        },
      })
      useEffect(() => {
        activeEditor = editor
      }, [editor])

      return (
        <MarkdownEditingSurface
          editor={editor}
          markdown={markdown}
          onSourceChange={next => setMarkdown(next)}
          toolbar={<button type="button">Formatting tool</button>}
        >
          {editor ? <EditorContent editor={editor} /> : null}
        </MarkdownEditingSurface>
      )
    }

    const { container } = render(<ControlledSurface />)
    const view = within(container)
    await waitFor(() => expect(activeEditor?.view).toBeTruthy())

    const editor = activeEditor!
    await act(async () => {
      editor.commands.setTextSelection({ from: 2, to: 5 })
      editor.commands.insertContent('!')
    })
    const before = {
      from: editor.state.selection.from,
      to: editor.state.selection.to,
    }
    const markdownBefore = editor.getMarkdown()
    const changeCountBefore = onChange.mock.calls.length
    expect(editor.can().undo()).toBe(true)

    await user.click(view.getByRole('button', { name: 'Source' }))
    expect(view.getByRole('textbox', { name: 'Markdown source' })).toHaveValue(markdownBefore)
    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))

    expect(editor.state.selection).toMatchObject(before)
    expect(editor.can().undo()).toBe(true)
    expect(onChange).toHaveBeenCalledTimes(changeCountBefore)

    await act(async () => {
      editor.commands.undo()
    })
    expect(editor.getMarkdown()).not.toBe(markdownBefore)
  })

  it('edits the same Markdown draft in Source and reflects external values and readOnly', async () => {
    const user = userEvent.setup()
    const sourceChanges = vi.fn()
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null

    function ControlledSurface() {
      const [markdown, setMarkdown] = useState('# Original')
      const [readOnly, setReadOnly] = useState(false)
      const editor = useMarkdownEditor({
        initialMarkdown: '# Original',
        editable: !readOnly,
        onChange: setMarkdown,
      })
      useEffect(() => {
        activeEditor = editor
      }, [editor])

      return (
        <>
          <button type="button" onClick={() => setMarkdown('**External body**')}>
            Set external body
          </button>
          <button type="button" onClick={() => setReadOnly(value => !value)}>
            Toggle read only
          </button>
          <output data-testid="markdown-value">{markdown}</output>
          <MarkdownEditingSurface
            editor={editor}
            markdown={markdown}
            readOnly={readOnly}
            onSourceChange={(next, editor) => {
              sourceChanges(next, editor)
              setMarkdown(next)
            }}
            sourceLabel="Markdown source"
            toolbar={<button type="button">Formatting tool</button>}
          >
            {editor ? <EditorContent editor={editor} /> : null}
          </MarkdownEditingSurface>
        </>
      )
    }

    const { container } = render(<ControlledSurface />)
    const view = within(container)
    await waitFor(() => expect(activeEditor?.view).toBeTruthy())
    const editor = activeEditor!

    await user.click(view.getByRole('button', { name: 'Source' }))
    const source = view.getByRole('textbox', { name: 'Markdown source' })
    expect(source).toHaveValue('# Original')
    expect(view.queryByRole('button', { name: 'Formatting tool' })).not.toBeInTheDocument()
    await user.type(source, '/')
    expect(view.queryByTestId('slash-command-menu')).not.toBeInTheDocument()
    const editedMarkdown = '## Edited\n\n![diagram](/api/assets/00000000-0000-4000-8000-000000000001)\n\n<!-- keep -->'
    fireEvent.compositionStart(source)
    fireEvent.change(source, { target: { value: editedMarkdown } })
    fireEvent.compositionEnd(source, { data: 'keep' })
    expect(source).toHaveValue(editedMarkdown)

    expect(sourceChanges).toHaveBeenLastCalledWith(
      editedMarkdown,
      editor,
    )
    expect(view.getByTestId('markdown-value')).toHaveTextContent('<!-- keep -->')

    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))
    expect(editor).toBe(activeEditor)
    expect(container.querySelector('h2')).toHaveTextContent('Edited')
    expect(container.querySelector('img')).toHaveAttribute(
      'data-markdown-target',
      '/api/assets/00000000-0000-4000-8000-000000000001',
    )
    expect(editor.getMarkdown()).toContain('<!-- keep -->')

    await user.click(view.getByRole('button', { name: 'Source' }))
    await user.click(view.getByRole('button', { name: 'Set external body' }))
    expect(view.getByRole('textbox', { name: 'Markdown source' })).toHaveValue('**External body**')
    await user.click(view.getByRole('button', { name: 'Toggle read only' }))
    const readOnlySource = view.getByRole('textbox', { name: 'Markdown source' })
    expect(readOnlySource).toHaveProperty('readOnly', true)
    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))
    await waitFor(() =>
      expect(container.querySelector('.ProseMirror')).toHaveAttribute('contenteditable', 'false'),
    )
    await user.click(view.getByRole('button', { name: 'Source' }))
    expect(view.getByRole('textbox', { name: 'Markdown source' })).toHaveProperty('readOnly', true)
  })

  it('applies runtime URLs without changing the canonical image target', async () => {
    const target = '/api/assets/00000000-0000-4000-8000-000000000001'
    const resolver = {
      resolve: async (value: string) => ({
        target: value,
        status: 'available' as const,
        runtimeUrl: 'blob:runtime-image',
      }),
    }
    const { container } = render(
      <MarkdownEditor
        markdown={`![diagram](${target})`}
        adapters={{ targetResolver: resolver }}
      />,
    )

    await waitFor(() => {
      const image = container.querySelector('img[data-markdown-target]')
      expect(image).toHaveAttribute('src', 'blob:runtime-image')
      expect(image).toHaveAttribute('data-markdown-target', target)
    })
  })

  it('renders an unavailable placeholder while preserving the source target', async () => {
    const target = 'akb://vault/coll/notes/doc/missing.md'
    const resolver = {
      resolve: async (value: string) => ({
        target: value,
        status: 'unavailable' as const,
        reason: 'unknown' as const,
      }),
    }
    const { container } = render(
      <MarkdownViewer
        markdown={`[Missing](${target})`}
        adapters={{ targetResolver: resolver }}
      />,
    )

    await waitFor(() => {
      const link = container.querySelector('a[data-markdown-target]')
      expect(link).toHaveAttribute('data-markdown-resolution', 'unavailable')
      expect(link).toHaveAttribute('href', '#')
      expect(link).toHaveAttribute('data-markdown-target', target)
    })
  })

  it('provides per-image controls, undo, serialization, and focus return through the public surface', async () => {
    const user = userEvent.setup()
    const target = 'https://example.com/shared.png'
    const initialMarkdown = [
      `![First](${target})`,
      `![Second](${target})`,
      '![Other](https://example.com/other.png)',
    ].join('\n\n')
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null

    function ImageSurface() {
      const [markdown, setMarkdown] = useState(initialMarkdown)
      const editor = useMarkdownEditor({
        initialMarkdown,
        onChange: setMarkdown,
      })
      useEffect(() => {
        activeEditor = editor
      }, [editor])

      return (
        <MarkdownEditingSurface
          editor={editor}
          markdown={markdown}
          imageMenu={{}}
          onSourceChange={setMarkdown}
        >
          {editor ? <EditorContent editor={editor} /> : null}
        </MarkdownEditingSurface>
      )
    }

    const { container } = render(<ImageSurface />)
    await waitFor(() => {
      expect(activeEditor?.view).toBeTruthy()
      expect(container.querySelectorAll('[data-markdown-image-controls="true"]')).toHaveLength(3)
    })

    const editor = activeEditor!
    const editSecond = screen.getByRole('button', { name: 'Edit image description: Second' })
    editSecond.focus()
    await user.keyboard('{Enter}')
    const description = screen.getByRole('textbox', { name: 'Description' })
    await user.clear(description)
    await user.type(description, 'Second updated')
    await user.keyboard('{Escape}')

    await waitFor(() => expect(document.activeElement).toBe(editor.view.dom))
    expect(editor.getMarkdown()).toContain(`![Second](${target})`)
    await user.click(editSecond)
    const cancelledDescription = screen.getByRole('textbox', { name: 'Description' })
    await user.clear(cancelledDescription)
    await user.type(cancelledDescription, 'Discarded edit')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(document.activeElement).toBe(editor.view.dom))
    expect(editor.getMarkdown()).toContain(`![Second](${target})`)
    await user.click(editSecond)
    const savedDescription = screen.getByRole('textbox', { name: 'Description' })
    await user.clear(savedDescription)
    await user.type(savedDescription, 'Second updated')
    await user.click(screen.getByRole('button', { name: 'Save description' }))

    await waitFor(() => expect(editor.getMarkdown()).toContain(`![Second updated](${target})`))
    expect(editor.getMarkdown()).toContain(`![First](${target})`)
    expect(editor.getMarkdown()).toContain('![Other](https://example.com/other.png)')

    const savedMarkdown = editor.getMarkdown()
    const reopenedEditor = createMarkdownEditor({ initialMarkdown: savedMarkdown })
    const reopenedAlts: string[] = []
    reopenedEditor.state.doc.descendants(node => {
      if (node.type.name === 'image') reopenedAlts.push(String(node.attrs.alt ?? ''))
    })
    expect(reopenedAlts).toEqual(['First', 'Second updated', 'Other'])
    reopenedEditor.destroy()

    expect(editor.can().undo()).toBe(true)
    await act(async () => editor.commands.undo())
    expect(editor.getMarkdown()).toContain(`![Second](${target})`)
    await act(async () => editor.commands.redo())
    expect(editor.getMarkdown()).toContain(`![Second updated](${target})`)

    await user.click(screen.getByRole('button', { name: 'Remove image: First' }))
    await waitFor(() => expect(editor.getMarkdown()).not.toContain(`![First](${target})`))
    expect(editor.getMarkdown()).toContain(`![Second updated](${target})`)
    await act(async () => editor.commands.undo())
    expect(editor.getMarkdown()).toContain(`![First](${target})`)

    expect(editor.getMarkdown()).toContain(`![First](${target})`)
    expect(editor.getMarkdown()).toContain(`![Second updated](${target})`)
  })

  it('does not expose image mutation controls in read-only mode', async () => {
    const target = 'https://example.com/image.png'
    const { container } = render(
      <MarkdownEditor
        markdown={`![Image](${target})`}
        readOnly
        imageMenu={{}}
      />,
    )

    await waitFor(() => expect(container.querySelector('.ProseMirror')).toHaveAttribute('contenteditable', 'false'))
    expect(container.querySelectorAll('[data-markdown-image-controls="true"]')).toHaveLength(0)
    expect(within(container).queryAllByRole('button', { name: /image description|remove image/i })).toHaveLength(0)
  })
})
