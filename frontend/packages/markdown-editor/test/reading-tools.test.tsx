import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, expect, it } from 'vitest'
import { MarkdownSurface, useMarkdownEditor } from '../src/react/index.js'
import { MarkdownViewerReadingTools, type MarkdownViewerReadingOptions } from '../src/react/markdown-reading-tools.js'
import { getMarkdownEditor } from '../src/react/editor-handle.js'
import { serializeEditorMarkdown } from '../src/core.js'
import type { MarkdownEditorHandle } from '../src/types.js'

afterEach(cleanup)

it('keeps widgets, folds and highlights out of authored Markdown and restores the plain viewer', async () => {
  const source = '# Heading\n\nA **literal** needle.\n\n```ts\nconst needle = 1;\n```'
  let handle: MarkdownEditorHandle | null = null
  function Harness({ reading }: { reading?: MarkdownViewerReadingOptions }) {
    const current = useMarkdownEditor({ initialMarkdown: source, editable: false, slash: false })
    useEffect(() => { handle = current }, [current])
    return <><MarkdownViewerReadingTools editor={getMarkdownEditor(current)} options={reading} /><MarkdownSurface editor={current} editable={false} /></>
  }
  const reading: MarkdownViewerReadingOptions = {
    collapseCodeBlocks: true,
    renderCodeToolbar: block => <button type="button">Needle toolbar {block.language}</button>,
    search: { query: 'needle', current: 0 },
  }
  const { container, rerender } = render(<Harness reading={reading} />)
  await screen.findByRole('button', { name: 'Needle toolbar ts' })
  await waitFor(() => expect(container.querySelectorAll('.markdown-reading-match')).toHaveLength(2))
  expect(container.querySelector('pre[data-reading-folded=true]')).toBeTruthy()
  expect(serializeEditorMarkdown(getMarkdownEditor(handle)!)).toBe(source)
  rerender(<Harness />)
  await waitFor(() => expect(container.querySelectorAll('.markdown-reading-match')).toHaveLength(0))
  expect(container.querySelector('[data-reading-folded]')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Needle toolbar ts' })).toBeNull()
  expect(serializeEditorMarkdown(getMarkdownEditor(handle)!)).toBe(source)
})
