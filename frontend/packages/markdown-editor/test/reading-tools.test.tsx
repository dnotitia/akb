import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { useEffect, useState } from 'react'
import { afterEach, expect, it } from 'vitest'
import { MarkdownSurface, useMarkdownEditor } from '../src/react/index.js'
import { MarkdownViewerReadingTools, type MarkdownViewerReadingOptions } from '../src/react/markdown-reading-tools.js'
import { getMarkdownEditor } from '../src/react/editor-handle.js'
import { serializeEditorMarkdown } from '../src/core.js'
import type { MarkdownEditorHandle } from '../src/types.js'

afterEach(cleanup)

it('keeps section controls attached across heading presentation changes without treating prose clicks as folding', async () => {
  const user = userEvent.setup()
  const source = '# Heading\n\n- A bullet remains visible.\n- Another item.\n\n## Child\n\nChild body.'
  let handle: MarkdownEditorHandle | null = null
  function Harness({ offset, reading = true }: { offset: number; reading?: boolean }) {
    const current = useMarkdownEditor({ initialMarkdown: source, editable: false, slash: false })
    const [collapsed, setCollapsed] = useState(false)
    useEffect(() => { handle = current }, [current])
    return <><MarkdownSurface editor={current} editable={false} contentAttributes={{ role: 'document', 'aria-label': 'Reader body' }} headings={{ levelOffset: offset, ids: ['heading', 'child'] }} />
      <MarkdownViewerReadingTools editor={getMarkdownEditor(current)} options={reading ? {
        collapsedSections: collapsed ? [0] : [],
        renderSectionToggle: section => <button aria-expanded={!section.collapsed} onClick={() => setCollapsed(value => !value)}>Toggle {section.title}</button>,
      } : undefined} /></>
  }
  const { container, rerender } = render(<Harness offset={1} />)
  const toggle = await screen.findByRole('button', { name: 'Toggle Heading' })
  expect(screen.getByRole('document', { name: 'Reader body' })).toContainElement(toggle)
  expect(screen.getByRole('heading', { name: 'Heading' }).tagName).toBe('H2')
  expect(container.querySelector('#heading')).toContainElement(toggle)
  await user.click(screen.getByText('A bullet remains visible.'))
  expect(screen.getByText('Another item.')).toBeVisible()
  expect(toggle).toHaveAttribute('aria-expanded', 'true')
  await user.click(toggle)
  expect(screen.getByText('Another item.')).not.toBeVisible()
  rerender(<Harness offset={2} />)
  await waitFor(() => expect(screen.getByRole('heading', { name: 'Heading' }).tagName).toBe('H3'))
  const moved = screen.getByRole('button', { name: 'Toggle Heading' })
  expect(container.querySelector('#heading')).toContainElement(moved)
  await user.click(moved)
  expect(screen.getByText('Another item.')).toBeVisible()
  expect(serializeEditorMarkdown(getMarkdownEditor(handle)!)).toBe(source)
  rerender(<Harness offset={0} reading={false} />)
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Toggle Heading' })).not.toBeInTheDocument())
  expect(container.querySelector('#heading')?.tagName).toBe('H1')
  expect(screen.getByText('Child body.')).toBeVisible()
  expect(screen.getByRole('document', { name: 'Reader body' })).toBeVisible()
  expect(serializeEditorMarkdown(getMarkdownEditor(handle)!)).toBe(source)
})

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

it('keeps image controls outside the prose and its serialized text', async () => {
  const source = 'Before ![Diagram](https://example.invalid/diagram.png) after.'
  let handle: MarkdownEditorHandle | null = null
  function Harness() {
    const current = useMarkdownEditor({ initialMarkdown: source, editable: false, slash: false })
    useEffect(() => { handle = current }, [current])
    return <><MarkdownSurface editor={current} editable={false} /><MarkdownViewerReadingTools editor={getMarkdownEditor(current)} options={{ renderImageToolbar: () => <button type="button">Image controls</button> }} /></>
  }
  const { container } = render(<Harness />)
  await screen.findByRole('button', { name: 'Image controls' })
  expect(container.querySelector('.ProseMirror')?.textContent).not.toContain('Image controls')
  expect(serializeEditorMarkdown(getMarkdownEditor(handle)!)).not.toContain('Image controls')
  expect(serializeEditorMarkdown(getMarkdownEditor(handle)!)).toContain('![Diagram]')
})
