import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { EditorContent } from '@tiptap/react'
import { MarkdownManager } from '@tiptap/markdown'
import { useEffect, useRef, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  MarkdownEditor,
  MarkdownEditingSurface,
  MarkdownSurface,
  MarkdownToolbar,
  MarkdownViewer,
  useMarkdownCommands,
  useMarkdownEditor,
  useMarkdownReferenceResolutions,
  useMarkdownTargetResolutions,
  useMarkdownState,
} from '../src/index.js'
import { createMarkdownEditor } from '../src/core.js'
import { getMarkdownEditor } from '../src/react/editor-handle.js'
import type {
  MarkdownSourceChangeResult,
  MarkdownReferenceAdapter,
  MarkdownTargetResolution,
  MarkdownTargetResolver,
  MarkdownTargetResolverContext,
} from '../src/index.js'

afterEach(cleanup)

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
  it('does not serialize or parse the whole document when only moving the cursor', async () => {
    let handle: ReturnType<typeof useMarkdownEditor> = null
    function Surface() {
      const editor = useMarkdownEditor({ initialMarkdown: 'A **large** document\n\nAnother paragraph' })
      useEffect(() => { handle = editor }, [editor])
      return <><MarkdownToolbar editor={editor} /><MarkdownSurface editor={editor} editable /></>
    }
    const { container, unmount } = render(<Surface />)
    await waitFor(() => expect(container.querySelector('.ProseMirror')).toBeTruthy())
    const editor = getMarkdownEditor(handle)!
    const getMarkdown = vi.spyOn(editor, 'getMarkdown')
    const parse = vi.spyOn(MarkdownManager.prototype, 'parse')
    await act(async () => { editor.commands.setTextSelection(5) })
    expect(getMarkdown).not.toHaveBeenCalled()
    expect(parse).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Bold' })).toHaveAttribute('aria-pressed', 'true')
    unmount()
  })

  it('keeps resolved targets when editing unrelated text', async () => {
    const resolve = vi.fn(async (target: string) => ({ target, status: 'available' as const, runtimeUrl: '/resolved' }))
    const resolver = { resolve }
    const { rerender, unmount } = render(<ResolutionProbe markdown="[Document](akb://fixture/doc/a.md)" resolver={resolver} />)
    await waitFor(() => expect(screen.getByTestId('resolution')).toHaveAttribute('data-runtime-url', '/resolved'))
    rerender(<ResolutionProbe markdown="Changed text. [Document](akb://fixture/doc/a.md)" resolver={resolver} />)
    await act(async () => {})
    expect(resolve).toHaveBeenCalledTimes(1)
    unmount()
  })

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
      return editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null
    }

    const { container } = render(<LinkSurface />)
    await waitFor(() => expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy())
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
    expect(getMarkdownEditor(activeEditor)!.getMarkdown()).toBe(markdown)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('shares reference display between editor and viewer without serializing runtime data', async () => {
    const markdown = '@alice REEF-123'
    const adapter: MarkdownReferenceAdapter = {
      search: async () => [],
      resolve: vi.fn(async reference => ({
        ...reference,
        status: 'available' as const,
        title: reference.kind === 'person' ? 'Ada Lovelace' : 'Reference issue',
        runtimeUrl:
          reference.kind === 'person' ? '/people/alice' : '/issues/REEF-123',
      })),
    }
    const onChange = vi.fn()
    const { container } = render(
      <>
        <MarkdownEditor
          markdown={markdown}
          onChange={onChange}
          reference={{ adapter }}
        />
        <MarkdownViewer markdown={markdown} reference={{ adapter }} />
      </>,
    )

    await waitFor(() => {
      expect(container.querySelectorAll('[data-markdown-reference-resolution="available"]')).toHaveLength(4)
    })

    const editorSurface = container.querySelector<HTMLElement>('.ProseMirror[contenteditable="true"]')!
    const viewerSurface = container.querySelector<HTMLElement>('.ProseMirror[contenteditable="false"]')!
    const editorReferences = editorSurface.querySelectorAll<HTMLElement>('[data-markdown-reference]')
    const viewerReferences = viewerSurface.querySelectorAll<HTMLElement>('[data-markdown-reference]')
    expect(editorReferences).toHaveLength(2)
    expect(editorReferences[0]).toHaveAttribute('role', 'link')
    expect(editorReferences[0]).toHaveAttribute('tabindex', '0')
    expect(editorReferences[0]).not.toHaveAttribute('href')
    expect(editorReferences[0]).toHaveAttribute('data-markdown-reference-runtime-url', '/people/alice')
    expect(editorReferences[0]).toHaveAttribute('data-markdown-reference-title', 'Ada Lovelace')
    expect(editorReferences[0]).toHaveTextContent('@alice')
    expect(editorReferences[1]).toHaveAttribute('role', 'link')
    expect(editorReferences[1]).toHaveAttribute('data-markdown-reference-runtime-url', '/issues/REEF-123')
    expect(editorReferences[1]).toHaveTextContent('REEF-123')
    expect(editorReferences[1]).toHaveAttribute('data-markdown-reference-title', 'Reference issue')
    const editorClick = new MouseEvent('click', { bubbles: true, cancelable: true })
    editorReferences[0]?.dispatchEvent(editorClick)
    expect(editorClick.defaultPrevented).toBe(true)
    expect(viewerReferences).toHaveLength(2)
    expect(viewerReferences[0]).toHaveAttribute('href', '/people/alice')
    expect(viewerReferences[1]).toHaveAttribute('href', '/issues/REEF-123')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('resolves escaped person identity exactly and keeps the latest React edit in history', async () => {
    const username = String.raw`team@ops\blue}`
    const token = String.raw`@{team@ops\\blue\}}`
    const editedUsername = String.raw`team@ops\blue2}`
    const editedToken = String.raw`@{team@ops\\blue2\}}`
    const markdown = [
      `Before ${token} and @{unknown} REEF-123`,
      '[@link](https://example.com) and `@inline`',
      String.raw`\@escaped after`,
    ].join('\n\n')
    const editedMarkdown = markdown.replace(token, editedToken)
    const resolvedIds: string[] = []
    const adapter: MarkdownReferenceAdapter = {
      search: async () => [],
      resolve: async reference => {
        resolvedIds.push(reference.id)
        if (reference.kind === 'person' && reference.id === username) {
          return {
            ...reference,
            status: 'available' as const,
            title: 'Exact roster person',
            runtimeUrl: '/people/exact',
          }
        }
        if (reference.kind === 'person' && reference.id === editedUsername) {
          return {
            ...reference,
            status: 'available' as const,
            title: 'Edited roster person',
            runtimeUrl: '/people/edited',
          }
        }
        return { ...reference, status: 'unavailable' as const, reason: 'unknown' as const }
      },
    }
    const reference = { adapter }
    const onChange = vi.fn()
    const user = userEvent.setup()
    let activeHandle: ReturnType<typeof useMarkdownEditor> = null
    let activeCommands: ReturnType<typeof useMarkdownCommands> | null = null

    function ReferenceSurface() {
      const [editable, setEditable] = useState(true)
      const [currentMarkdown, setCurrentMarkdown] = useState(markdown)
      const handle = useMarkdownEditor({
        initialMarkdown: markdown,
        onChange: next => {
          onChange(next)
          setCurrentMarkdown(next)
        },
        reference,
        editable,
      })
      const commands = useMarkdownCommands(handle)
      const state = useMarkdownState(handle)
      const resolutions = useMarkdownReferenceResolutions(currentMarkdown, adapter)
      useEffect(() => {
        activeHandle = handle
        activeCommands = commands
      }, [commands, handle])

      return (
        <>
          <output data-testid="reference-body">{currentMarkdown}</output>
          <output data-testid="markdown-state">{state?.markdown}</output>
          <MarkdownSurface
            editor={handle}
            editable={editable}
            referenceResolutions={resolutions}
            resolvingReferences
          />
          <MarkdownViewer markdown={currentMarkdown} reference={reference} />
          <button type="button" onClick={() => setEditable(false)}>Make read only</button>
        </>
      )
    }

    const { container } = render(<ReferenceSurface />)
    await waitFor(() => {
      expect(container.querySelectorAll('[data-markdown-reference-resolution="available"]'))
        .toHaveLength(2)
      expect(resolvedIds).toContain(username)
      expect(resolvedIds).toContain('unknown')
      expect(resolvedIds).not.toContain('link')
      expect(resolvedIds).not.toContain('inline')
      expect(resolvedIds).not.toContain('escaped')
      expect(screen.getByTestId('markdown-state').textContent).toBe(markdown)
    })

    const editorSurface = container.querySelector<HTMLElement>('.ProseMirror[contenteditable="true"]')!
    const viewerSurface = container.querySelector<HTMLElement>('.ProseMirror[contenteditable="false"]')!
    const editorPerson = editorSurface.querySelector<HTMLElement>('[data-markdown-reference-kind="person"]')!
    const viewerPerson = viewerSurface.querySelector<HTMLElement>('[data-markdown-reference-kind="person"]')!
    expect(editorPerson).toHaveAttribute('data-markdown-reference-id', username)
    expect(editorPerson).toHaveAttribute('data-markdown-reference-value', token)
    expect(editorPerson).toHaveAttribute('data-markdown-reference-title', 'Exact roster person')
    expect(viewerPerson).toHaveAttribute('data-markdown-reference-id', username)
    expect(viewerPerson).toHaveAttribute('href', '/people/exact')
    expect(onChange).not.toHaveBeenCalled()

    const editor = getMarkdownEditor(activeHandle)!
    const tokenOffset = markdown.indexOf(token)
    const editPosition = 1 + tokenOffset + token.indexOf('blue') + 'blue'.length
    await act(async () => {
      editor.commands.setTextSelection(editPosition)
      editor.commands.insertContent('2')
    })
    await waitFor(() => {
      expect(screen.getByTestId('reference-body')).toHaveTextContent(editedToken)
      expect(screen.getByTestId('markdown-state').textContent).toBe(editedMarkdown)
      expect(onChange.mock.lastCall?.[0]).toBe(editedMarkdown)
      expect(resolvedIds).toContain(editedUsername)
    })
    expect(onChange.mock.lastCall?.[0]).not.toContain('Exact roster person')
    expect(onChange.mock.lastCall?.[0]).not.toContain('/people/exact')

    await act(async () => { activeCommands?.undo() })
    await waitFor(() => {
      expect(screen.getByTestId('reference-body')).toHaveTextContent(token)
      expect(screen.getByTestId('markdown-state').textContent).toBe(markdown)
      expect(onChange.mock.lastCall?.[0]).toBe(markdown)
    })
    await act(async () => { activeCommands?.redo() })
    await waitFor(() => {
      expect(screen.getByTestId('reference-body')).toHaveTextContent(editedToken)
      expect(screen.getByTestId('markdown-state').textContent).toBe(editedMarkdown)
      expect(onChange.mock.lastCall?.[0]).toBe(editedMarkdown)
    })

    const changeCount = onChange.mock.calls.length
    await user.click(screen.getByRole('button', { name: 'Make read only' }))
    await waitFor(() => expect(container.querySelector('.ProseMirror[contenteditable="false"]'))
      .toBeTruthy())
    const readOnlyEditor = container.querySelector<HTMLElement>(
      '[data-markdown-surface="editor"] .ProseMirror',
    )!
    await user.click(readOnlyEditor)
    await user.keyboard(' blocked')
    expect(screen.getByTestId('reference-body')).toHaveTextContent(editedToken)
    expect(onChange).toHaveBeenCalledTimes(changeCount)
  })

  it('keeps resolved reference display outside saved Markdown after editing inline code', async () => {
    const markdown = 'AKB-359 @person\n\n`inline code`'
    const adapter: MarkdownReferenceAdapter = {
      search: async () => [],
      resolve: async reference => ({
        ...reference,
        status: 'available',
        title: reference.kind === 'person' ? 'Person display name' : 'Issue display title',
        ...(reference.kind === 'issue' ? { runtimeUrl: '/issues/AKB-359' } : {}),
      }),
    }
    const onChange = vi.fn()
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null

    function ReferenceSurface() {
      const editor = useMarkdownEditor({
        initialMarkdown: markdown,
        onChange,
        reference: { adapter },
      })
      const referenceResolutions = useMarkdownReferenceResolutions(markdown, adapter)
      useEffect(() => { activeEditor = editor }, [editor])
      return (
        <MarkdownSurface
          editor={editor}
          editable
          referenceResolutions={referenceResolutions}
          resolvingReferences
        />
      )
    }

    const { container, unmount } = render(<ReferenceSurface />)
    await waitFor(() => {
      expect(container.querySelectorAll('[data-markdown-reference-resolution="available"]'))
        .toHaveLength(2)
    })

    const editor = getMarkdownEditor(activeEditor)!
    let codeTextPosition: number | undefined
    editor.state.doc.descendants((node, position) => {
      if (node.isText && node.marks.some(mark => mark.type.name === 'code')) {
        codeTextPosition = position
      }
    })
    if (codeTextPosition === undefined) throw new Error('inline code was not parsed')
    const codePosition = codeTextPosition
    await act(async () => {
      editor.commands.setTextSelection(codePosition + 1)
      editor.commands.insertContent('!')
    })

    await waitFor(() => expect(onChange).toHaveBeenCalled())
    const saved = onChange.mock.lastCall?.[0] as string
    expect(container.querySelectorAll('[data-markdown-reference-resolution="available"]'))
      .toHaveLength(2)
    expect(container.querySelectorAll('[data-markdown-reference-title]')).toHaveLength(2)
    expect(saved).toContain('AKB-359')
    expect(saved).toContain('@person')
    expect(saved).toContain('`i!nline code`')
    expect(saved).not.toContain('Issue display title')
    expect(saved).not.toContain('Person display name')
    expect(saved).not.toContain('/issues/AKB-359')
    unmount()
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

  it('preserves saved Markdown spelling until a visual edit actually changes the document', async () => {
    const user = userEvent.setup()
    const savedMarkdown = [
      '# Recovery document',
      '',
      'Existing saved payload gamma-630.',
      '',
      '[Canonical recovery][canon]',
      '',
      '<!-- preserved: existing-778225f7 -->',
      '',
      '[canon]: https://example.test/canonical-existing',
    ].join('\n')
    const externalMarkdown = savedMarkdown.replace('gamma-630', 'gamma-631')
    const onChange = vi.fn()
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null

    function ControlledSurface() {
      const [markdown, setMarkdown] = useState(savedMarkdown)
      const editor = useMarkdownEditor({
        initialMarkdown: savedMarkdown,
        onChange: next => {
          onChange(next)
          setMarkdown(next)
        },
      })
      useEffect(() => {
        activeEditor = editor
      }, [editor])

      return (
        <>
          <button type="button" onClick={() => setMarkdown(externalMarkdown)}>
            Set external Markdown
          </button>
          <output data-testid="host-markdown">{markdown}</output>
          <MarkdownEditingSurface
            editor={editor}
            markdown={markdown}
            onSourceChange={setMarkdown}
            toolbar={<MarkdownToolbar editor={editor} />}
          >
            {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
          </MarkdownEditingSurface>
        </>
      )
    }

    const { container } = render(<ControlledSurface />)
    const view = within(container)
    await waitFor(() => expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy())
    expect(onChange).not.toHaveBeenCalled()

    await user.click(view.getByRole('button', { name: 'Source' }))
    const source = view.getByRole('textbox', { name: 'Markdown source' }) as HTMLTextAreaElement
    expect(source).toHaveValue(savedMarkdown)
    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))
    await user.click(view.getByRole('button', { name: 'Source' }))
    expect(source).toHaveValue(savedMarkdown)
    expect(onChange).not.toHaveBeenCalled()

    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))
    await user.click(view.getByRole('button', { name: 'Set external Markdown' }))
    await waitFor(() =>
      expect(view.getByTestId('host-markdown').textContent).toBe(externalMarkdown),
    )
    await user.click(view.getByRole('button', { name: 'Source' }))
    expect(source).toHaveValue(externalMarkdown)
    expect(onChange).not.toHaveBeenCalled()
    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))

    const editor = getMarkdownEditor(activeEditor)!
    await act(async () => {
      editor.commands.setTextSelection(editor.state.doc.content.size - 1)
    })
    editor.view.focus()
    await user.keyboard('visual edit')
    await waitFor(() => expect(onChange).toHaveBeenCalled())
    await user.click(view.getByRole('button', { name: 'Source' }))
    expect(source.value).toContain('visual edit')
  })

  it('lets a product combine controls without replacing the mode and draft lifecycle', async () => {
    const user = userEvent.setup()
    function CustomHeader() {
      const [markdown, setMarkdown] = useState('Draft')
      const [locked, setLocked] = useState(false)
      const editor = useMarkdownEditor({ initialMarkdown: 'Draft', onChange: setMarkdown })
      return <>
        <button onClick={() => setLocked(!locked)}>Lock switching</button>
        <MarkdownEditingSurface editor={editor} markdown={markdown} onSourceChange={setMarkdown}
          modeSwitchDisabled={locked} toolbar={<button>Format draft</button>}
          renderHeader={({ mode, onModeChange, disabled, toolbar }) => <div>
            {toolbar}
            <button disabled={disabled} onClick={() => onModeChange(mode === 'source' ? 'wysiwyg' : 'source')}>Change mode</button>
          </div>}>
          {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
        </MarkdownEditingSurface>
      </>
    }
    const { container } = render(<CustomHeader />)
    const view = within(container)
    expect(view.queryByRole('button', { name: 'WYSIWYG' })).toBeNull()
    expect(view.getAllByRole('button', { name: 'Format draft' })).toHaveLength(1)
    await user.click(view.getByRole('button', { name: 'Lock switching' }))
    expect(view.getByRole('button', { name: 'Change mode' })).toBeDisabled()
    await user.click(view.getByRole('button', { name: 'Lock switching' }))
    await user.click(view.getByRole('button', { name: 'Change mode' }))
    expect(view.getByRole('button', { name: 'Format draft' })).toBeVisible()
    const source = view.getByRole('textbox', { name: 'Markdown source' })
    await user.clear(source)
    await user.type(source, 'Updated draft')
    await user.click(view.getByRole('button', { name: 'Change mode' }))
    expect(container.querySelector('.ProseMirror')).toHaveTextContent('Updated draft')
    expect(view.getAllByRole('button', { name: 'Format draft' })).toHaveLength(1)
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
          toolbar={<MarkdownToolbar editor={editor} />}
        >
          {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
        </MarkdownEditingSurface>
      )
    }

    const { container } = render(<ControlledSurface />)
    const view = within(container)
    await waitFor(() => expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy())

    const editor = getMarkdownEditor(activeEditor)!
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

  it('preserves the Source draft and history when the controlled host echoes canonical Markdown', async () => {
    const user = userEvent.setup()
    const initial = '- item'
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null

    function ControlledSurface() {
      const [markdown, setMarkdown] = useState(initial)
      const editor = useMarkdownEditor({ initialMarkdown: initial })
      useEffect(() => {
        activeEditor = editor
      }, [editor])

      return (
        <>
          <output data-testid="markdown-value">{markdown}</output>
          <MarkdownEditingSurface
            editor={editor}
            markdown={markdown}
            onSourceChange={setMarkdown}
            onMarkdownApplied={appliedEditor =>
              setMarkdown(getMarkdownEditor(appliedEditor)?.getMarkdown() ?? '')
            }
            toolbar={<MarkdownToolbar editor={editor} />}
          >
            {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
          </MarkdownEditingSurface>
        </>
      )
    }

    const { container } = render(<ControlledSurface />)
    const view = within(container)
    await waitFor(() => expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy())

    await user.click(view.getByRole('button', { name: 'Source' }))
    const source = view.getByRole('textbox', { name: 'Markdown source' })
    fireEvent.change(source, { target: { value: '* item' } })
    expect(view.getByRole('button', { name: 'Undo' })).toBeEnabled()

    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))
    const canonicalMarkdown = getMarkdownEditor(activeEditor)!.getMarkdown()
    expect(canonicalMarkdown).not.toBe('* item')
    await waitFor(() =>
      expect(view.getByTestId('markdown-value').textContent).toBe(canonicalMarkdown),
    )

    await user.click(view.getByRole('button', { name: 'Source' }))
    expect(view.getByRole('textbox', { name: 'Markdown source' })).toHaveValue('* item')
    expect(view.getByRole('button', { name: 'Undo' })).toBeEnabled()

    await user.click(view.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(view.getByRole('textbox', { name: 'Markdown source' })).toHaveValue(initial))
    expect(view.getByRole('button', { name: 'Redo' })).toBeEnabled()
  })

  it('refreshes Source from a visual task change before the controlled host echoes it', async () => {
    const user = userEvent.setup()
    const sourceMarkdown = [
      '# Keyboard controls',
      '',
      '- [ ] TASKPARENTAKB330',
      '  - [x] TASKNESTEDAKB330',
      '- [ ] TASKSIBLINGAKB330',
      '',
      '```text',
      'keyboard_code_AKB330',
      '```',
    ].join('\n')
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null
    const visualChanges: string[] = []

    function ControlledSurface() {
      const [markdown, setMarkdown] = useState('')
      const pendingMarkdown = useRef<string | null>(null)
      const editor = useMarkdownEditor({
        initialMarkdown: '',
        onChange: next => {
          pendingMarkdown.current = next
          visualChanges.push(next)
        },
      })
      useEffect(() => {
        activeEditor = editor
      }, [editor])

      return (
        <>
          <button
            type="button"
            onClick={() => {
              if (pendingMarkdown.current !== null) setMarkdown(pendingMarkdown.current)
            }}
          >
            Deliver visual value
          </button>
          <output data-testid="host-markdown">{markdown}</output>
          <MarkdownEditingSurface
            editor={editor}
            markdown={markdown}
            onSourceChange={setMarkdown}
            toolbar={<MarkdownToolbar editor={editor} />}
          >
            {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
          </MarkdownEditingSurface>
        </>
      )
    }

    const { container } = render(<ControlledSurface />)
    const view = within(container)
    await waitFor(() => expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy())

    await user.click(view.getByRole('button', { name: 'Source' }))
    const source = view.getByRole('textbox', { name: 'Markdown source' }) as HTMLTextAreaElement
    fireEvent.change(source, { target: { value: sourceMarkdown } })
    await waitFor(() => expect(source).toHaveValue(sourceMarkdown))

    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))
    const parent = view.getByRole('checkbox', {
      name: 'Task item checkbox for TASKPARENTAKB330',
    })
    await user.click(parent)
    expect(parent).toBeChecked()
    expect(visualChanges.at(-1)).toContain('[x] TASKPARENTAKB330')

    await user.click(view.getByRole('button', { name: 'Source' }))
    expect(source.value).toContain('- [x] TASKPARENTAKB330')
    expect(source.value).toContain('- [x] TASKNESTEDAKB330')
    expect(source.value).toContain('- [ ] TASKSIBLINGAKB330')
    expect(source.value).toContain('```text\nkeyboard_code_AKB330\n```')

    await user.click(view.getByRole('button', { name: 'Deliver visual value' }))
    await waitFor(() => expect(view.getByTestId('host-markdown').textContent).toContain('[x] TASKPARENTAKB330'))
    expect(source.value).toContain('- [x] TASKPARENTAKB330')
  })

  it('keeps the rebased Source draft when an earlier apply echo arrives late', async () => {
    const user = userEvent.setup()
    const initial = '# Initial\n\n- [ ] TASKPARENTAKB331'
    const sourceMarkdown = [
      '# Keyboard controls',
      '',
      '- [ ] TASKPARENTAKB331',
      '  - [x] TASKNESTEDAKB331',
      '- [ ] TASKSIBLINGAKB331',
      '',
      '```text',
      'keyboard_code_AKB331',
      '```',
    ].join('\n')
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null
    const visualChanges: string[] = []
    const sourceChanges: string[] = []
    const probe: {
      commands: ReturnType<typeof useMarkdownCommands> | null
      state: ReturnType<typeof useMarkdownState>
    } = { commands: null, state: null }

    function ControlledSurface() {
      const [markdown, setMarkdown] = useState(initial)
      const pendingAppliedMarkdown = useRef<string | null>(null)
      const editor = useMarkdownEditor({
        initialMarkdown: initial,
        onChange: next => visualChanges.push(next),
      })
      const commands = useMarkdownCommands(editor)
      const state = useMarkdownState(editor)
      useEffect(() => {
        activeEditor = editor
        probe.commands = commands
        probe.state = state
      }, [commands, editor, state])

      return (
        <>
          <button
            type="button"
            onClick={() => {
              if (pendingAppliedMarkdown.current !== null) {
                setMarkdown(pendingAppliedMarkdown.current)
              }
            }}
          >
            Deliver earlier apply value
          </button>
          <output data-testid="host-markdown">{markdown}</output>
          <MarkdownEditingSurface
            editor={editor}
            markdown={markdown}
            onSourceChange={next => {
              sourceChanges.push(next)
              setMarkdown(next)
            }}
            onMarkdownApplied={appliedEditor => {
              pendingAppliedMarkdown.current =
                getMarkdownEditor(appliedEditor)?.getMarkdown() ?? ''
            }}
            toolbar={<MarkdownToolbar editor={editor} />}
          >
            {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
          </MarkdownEditingSurface>
        </>
      )
    }

    const { container } = render(<ControlledSurface />)
    const view = within(container)
    await waitFor(() => expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy())

    await user.click(view.getByRole('button', { name: 'Source' }))
    const source = view.getByRole('textbox', { name: 'Markdown source' }) as HTMLTextAreaElement
    fireEvent.change(source, { target: { value: sourceMarkdown } })
    await waitFor(() => expect(source).toHaveValue(sourceMarkdown))

    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))
    expect(view.getByTestId('host-markdown').textContent).toBe(sourceMarkdown)

    const parent = view.getByRole('checkbox', {
      name: 'Task item checkbox for TASKPARENTAKB331',
    })
    await user.click(parent)
    expect(parent).toBeChecked()
    expect(visualChanges.at(-1)).toContain('[x] TASKPARENTAKB331')

    await user.click(view.getByRole('button', { name: 'Source' }))
    expect(source.value).toContain('- [x] TASKPARENTAKB331')
    expect(source.value).toContain('- [x] TASKNESTEDAKB331')
    expect(source.value).toContain('- [ ] TASKSIBLINGAKB331')
    source.setSelectionRange(8, 16, 'forward')
    fireEvent.select(source)
    await waitFor(() =>
      expect(probe.state?.source.selection).toMatchObject({ start: 8, end: 16 }),
    )
    const selectionAfterRebase = [
      source.selectionStart,
      source.selectionEnd,
      source.selectionDirection,
    ]
    const rebasedSnapshot = probe.state!.source
    const from = source.value.indexOf('TASKSIBLINGAKB331')
    const replacement = 'TASKSIBLINGAKB331-EDITED'

    await user.click(view.getByRole('button', { name: 'Deliver earlier apply value' }))
    await waitFor(() => expect(view.getByTestId('host-markdown').textContent).not.toBe(sourceMarkdown))
    expect(source.value).toContain('- [x] TASKPARENTAKB331')
    expect(view.getByTestId('host-markdown').textContent).toContain('- [x] TASKPARENTAKB331')
    expect(sourceChanges.at(-1)).toBe(source.value)
    expect([
      source.selectionStart,
      source.selectionEnd,
      source.selectionDirection,
    ]).toEqual(selectionAfterRebase)

    let result: MarkdownSourceChangeResult | undefined
    await act(async () => {
      result = probe.commands!.applySourceChange({
        revision: rebasedSnapshot.revision,
        session: rebasedSnapshot.session,
        from,
        to: from + 'TASKSIBLINGAKB331'.length,
        expectedText: 'TASKSIBLINGAKB331',
        replacementText: replacement,
      })
    })
    const updated = rebasedSnapshot.markdown.slice(0, from) + replacement +
      rebasedSnapshot.markdown.slice(from + 'TASKSIBLINGAKB331'.length)
    expect(result).toEqual({ applied: true, markdown: updated })
    await waitFor(() => expect(source).toHaveValue(updated))
    expect(probe.state?.canUndo).toBe(true)
    await user.click(view.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(source).toHaveValue(rebasedSnapshot.markdown))
    expect(probe.state?.canRedo).toBe(true)
  })

  it('applies a distinct external Markdown value while an applied echo is pending', async () => {
    const user = userEvent.setup()
    const initial = '# Initial'
    const pendingAppliedValues: string[] = []
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null

    function ControlledSurface() {
      const [markdown, setMarkdown] = useState(initial)
      const editor = useMarkdownEditor({ initialMarkdown: initial })
      useEffect(() => {
        activeEditor = editor
      }, [editor])

      return (
        <>
          <button type="button" onClick={() => setMarkdown('## External replacement')}>
            Set external Markdown
          </button>
          <button
            type="button"
            onClick={() => {
              const earlierValue = pendingAppliedValues.shift()
              if (earlierValue !== undefined) setMarkdown(earlierValue)
            }}
          >
            Deliver delayed Source echo
          </button>
          <output data-testid="host-markdown">{markdown}</output>
          <MarkdownEditingSurface
            editor={editor}
            markdown={markdown}
            onSourceChange={setMarkdown}
            onMarkdownApplied={appliedEditor => {
              pendingAppliedValues.push(getMarkdownEditor(appliedEditor)?.getMarkdown() ?? '')
            }}
            toolbar={<MarkdownToolbar editor={editor} />}
          >
            {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
          </MarkdownEditingSurface>
        </>
      )
    }

    const { container } = render(<ControlledSurface />)
    const view = within(container)
    await waitFor(() => expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy())

    await user.click(view.getByRole('button', { name: 'Source' }))
    const source = view.getByRole('textbox', { name: 'Markdown source' }) as HTMLTextAreaElement
    fireEvent.change(source, { target: { value: '## Applied source value' } })
    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))
    expect(pendingAppliedValues.length).toBeGreaterThan(0)

    await user.click(view.getByRole('button', { name: 'Set external Markdown' }))
    await waitFor(() =>
      expect(getMarkdownEditor(activeEditor)?.getMarkdown()).toContain('External replacement'),
    )

    await user.click(view.getByRole('button', { name: 'Source' }))
    expect(source.value).toContain('## External replacement')
    await user.click(view.getByRole('button', { name: 'Deliver delayed Source echo' }))
    await waitFor(() =>
      expect(view.getByTestId('host-markdown').textContent).toContain('External replacement'),
    )
    expect(source.value).toContain('## External replacement')
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
            toolbar={<MarkdownToolbar editor={editor} />}
          >
            {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
          </MarkdownEditingSurface>
        </>
      )
    }

    const { container } = render(<ControlledSurface />)
    const view = within(container)
    await waitFor(() => expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy())
    const editor = getMarkdownEditor(activeEditor)!

    await user.click(view.getByRole('button', { name: 'Source' }))
    const source = view.getByRole('textbox', { name: 'Markdown source' })
    expect(source).toHaveValue('# Original')
    expect(view.getByRole('toolbar', { name: 'Text formatting' })).toBeInTheDocument()
    expect(view.getByRole('button', { name: 'Bold' })).toBeDisabled()
    await user.type(source, '/')
    expect(view.queryByTestId('slash-command-menu')).not.toBeInTheDocument()
    const editedMarkdown = '## Edited\n\n![diagram](/api/assets/00000000-0000-4000-8000-000000000001)\n\n<!-- keep -->'
    fireEvent.compositionStart(source)
    fireEvent.change(source, { target: { value: editedMarkdown } })
    fireEvent.compositionEnd(source, { data: 'keep' })
    expect(source).toHaveValue(editedMarkdown)

    expect(sourceChanges).toHaveBeenLastCalledWith(
      editedMarkdown,
      activeEditor,
    )
    expect(view.getByTestId('markdown-value')).toHaveTextContent('<!-- keep -->')

    await user.click(view.getByRole('button', { name: 'WYSIWYG' }))
    expect(editor).toBe(getMarkdownEditor(activeEditor))
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

  it('applies compare-and-swap Source edits without replacing the editor and gives Source its own history', async () => {
    const user = userEvent.setup()
    const target = 'akb://team/coll/notes/doc/guide.md'
    const initial = `Keep this prefix. ${target} and this suffix.\n\n<!-- preserve -->`
    const replacement = `[Guide](${target})`
    const onSourceChange = vi.fn()
    const probe: {
      editor: ReturnType<typeof useMarkdownEditor>
      commands: ReturnType<typeof useMarkdownCommands> | null
      state: ReturnType<typeof useMarkdownState>
    } = { editor: null, commands: null, state: null }

    function ControlledSurface() {
      const [markdown, setMarkdown] = useState(initial)
      const editor = useMarkdownEditor({
        initialMarkdown: initial,
        onChange: setMarkdown,
      })
      const commands = useMarkdownCommands(editor)
      const state = useMarkdownState(editor)
      useEffect(() => {
        probe.editor = editor
        probe.commands = commands
        probe.state = state
      }, [commands, editor, state])

      return (
        <MarkdownEditingSurface
          editor={editor}
          markdown={markdown}
          onSourceChange={next => {
            onSourceChange(next)
            setMarkdown(next)
          }}
          toolbar={<MarkdownToolbar editor={editor} />}
        >
          <MarkdownSurface editor={editor} editable />
        </MarkdownEditingSurface>
      )
    }

    render(<ControlledSurface />)
    await waitFor(() => expect(getMarkdownEditor(probe.editor)?.view).toBeTruthy())
    const handle = probe.editor
    const toolbar = screen.getByRole('toolbar', { name: 'Text formatting' })

    await act(async () => {
      expect(probe.commands?.insertMarkdown(' extra')).toBe(true)
    })
    const editor = getMarkdownEditor(handle)!
    const wysiwygMarkdown = editor.getMarkdown()
    expect(editor.can().undo()).toBe(true)

    await user.click(screen.getByRole('button', { name: 'Source' }))
    const source = await screen.findByRole('textbox', { name: 'Markdown source' }) as HTMLTextAreaElement
    expect(screen.getByRole('toolbar', { name: 'Text formatting' })).toBe(toolbar)
    expect(screen.getByRole('button', { name: 'Bold' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Insert link' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Undo' })).toBeDisabled()
    expect(probe.state?.source.mode).toBe('source')
    expect(probe.editor).toBe(handle)

    const targetStart = source.value.indexOf(target)
    source.setSelectionRange(targetStart, targetStart + target.length)
    fireEvent.select(source)
    await waitFor(() => expect(probe.state?.source.selection).toMatchObject({
      start: targetStart,
      end: targetStart + target.length,
    }))

    const snapshot = probe.state!.source
    const from = snapshot.markdown.indexOf(target)
    expect(from).toBeGreaterThan(0)
    const request = {
      revision: snapshot.revision,
      session: snapshot.session,
      from,
      to: from + target.length,
      expectedText: target,
      replacementText: replacement,
    }
    expect(probe.commands!.applySourceChange({ ...request, from: -1 })).toEqual({
      applied: false,
      reason: 'invalid-range',
    })
    expect(probe.commands!.applySourceChange({ ...request, replacementText: target })).toEqual({
      applied: false,
      reason: 'unchanged',
    })
    let result: MarkdownSourceChangeResult | undefined
    await act(async () => {
      await Promise.resolve()
      result = probe.commands!.applySourceChange(request)
    })

    const updated = snapshot.markdown.slice(0, from) + replacement + snapshot.markdown.slice(from + target.length)
    expect(result).toEqual({ applied: true, markdown: updated })
    await waitFor(() => expect(source).toHaveValue(updated))
    expect(source).toBe(screen.getByRole('textbox', { name: 'Markdown source' }))
    expect(source.selectionStart).toBe(from)
    expect(source.selectionEnd).toBe(from + replacement.length)
    expect(updated).toContain('Keep this prefix.')
    expect(updated).toContain(`(${target})`)
    expect(updated).toContain('and this suffix.')
    expect(updated).toContain('<!-- preserve -->')
    expect(probe.editor).toBe(handle)
    expect(probe.state?.source.mode).toBe('source')
    expect(probe.state?.markdown).toBe(updated)
    expect(onSourceChange).toHaveBeenLastCalledWith(updated)
    expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(source).toHaveValue(snapshot.markdown))
    expect(probe.state?.canUndo).toBe(false)
    expect(probe.state?.canRedo).toBe(true)
    expect(editor.getMarkdown()).toBe(wysiwygMarkdown)
    expect(editor.can().undo()).toBe(true)
    expect(screen.getByRole('button', { name: 'Undo' })).toBeDisabled()

    fireEvent.keyDown(source, { key: 'z', ctrlKey: true, shiftKey: true })
    await waitFor(() => expect(source).toHaveValue(updated))
    fireEvent.keyDown(source, { key: 'z', ctrlKey: true })
    await waitFor(() => expect(source).toHaveValue(snapshot.markdown))
    await user.click(screen.getByRole('button', { name: 'Redo' }))
    await waitFor(() => expect(source).toHaveValue(updated))
    expect(editor.getMarkdown()).toBe(wysiwygMarkdown)
    expect(onSourceChange).toHaveBeenLastCalledWith(updated)

    source.setSelectionRange(0, 0)
    fireEvent.select(source)
    await waitFor(() => expect(probe.state?.source.selection).toMatchObject({ start: 0, end: 0 }))
    const insertionSnapshot = probe.state!.source
    let insertion: MarkdownSourceChangeResult | undefined
    await act(async () => {
      insertion = probe.commands!.applySourceChange({
        revision: insertionSnapshot.revision,
        session: insertionSnapshot.session,
        from: 0,
        to: 0,
        expectedText: '',
        replacementText: 'Intro ',
      })
    })
    expect(insertion).toEqual({ applied: true, markdown: `Intro ${updated}` })
    await waitFor(() => expect(source).toHaveValue(`Intro ${updated}`))
    expect(source.selectionStart).toBe(6)
    expect(source.selectionEnd).toBe(6)
    await user.click(screen.getByRole('button', { name: 'Undo' }))
    await waitFor(() => expect(source).toHaveValue(updated))
  })

  it('rejects deferred Source changes after input, external values, target, composition, readOnly, or mode changes', async () => {
    const user = userEvent.setup()
    const target = 'akb://team/coll/notes/doc/guide.md'
    const initial = `${target}\n\nKeep the authored body.`
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null
    let activeCommands: ReturnType<typeof useMarkdownCommands> | null = null
    let activeState: ReturnType<typeof useMarkdownState> = null
    let setExternalMarkdown: ((value: string) => void) | null = null
    let setContextKey: ((value: string) => void) | null = null
    let setReadOnly: ((value: boolean) => void) | null = null

    function ControlledSurface() {
      const [markdown, setMarkdown] = useState(initial)
      const [contextKey, updateContextKey] = useState('document-a')
      const [readOnly, updateReadOnly] = useState(false)
      const editor = useMarkdownEditor({ initialMarkdown: initial, editable: !readOnly, onChange: setMarkdown })
      const commands = useMarkdownCommands(editor)
      const state = useMarkdownState(editor)
      useEffect(() => {
        activeEditor = editor
        activeCommands = commands
        activeState = state
        setExternalMarkdown = setMarkdown
        setContextKey = updateContextKey
        setReadOnly = updateReadOnly
      }, [commands, editor, readOnly, state])

      return (
        <MarkdownEditingSurface
          editor={editor}
          markdown={markdown}
          sourceContextKey={contextKey}
          readOnly={readOnly}
          onSourceChange={setMarkdown}
          toolbar={<MarkdownToolbar editor={editor} />}
        >
          <MarkdownSurface editor={editor} editable={!readOnly} />
        </MarkdownEditingSurface>
      )
    }

    const ui = render(<ControlledSurface />)
    await waitFor(() => expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy())
    await user.click(screen.getByRole('button', { name: 'Source' }))
    const source = await screen.findByRole('textbox', { name: 'Markdown source' }) as HTMLTextAreaElement

    const readRequest = () => {
      const snapshot = activeState!.source
      const from = snapshot.markdown.indexOf(target)
      return {
        revision: snapshot.revision,
        session: snapshot.session,
        from,
        to: from + target.length,
        expectedText: target,
        replacementText: `[Guide](${target})`,
      }
    }

    const afterType = readRequest()
    const typedValue = `${source.value} User's newer text`
    fireEvent.change(source, { target: { value: typedValue } })
    await waitFor(() => expect(source).toHaveValue(typedValue))
    expect(activeCommands!.applySourceChange(afterType)).toEqual({ applied: false, reason: 'stale' })
    expect(source).toHaveValue(typedValue)

    const targetMismatch = readRequest()
    expect(activeCommands!.applySourceChange({
      ...targetMismatch,
      expectedText: 'another target',
    })).toEqual({ applied: false, reason: 'target-mismatch' })

    const beforeExternalValue = readRequest()
    act(() => setExternalMarkdown?.(`${initial}\nServer revision`))
    await waitFor(() => expect(source).toHaveValue(typedValue))
    expect(activeCommands!.applySourceChange(beforeExternalValue)).toEqual({ applied: false, reason: 'stale' })

    const beforeContextChange = readRequest()
    const previousSession = activeState!.source.session
    act(() => setContextKey?.('document-b'))
    await waitFor(() => expect(activeState!.source.session).toBeGreaterThan(previousSession))
    expect(activeCommands!.applySourceChange(beforeContextChange)).toEqual({ applied: false, reason: 'stale' })

    const duringComposition = readRequest()
    fireEvent.compositionStart(source)
    expect(activeCommands!.undo()).toBe(false)
    expect(screen.getByRole('button', { name: 'Undo' })).toBeDisabled()
    expect(activeCommands!.applySourceChange(duringComposition)).toEqual({ applied: false, reason: 'composing' })
    const composingValue = `${source.value} 한`
    fireEvent.change(source, { target: { value: composingValue } })
    fireEvent.compositionEnd(source, { data: '한' })
    await waitFor(() => expect(source).toHaveValue(composingValue))
    expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled()

    const whileEditable = readRequest()
    act(() => setReadOnly?.(true))
    await waitFor(() => expect(source).toHaveProperty('readOnly', true))
    expect(activeCommands!.applySourceChange(whileEditable)).toEqual({ applied: false, reason: 'read-only' })
    act(() => setReadOnly?.(false))
    await waitFor(() => expect(source).toHaveProperty('readOnly', false))

    const afterReadOnly = readRequest()
    const activeSession = activeState!.source.session
    await user.click(screen.getByRole('button', { name: 'WYSIWYG' }))
    await waitFor(() => expect(activeState!.source.mode).toBe('wysiwyg'))
    expect(activeState!.source.session).toBeGreaterThan(activeSession)
    expect(activeCommands!.applySourceChange(afterReadOnly)).toEqual({ applied: false, reason: 'source-inactive' })

    await user.click(screen.getByRole('button', { name: 'Source' }))
    const afterReentry = readRequest()
    ui.unmount()
    expect(activeCommands!.applySourceChange(afterReentry)).toEqual({ applied: false, reason: 'source-inactive' })
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
    await waitFor(() => expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy())
    const editor = getMarkdownEditor(activeEditor)!
    expect(container.querySelector('.ProseMirror > p > [data-markdown-image-frame]')).toBeInTheDocument()
    expect(editor.getMarkdown()).toBe(`![Only image](${target})`)

    await act(async () => {
      editor.commands.focus('end')
      editor.commands.insertContent('Continue below')
    })
    expect(editor.getMarkdown()).toContain(`![Only image](${target})`)
    expect(editor.getMarkdown()).toContain('Continue below')
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
          {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
        </MarkdownEditingSurface>
      )
    }

    const { container } = render(<ImageSurface />)
    await waitFor(() => {
      expect(getMarkdownEditor(activeEditor)?.view).toBeTruthy()
      expect(container.querySelectorAll('[data-markdown-image-controls="true"]')).toHaveLength(3)
    })

    const editor = getMarkdownEditor(activeEditor)!
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
