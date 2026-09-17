import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  EditorContent,
  MarkdownEditingSurface,
  MarkdownToolbar,
  useMarkdownEditor,
} from '../src/react/index.js'
import type { MarkdownAsset, MarkdownUploadAdapter } from '../src/types.js'

afterEach(() => cleanup())

function UploadSurface({
  adapter,
  initialMarkdown = 'One two',
  readOnly = false,
  onEditor,
}: {
  adapter: MarkdownUploadAdapter
  initialMarkdown?: string
  readOnly?: boolean
  onEditor?: (editor: ReturnType<typeof useMarkdownEditor>) => void
}) {
  const [markdown, setMarkdown] = useState(initialMarkdown)
  const editor = useMarkdownEditor({
    initialMarkdown,
    editable: !readOnly,
    onChange: next => setMarkdown(next),
  })

  useEffect(() => onEditor?.(editor), [editor, onEditor])

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
      {editor ? <EditorContent editor={editor} /> : null}
    </MarkdownEditingSurface>
  )
}

describe('shared image upload surface', () => {
  it('keeps the original insertion anchor through later edits', async () => {
    let resolveUpload!: (asset: MarkdownAsset) => void
    const adapter: MarkdownUploadAdapter = {
      upload: vi.fn(() => new Promise<MarkdownAsset>(resolve => { resolveUpload = resolve })),
    }
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null
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
    let activeEditor: ReturnType<typeof useMarkdownEditor> = null
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
    expect(screen.getByRole('img', { name: 'one' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(adapter.upload).toHaveBeenCalledTimes(3))
    expect(adapter.upload).toHaveBeenLastCalledWith(second, expect.anything())
    expect(await screen.findByRole('img', { name: 'two' })).toBeVisible()
    expect(screen.getAllByRole('img')).toHaveLength(2)
  })

  it('does not expose the upload surface in read-only mode', async () => {
    const adapter: MarkdownUploadAdapter = { upload: vi.fn() }
    render(<UploadSurface adapter={adapter} readOnly />)
    await waitFor(() => expect(screen.getByRole('toolbar', { name: 'Text formatting' })).toBeInTheDocument())
    expect(document.querySelector('input[type="file"]')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Insert image' })).toBeNull()
  })
})
