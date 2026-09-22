import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Editor } from '@tiptap/core'
import { EditorContent } from '@tiptap/react'

import {
  MarkdownEditingSurface,
  MarkdownToolbar,
  useMarkdownEditor,
} from '../src/react/index.js'
import { getMarkdownEditor } from '../src/react/editor-handle.js'
import type { MarkdownAsset, MarkdownUploadAdapter } from '../src/types.js'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function UploadSurface({
  adapter,
  initialMarkdown = 'One two',
  readOnly = false,
  onEditor,
}: {
  adapter: MarkdownUploadAdapter
  initialMarkdown?: string
  readOnly?: boolean
  onEditor?: (editor: Editor | null) => void
}) {
  const [markdown, setMarkdown] = useState(initialMarkdown)
  const editor = useMarkdownEditor({
    initialMarkdown,
    editable: !readOnly,
    onChange: next => setMarkdown(next),
  })

  useEffect(() => onEditor?.(getMarkdownEditor(editor)), [editor, onEditor])

  return (
    <MarkdownEditingSurface
      editor={editor}
      markdown={markdown}
      readOnly={readOnly}
      imageMenu={{}}
      imageUpload={{ adapter }}
      toolbar={<MarkdownToolbar editor={editor} />}
      onSourceChange={next => setMarkdown(next)}
    >
      {editor ? <EditorContent editor={getMarkdownEditor(editor)} /> : null}
    </MarkdownEditingSurface>
  )
}

describe('shared image upload surface', () => {
  it('keeps the original insertion anchor through later edits', async () => {
    let resolveUpload!: (asset: MarkdownAsset) => void
    const adapter: MarkdownUploadAdapter = {
      upload: vi.fn(() => new Promise<MarkdownAsset>(resolve => { resolveUpload = resolve })),
    }
    let activeEditor: Editor | null = null
    render(<UploadSurface adapter={adapter} onEditor={editor => { activeEditor = editor }} />)

    await waitFor(() => expect(activeEditor?.view).toBeTruthy())
    const editor = activeEditor!
    editor.commands.setTextSelection({ from: 4, to: 4 })
    const file = new File(['image'], 'anchor.png', { type: 'image/png' })
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [file] },
    })
    await waitFor(() => expect(adapter.upload).toHaveBeenCalled())

    act(() => {
      editor.commands.setTextSelection({ from: 1, to: 1 })
      editor.commands.insertContent('prefix ')
    })
    resolveUpload({ kind: 'attachment', target: '/api/assets/anchor', alt: 'anchor' })

    await waitFor(() => expect(editor.getMarkdown()).toContain('/api/assets/anchor'))
    const markdown = editor.getMarkdown()
    expect(markdown.indexOf('One')).toBeLessThan(markdown.indexOf('/api/assets/anchor'))
    expect(markdown.indexOf('/api/assets/anchor')).toBeLessThan(markdown.indexOf('two'))
  })

  it('replaces the first requested image occurrence after document edits', async () => {
    let resolveUpload!: (asset: MarkdownAsset) => void
    const adapter: MarkdownUploadAdapter = {
      upload: vi.fn(() => new Promise<MarkdownAsset>(resolve => { resolveUpload = resolve })),
    }
    const oldTarget = '/api/assets/old'
    let activeEditor: Editor | null = null
    render(
      <UploadSurface
        adapter={adapter}
        initialMarkdown={`![first](${oldTarget})\n\n![second](${oldTarget})`}
        onEditor={editor => { activeEditor = editor }}
      />,
    )

    await waitFor(() => expect(activeEditor?.view).toBeTruthy())
    const replace = await screen.findByRole('button', { name: 'Replace image: first' })
    fireEvent.click(replace)
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
    expect(input).toHaveProperty('multiple', false)
    const file = new File(['replacement'], 'replacement.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [file] } })
    await waitFor(() => expect(adapter.upload).toHaveBeenCalled())

    act(() => {
      activeEditor!.commands.setTextSelection({ from: 1, to: 1 })
      activeEditor!.commands.insertContent('prefix ')
    })
    resolveUpload({ kind: 'attachment', target: '/api/assets/new', alt: 'replacement' })

    await waitFor(() => expect(activeEditor!.getMarkdown()).toContain('/api/assets/new'))
    const markdown = activeEditor!.getMarkdown()
    expect(markdown).toContain(`![replacement](/api/assets/new)`)
    expect(markdown).toContain(`![second](${oldTarget})`)
    expect(markdown).not.toContain(`![first](${oldTarget})`)
  })

  it('keeps partial results and retries only retryable failed files', async () => {
    const first = new File(['one'], 'one.png', { type: 'image/png' })
    const second = new File(['two'], 'two.png', { type: 'image/png' })
    const adapter: MarkdownUploadAdapter = {
      upload: vi.fn()
        .mockResolvedValueOnce({ kind: 'attachment', target: '/api/assets/one', alt: 'one' })
        .mockRejectedValueOnce(Object.assign(new Error('busy'), { code: 'unavailable', retryable: true }))
        .mockResolvedValueOnce({ kind: 'attachment', target: '/api/assets/two', alt: 'two' }),
    }
    render(<UploadSurface adapter={adapter} />)
    const input = document.querySelector('input[type="file"]')!
    fireEvent.click(screen.getByRole('button', { name: 'Insert image' }))
    expect(input).toHaveProperty('multiple', true)
    fireEvent.change(input, { target: { files: [first, second] } })

    expect(await screen.findByText('Image upload failed')).toBeVisible()
    const alert = screen.getByRole('alert')
    expect(alert).toHaveAttribute('data-markdown-image-upload-state', 'error')
    expect(alert).toHaveClass('border-destructive/30', 'bg-destructive-soft', 'text-destructive-soft-foreground')
    expect(alert.querySelector('[data-markdown-image-upload-error-icon]')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toHaveClass('focus-ring-instant')
    expect(screen.getByRole('button', { name: 'Dismiss' })).toHaveClass('focus-ring-instant')
    expect(screen.getByRole('img', { name: 'one' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(adapter.upload).toHaveBeenCalledTimes(3))
    expect(adapter.upload).toHaveBeenLastCalledWith(second, expect.anything())
    expect(await screen.findByRole('img', { name: 'two' })).toBeVisible()
    expect(screen.getAllByRole('img')).toHaveLength(2)
  })

  it('keeps queued recovery neutral instead of presenting it as an error', async () => {
    let resolveFirst!: (asset: MarkdownAsset) => void
    const first = new File(['one'], 'one.png', { type: 'image/png' })
    const second = new File(['two'], 'two.png', { type: 'image/png' })
    const adapter: MarkdownUploadAdapter = {
      upload: vi.fn()
        .mockImplementationOnce(() => new Promise<MarkdownAsset>(resolve => { resolveFirst = resolve }))
        .mockResolvedValueOnce({ kind: 'attachment', target: '/api/assets/two', alt: 'two' }),
    }
    render(<UploadSurface adapter={adapter} />)
    const input = document.querySelector('input[type="file"]')!
    fireEvent.click(screen.getByRole('button', { name: 'Insert image' }))
    fireEvent.change(input, { target: { files: [first] } })
    await waitFor(() => expect(adapter.upload).toHaveBeenCalledTimes(1))
    fireEvent.change(input, { target: { files: [second] } })

    act(() => resolveFirst({ kind: 'attachment', target: '/api/assets/one', alt: 'one' }))

    await waitFor(() => expect(document.querySelector('[data-markdown-image-upload-state="queued"]')).toBeInTheDocument())
    const status = document.querySelector<HTMLElement>('[data-markdown-image-upload-state="queued"]')!
    expect(status).toHaveAttribute('data-markdown-image-upload-state', 'queued')
    expect(status).not.toHaveClass('border-destructive/30', 'bg-destructive-soft')
    expect(status.querySelector('[data-markdown-image-upload-error-icon]')).toBeNull()
  })

  it('repositions image controls when a failure banner changes the editor layout', async () => {
    const resizeObservers: Array<{ trigger: () => void }> = []
    class TestResizeObserver {
      private readonly callback: ResizeObserverCallback

      constructor(callback: ResizeObserverCallback) {
        this.callback = callback
        resizeObservers.push({
          trigger: () => this.callback([], this as unknown as ResizeObserver),
        })
      }

      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', TestResizeObserver)

    const adapter: MarkdownUploadAdapter = {
      upload: vi.fn().mockRejectedValue(
        Object.assign(new Error('server busy'), { code: 'unavailable', retryable: true }),
      ),
    }
    const { container } = render(
      <UploadSurface
        adapter={adapter}
        initialMarkdown="![Existing](https://example.com/existing.png)"
      />,
    )
    await screen.findByRole('button', { name: 'Edit image description: Existing' })

    const surface = container.querySelector<HTMLElement>('[data-markdown-mode="wysiwyg"]')!
    const image = container.querySelector<HTMLImageElement>('img[data-markdown-target]')!
    let imageTop = 300
    Object.defineProperty(surface, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ top: 100, left: 0, right: 500, bottom: 800, width: 500, height: 700 }),
    })
    Object.defineProperty(image, 'getBoundingClientRect', {
      configurable: true,
      value: () => ({ top: imageTop, left: 40, right: 120, bottom: imageTop + 1, width: 80, height: 1 }),
    })

    await act(async () => resizeObservers.at(-1)?.trigger())
    const controls = container.querySelector<HTMLElement>('[data-markdown-image-controls="true"]')!
    expect(controls).toHaveStyle({ top: '204px' })

    const input = document.querySelector('input[type="file"]')!
    fireEvent.click(screen.getByRole('button', { name: 'Insert image' }))
    fireEvent.change(input, {
      target: { files: [new File(['failed'], 'failed.png', { type: 'image/png' })] },
    })
    await screen.findByRole('alert')

    imageTop = 420
    await act(async () => resizeObservers.at(-1)?.trigger())
    expect(controls).toHaveStyle({ top: '324px' })
  })

  it('does not expose the upload surface in read-only mode', async () => {
    const adapter: MarkdownUploadAdapter = { upload: vi.fn() }
    render(<UploadSurface adapter={adapter} readOnly />)
    await waitFor(() => expect(screen.getByRole('toolbar', { name: 'Text formatting' })).toBeInTheDocument())
    expect(document.querySelector('input[type="file"]')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Insert image' })).toBeNull()
  })
})
