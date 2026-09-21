import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Editor } from '@tiptap/core'
import { EditorContent } from '@tiptap/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createMarkdownExtensions } from '../src/extensions.js'
import {
  createMarkdownReferenceExtension,
  DEFAULT_MARKDOWN_REFERENCE_LABELS,
  createLiveMarkdownReferenceExtension,
} from '../src/react/markdown-reference-menu.js'
import type {
  MarkdownReferenceAdapter,
  MarkdownReferenceCandidate,
} from '../src/types.js'

const editors: Editor[] = []

const candidates: readonly MarkdownReferenceCandidate[] = [
  { id: 'ada', kind: 'person', title: 'Ada Lovelace', value: '@ada' },
  { id: 'AKB-326', kind: 'issue', title: 'AKB-326', value: 'AKB-326' },
  {
    id: 'guide',
    kind: 'document',
    title: 'Guide',
    target: 'akb://team/doc/guide.md',
  },
  {
    id: 'readme',
    kind: 'file',
    title: 'README',
    target: 'akb://team/file/README.md',
  },
]

function dispatchKey(editor: Editor, key: string): boolean {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
  return editor.view.someProp('handleKeyDown', handler => handler(editor.view, event)) ?? false
}

function mountEditor(
  adapter: MarkdownReferenceAdapter,
  markdown = '',
  options: { context?: { vault?: string; document?: string; commit?: string } } = {},
): Editor {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const editor = new Editor({
    element: host,
    extensions: [
      ...createMarkdownExtensions(),
      createMarkdownReferenceExtension({
        adapter,
        context: options.context,
        labels: DEFAULT_MARKDOWN_REFERENCE_LABELS,
      }),
    ],
    content: markdown,
    contentType: 'markdown',
  })
  editors.push(editor)
  render(<EditorContent editor={editor} />)
  return editor
}

afterEach(() => {
  while (editors.length > 0) editors.pop()?.destroy()
  document.body.innerHTML = ''
})

describe('markdown @ reference menu', () => {
  it('groups the common kinds and inserts plain or canonical Markdown references', async () => {
    const adapter: MarkdownReferenceAdapter = {
      search: vi.fn(async () => candidates),
    }
    const editor = mountEditor(adapter)

    await act(async () => editor.commands.insertContent('@'))
    const menu = await screen.findByTestId('markdown-reference-menu')
    await waitFor(() => expect(menu.querySelectorAll('[role="option"]')).toHaveLength(4))

    expect(menu.querySelectorAll('[data-reference-section]')).toHaveLength(4)
    expect(menu.querySelector('[data-reference-section="person"]')).toHaveTextContent('Ada Lovelace')
    expect(menu.querySelector('[data-reference-section="issue"]')).toHaveTextContent('AKB-326')

    const documentOption = menu.querySelector<HTMLButtonElement>('[data-reference-kind="document"]')!
    fireEvent.mouseDown(documentOption)
    fireEvent.click(documentOption)
    await waitFor(() => expect(screen.queryByTestId('markdown-reference-menu')).not.toBeInTheDocument())
    expect(editor.getMarkdown()).toContain('[Guide](akb://team/doc/guide.md)')

    await act(async () => editor.commands.insertContent('continue'))
    expect(editor.getMarkdown()).toContain('continue')
  })

  it('keeps long labels inspectable when the visual label is constrained', async () => {
    const title = 'akb_reference_adapter_release_checklist_2026_09_final_review_notes.md'
    const editor = mountEditor({
      search: vi.fn(async () => [
        { id: 'long-file', kind: 'file' as const, title, target: 'akb://team/file/long-file.md' },
      ]),
    })

    await act(async () => editor.commands.insertContent('@'))
    const option = await screen.findByRole('option')
    const label = option.querySelector('.markdown-reference-label')

    expect(option).toHaveAttribute('aria-label', title)
    expect(label).toHaveAttribute('title', title)
  })

  it('uses the same candidate for pointer and keyboard selection and preserves undo/redo', async () => {
    const adapter: MarkdownReferenceAdapter = {
      search: vi.fn(async () => candidates),
    }
    const pointer = mountEditor(adapter)
    await act(async () => pointer.commands.insertContent('@'))
    const pointerMenu = await screen.findByTestId('markdown-reference-menu')
    const issue = pointerMenu.querySelector<HTMLButtonElement>('[data-reference-kind="issue"]')!
    fireEvent.pointerMove(issue)
    fireEvent.mouseDown(issue)
    fireEvent.click(issue)
    await waitFor(() => expect(pointer.getMarkdown()).toContain('AKB-326'))

    const keyboard = mountEditor(adapter)
    await act(async () => keyboard.commands.insertContent('@'))
    await screen.findByTestId('markdown-reference-menu')
    dispatchKey(keyboard, 'ArrowDown')
    dispatchKey(keyboard, 'Enter')
    await waitFor(() => expect(keyboard.getMarkdown()).toContain('AKB-326'))
    expect(keyboard.getMarkdown()).not.toContain('@')

    await act(async () => keyboard.commands.undo())
    expect(keyboard.getMarkdown()).toBe('')
    await act(async () => keyboard.commands.redo())
    expect(keyboard.getMarkdown()).toContain('AKB-326')
  })

  it('inserts person and issue values as literal text nodes', async () => {
    const literalValue = '<strong>@ada</strong>'
    const editor = mountEditor({
      search: vi.fn(async (): Promise<readonly MarkdownReferenceCandidate[]> => [
        { id: 'ada', kind: 'person', title: 'Ada Lovelace', value: literalValue },
      ]),
    })

    await act(async () => editor.commands.insertContent('@'))
    const menu = await screen.findByTestId('markdown-reference-menu')
    await waitFor(() => expect(menu.querySelectorAll('[role="option"]')).toHaveLength(1))
    fireEvent.click(menu.querySelector('[data-reference-kind="person"]')!)

    await waitFor(() => expect(JSON.stringify(editor.getJSON())).toContain(`"text":"${literalValue} "`))
    expect(editor.getMarkdown()).toContain('&lt;strong&gt;@ada&lt;/strong&gt;')
  })

  it('keeps loading, empty, and error states distinct without an invalid selection', async () => {
    let resolve: ((value: readonly MarkdownReferenceCandidate[]) => void) | undefined
    const pending: MarkdownReferenceAdapter = {
      search: vi.fn(
        () =>
          new Promise<readonly MarkdownReferenceCandidate[]>(next => {
            resolve = next
          }),
      ),
    }
    const editor = mountEditor(pending)
    await act(async () => editor.commands.insertContent('@'))
    await screen.findByTestId('markdown-reference-loading')
    resolve?.([])
    await screen.findByTestId('markdown-reference-empty')
    dispatchKey(editor, 'Enter')
    expect(editor.getMarkdown()).toContain('@')

    const error = mountEditor({
      search: vi.fn(async () => {
        throw new Error('search failed')
      }),
    })
    await act(async () => error.commands.insertContent('@'))
    await screen.findByTestId('markdown-reference-error')
    expect(error.getMarkdown()).toContain('@')
  })

  it('discards reversed responses and late cancellation results', async () => {
    const deferred = new Map<string, {
      resolve: (value: readonly MarkdownReferenceCandidate[]) => void
    }>()
    const adapter: MarkdownReferenceAdapter = {
      search: vi.fn(query =>
        new Promise<readonly MarkdownReferenceCandidate[]>(resolve => {
          deferred.set(query, { resolve })
        }),
      ),
    }
    const editor = mountEditor(adapter, '', { context: { vault: 'first' } })

    await act(async () => editor.commands.insertContent('@'))
    await screen.findByTestId('markdown-reference-loading')
    await act(async () => editor.commands.insertContent('a'))
    expect(deferred.has('')).toBe(true)
    expect(deferred.has('a')).toBe(true)

    deferred.get('')?.resolve([
      { id: 'stale', kind: 'document', title: 'Stale', target: 'akb://team/doc/stale.md' },
    ])
    deferred.get('a')?.resolve([
      { id: 'fresh', kind: 'file', title: 'Fresh', target: 'akb://team/file/fresh.md' },
    ])
    await waitFor(() => expect(screen.getByTestId('markdown-reference-menu')).toHaveTextContent('Fresh'))
    expect(screen.getByTestId('markdown-reference-menu')).not.toHaveTextContent('Stale')

    fireEvent.keyDown(editor.view.dom, { key: 'Escape' })
    deferred.get('a')?.resolve([
      { id: 'late', kind: 'file', title: 'Late', target: 'akb://team/file/late.md' },
    ])
    await waitFor(() => expect(screen.queryByTestId('markdown-reference-menu')).not.toBeInTheDocument())
    expect(editor.getMarkdown()).toContain('@a')
  })

  it('closes the menu and invalidates old results when live context changes', async () => {
    const deferred: { resolve: (value: readonly MarkdownReferenceCandidate[]) => void }[] = []
    const adapter: MarkdownReferenceAdapter = {
      search: vi.fn(
        () => new Promise<readonly MarkdownReferenceCandidate[]>(resolve => deferred.push({ resolve })),
      ),
    }
    let currentOptions = {
      adapter,
      context: { vault: 'first' },
      labels: DEFAULT_MARKDOWN_REFERENCE_LABELS,
    }
    const listeners = new Set<() => void>()
    const host = document.createElement('div')
    document.body.appendChild(host)
    const editor = new Editor({
      element: host,
      extensions: [
        ...createMarkdownExtensions(),
        createLiveMarkdownReferenceExtension(
          () => currentOptions,
          listener => {
            listeners.add(listener)
            return () => listeners.delete(listener)
          },
        ),
      ],
      contentType: 'markdown',
    })
    editors.push(editor)
    render(<EditorContent editor={editor} />)

    await act(async () => editor.commands.insertContent('@'))
    await screen.findByTestId('markdown-reference-loading')
    expect(deferred).toHaveLength(1)

    currentOptions = {
      ...currentOptions,
      context: { vault: 'second' },
    }
    for (const listener of listeners) listener()
    await waitFor(() => expect(screen.queryByTestId('markdown-reference-menu')).not.toBeInTheDocument())

    deferred[0]?.resolve([
      { id: 'old', kind: 'document', title: 'Old vault', target: 'akb://first/doc/old.md' },
    ])
    await waitFor(() => expect(editor.getMarkdown()).toBe('@'))
    expect(screen.queryByText('Old vault')).not.toBeInTheDocument()
  })

  it('rejects inline, code, link, read-only, and IME triggers and consumes Escape locally', async () => {
    const adapter: MarkdownReferenceAdapter = {
      search: vi.fn(async () => candidates),
    }
    const inline = mountEditor(adapter, 'email@example')
    await act(async () => inline.commands.setTextSelection(inline.state.doc.content.size))
    await act(async () => inline.commands.insertContent('@'))
    expect(screen.queryByTestId('markdown-reference-menu')).not.toBeInTheDocument()

    const code = mountEditor(adapter)
    await act(async () => {
      code.commands.setCodeBlock()
      code.commands.insertContent('@')
    })
    expect(screen.queryByTestId('markdown-reference-menu')).not.toBeInTheDocument()

    const linked = mountEditor(adapter, '[linked](akb://team/doc/linked.md)')
    await act(async () => linked.commands.setTextSelection({ from: 5, to: 5 }))
    await act(async () => linked.commands.insertContent('@'))
    expect(screen.queryByTestId('markdown-reference-menu')).not.toBeInTheDocument()

    const readOnly = mountEditor(adapter)
    readOnly.setEditable(false)
    await act(async () => readOnly.commands.insertContent('@'))
    expect(screen.queryByTestId('markdown-reference-menu')).not.toBeInTheDocument()

    const composing = mountEditor(adapter)
    Object.defineProperty(composing.view, 'composing', { configurable: true, value: true })
    await act(async () => composing.commands.insertContent('@'))
    expect(screen.queryByTestId('markdown-reference-menu')).not.toBeInTheDocument()
    Object.defineProperty(composing.view, 'composing', { configurable: true, value: false })

    const editor = mountEditor(adapter)
    const host = editor.view.dom.parentElement!
    let bubbled = false
    host.addEventListener('keydown', () => {
      bubbled = true
    })
    await act(async () => editor.commands.insertContent('@'))
    await screen.findByTestId('markdown-reference-menu')
    fireEvent.keyDown(editor.view.dom, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('markdown-reference-menu')).not.toBeInTheDocument())
    expect(bubbled).toBe(false)
    expect(editor.getMarkdown()).toContain('@')
  })
})
