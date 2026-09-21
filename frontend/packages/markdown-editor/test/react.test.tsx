import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { useEffect, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import {
  createMarkdownEditor,
  EditorContent,
  MarkdownEditor,
  MarkdownEditingSurface,
  MarkdownSurface,
  MarkdownViewer,
  useMarkdownCommands,
  useMarkdownEditor,
  useMarkdownTargetResolutions,
  useMarkdownState,
} from '../src/index.js'
import type {
  MarkdownTargetResolution,
  MarkdownTargetResolver,
  MarkdownTargetResolverContext,
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

function ResolutionProbe({
  markdown,
  resolver,
  context,
}: {
  markdown: string
  resolver: MarkdownTargetResolver
  context?: MarkdownTargetResolverContext
}) {
  const resolutions = useMarkdownTargetResolutions(markdown, resolver, context)
  const target = resolutions.values().next().value as MarkdownTargetResolution | undefined
  return (
    <output
      data-testid="resolution"
      data-runtime-url={target?.status === 'available' ? target.runtimeUrl : ''}
      data-markdown={markdown}
    />
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

  it('keeps editor and viewer image semantics, sizing, and failure copy aligned', async () => {
    const target = '/api/assets/00000000-0000-4000-8000-000000000002'
    const resolver: MarkdownTargetResolver = {
      resolve: async value => ({
        target: value,
        status: 'available' as const,
        runtimeUrl: 'blob:shared-image',
      }),
    }
    const markdown = `![Transparent diagram](${target} "Original title")`
    const onChange = vi.fn()
    const { container } = render(
      <>
        <MarkdownEditor markdown={markdown} onChange={onChange} adapters={{ targetResolver: resolver }} />
        <MarkdownViewer markdown={markdown} adapters={{ targetResolver: resolver }} />
      </>,
    )

    await waitFor(() => {
      const images = [...container.querySelectorAll<HTMLImageElement>('img[data-markdown-target]')]
      expect(images).toHaveLength(2)
      expect(images.every(image => image.getAttribute('src') === 'blob:shared-image')).toBe(true)
    })
    const images = [...container.querySelectorAll<HTMLImageElement>('img[data-markdown-target]')]
    for (const image of images) {
      expect(image).toHaveAttribute('src', 'blob:shared-image')
      expect(image).toHaveAttribute('alt', 'Transparent diagram')
      expect(image).toHaveAttribute('title', 'Original title')
      expect(image).toHaveClass('block', 'h-auto', 'max-w-full')
      expect(image.closest('[data-markdown-image-frame]')).toHaveClass('max-w-full')
    }
    expect(onChange).not.toHaveBeenCalled()

    for (const image of images) fireEvent.error(image)
    expect(container.querySelectorAll('[data-markdown-image-state="decode"]')).toHaveLength(2)
    expect(screen.getAllByRole('img', { name: 'Image unavailable: Transparent diagram' })).toHaveLength(2)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('renders request failures as accessible image placeholders without losing the target', async () => {
    const target = '/api/assets/00000000-0000-4000-8000-000000000003'
    const resolver: MarkdownTargetResolver = {
      resolve: async value => ({
        target: value,
        status: 'unavailable' as const,
        reason: 'inaccessible' as const,
      }),
    }
    const { container } = render(
      <MarkdownViewer
        markdown={`![Restricted diagram](${target})`}
        adapters={{ targetResolver: resolver }}
      />,
    )

    await waitFor(() => {
      const frame = container.querySelector('[data-markdown-image-frame]')
      expect(frame).toHaveAttribute('data-markdown-image-state', 'unavailable')
      expect(frame).toHaveAttribute('data-markdown-target', target)
    })
    expect(screen.getByRole('img', { name: 'Image unavailable: Restricted diagram' })).toBeVisible()
    expect(container.querySelector('img[data-markdown-target]')).toHaveAttribute(
      'data-markdown-target',
      target,
    )
    expect(container.querySelector('img[data-markdown-target]')).not.toHaveAttribute('src')
  })

  it('releases runtime image resources on context changes and unmount', async () => {
    const target = '/api/assets/00000000-0000-4000-8000-000000000004'
    const releases: Array<ReturnType<typeof vi.fn>> = []
    const resolver: MarkdownTargetResolver = {
      resolve: vi.fn(async (value, context) => {
        const release = vi.fn()
        releases.push(release)
        return {
          target: value,
          status: 'available' as const,
          runtimeUrl: `blob:${context?.document ?? 'initial'}`,
          release,
        }
      }),
    }
    const { container, rerender, unmount } = render(
      <MarkdownEditor
        markdown={`![Diagram](${target})`}
        adapters={{ targetResolver: resolver }}
        resolverContext={{ document: 'first.md' }}
      />,
    )

    await waitFor(() => expect(container.querySelector('img')).toHaveAttribute('src', 'blob:first.md'))
    rerender(
      <MarkdownEditor
        markdown={`![Diagram](${target})`}
        adapters={{ targetResolver: resolver }}
        resolverContext={{ document: 'second.md' }}
      />,
    )
    await waitFor(() => expect(container.querySelector('img')).toHaveAttribute('src', 'blob:second.md'))
    expect(releases[0]).toHaveBeenCalledTimes(1)
    unmount()
    expect(releases[1]).toHaveBeenCalledTimes(1)
  })

  it('keeps image-only documents paragraph-safe and serializes without a caret paragraph', async () => {
    const target = 'https://example.com/standalone.png'
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null

    function ImageOnlySurface() {
      const editor = useMarkdownEditor({ initialMarkdown: `![Only image](${target})` })
      useEffect(() => {
        activeEditor = editor
      }, [editor])
      return <MarkdownSurface editor={editor} editable />
    }

    const { container } = render(<ImageOnlySurface />)
    await waitFor(() => expect(activeEditor?.view).toBeTruthy())
    expect(container.querySelector('.ProseMirror > p > [data-markdown-image-frame]')).toBeInTheDocument()
    expect(activeEditor!.getMarkdown()).toBe(`![Only image](${target})`)

    await act(async () => {
      activeEditor!.commands.focus('end')
      activeEditor!.commands.insertContent('Continue below')
    })
    expect(activeEditor!.getMarkdown()).toContain(`![Only image](${target})`)
    expect(activeEditor!.getMarkdown()).toContain('Continue below')
  })

  it('re-resolves expiring targets before expiry without changing canonical Markdown', async () => {
    vi.useFakeTimers()
    try {
      const target = 'akb://fixture/file/expiring.bin'
      const markdown = `[Download](${target})`
      let calls = 0
      const resolver: MarkdownTargetResolver = {
        resolve: vi.fn(async value => {
          calls += 1
          return {
            target: value,
            status: 'available' as const,
            runtimeUrl: `/runtime/${calls}`,
            expiresAt: new Date(Date.now() + 2_000).toISOString(),
          }
        }),
      }

      render(<ResolutionProbe markdown={markdown} resolver={resolver} />)
      await act(async () => {
        await Promise.resolve()
      })

      expect(screen.getByTestId('resolution')).toHaveAttribute('data-runtime-url', '/runtime/1')
      expect(calls).toBe(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_001)
      })

      expect(calls).toBe(2)
      expect(screen.getByTestId('resolution')).toHaveAttribute('data-runtime-url', '/runtime/2')
      expect(screen.getByTestId('resolution')).toHaveAttribute('data-markdown', markdown)
    } finally {
      vi.useRealTimers()
    }
  })

  it('discards a late resolution from a previous document context', async () => {
    const target = 'akb://fixture/doc/context.md'
    const oldResult: MarkdownTargetResolution = {
      target,
      status: 'available',
      runtimeUrl: '/runtime/old',
    }
    let releaseOld!: (resolution: MarkdownTargetResolution) => void
    const oldPromise = new Promise<MarkdownTargetResolution>(resolve => {
      releaseOld = resolve
    })
    const resolver: MarkdownTargetResolver = {
      resolve: vi.fn((value, context) =>
        context?.document === 'old'
          ? oldPromise
          : Promise.resolve({
              target: value,
              status: 'available' as const,
              runtimeUrl: '/runtime/current',
            }),
      ),
    }

    const { container, rerender } = render(
      <ResolutionProbe
        markdown={`[Document](${target})`}
        resolver={resolver}
        context={{ document: 'old' }}
      />,
    )
    rerender(
      <ResolutionProbe
        markdown={`[Document](${target})`}
        resolver={resolver}
        context={{ document: 'current' }}
      />,
    )

    const view = within(container)
    await waitFor(() =>
      expect(view.getByTestId('resolution')).toHaveAttribute('data-runtime-url', '/runtime/current'),
    )
    await act(async () => {
      releaseOld(oldResult)
      await oldPromise
    })

    expect(view.getByTestId('resolution')).toHaveAttribute('data-runtime-url', '/runtime/current')
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

  it('keeps external images visible when a target resolver is present', async () => {
    const externalTarget = 'https://example.com/external.png'
    const managedTarget = '/api/assets/00000000-0000-4000-8000-000000000099'
    const resolver = {
      resolve: async (value: string) => ({
        target: value,
        status: 'unavailable' as const,
        reason: 'unknown' as const,
      }),
    }
    const { container } = render(
      <MarkdownViewer
        markdown={`![External](${externalTarget})\n\n![Managed](${managedTarget})`}
        adapters={{ targetResolver: resolver }}
      />,
    )

    await waitFor(() => {
      expect(container.querySelector(`img[data-markdown-target="${externalTarget}"]`)).toHaveAttribute(
        'src',
        externalTarget,
      )
    })
    await waitFor(() => {
      const managedImage = container.querySelector(`img[data-markdown-target="${managedTarget}"]`)
      expect(managedImage).toHaveAttribute('data-markdown-resolution', 'unavailable')
      expect(managedImage).not.toHaveAttribute('src')
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
