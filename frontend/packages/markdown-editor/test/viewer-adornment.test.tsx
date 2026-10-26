import { cleanup, render, waitFor, within } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MarkdownViewer } from '../src/index.js'

describe('MarkdownViewer read-only adornment', () => {
  afterEach(cleanup)

  it('places interactive metadata after a leading heading while preserving references and duplicate anchors', async () => {
    const click = vi.fn()
    const markdown = '# Title\n\n[Read the guide][guide]\n\n# Title\n\n[guide]: https://example.com/guide'
    const { container } = render(<MarkdownViewer markdown={markdown}
      headings={{ levelOffset: 1, ids: ['title', 'title-2'] }}
      afterLeadingHeading={<><h2 id="metadata-title">Metadata</h2><a href="https://example.com/metadata">Metadata link</a><button onClick={click}>Pause summary</button></>} />)
    const control = await within(container).findByRole('button', { name: 'Pause summary' })
    const first = container.querySelector('#title')
    expect(first).toHaveAttribute('id', 'title')
    const host = first?.nextElementSibling
    expect(host).toHaveAttribute('data-markdown-leading-adornment', 'true')
    expect(host).toHaveAttribute('contenteditable', 'false')
    expect(host).toContainElement(control)
    expect(within(container).getByRole('heading', { name: 'Metadata' })).toHaveAttribute('id', 'metadata-title')
    expect(within(container).getByRole('heading', { name: 'Metadata' }).tagName).toBe('H2')
    expect(container.querySelector('#title-2')).toHaveTextContent('Title')
    expect(within(container).getByRole('link', { name: 'Metadata link' })).toHaveAttribute('href', 'https://example.com/metadata')
    expect(within(container).getByRole('link', { name: 'Read the guide' })).toHaveAttribute('href', 'https://example.com/guide')
    await userEvent.setup().click(control)
    expect(click).toHaveBeenCalledOnce()
  })

  it('moves presentation with changed content and removes it without altering the body', async () => {
    const { container, rerender, unmount } = render(<MarkdownViewer markdown={'Title\n===\n\nOriginal body'}
      afterLeadingHeading={<p>First summary</p>} />)
    const first = await within(container).findByText('First summary')
    expect(first.parentElement?.previousElementSibling?.tagName).toBe('H1')
    rerender(<MarkdownViewer markdown={'A body without a heading.\n\nSecond paragraph.'}
      afterLeadingHeading={<p>Next summary</p>} />)
    await waitFor(() => expect(container.querySelector('.ProseMirror')?.firstElementChild).toHaveAttribute('data-markdown-leading-adornment', 'true'))
    expect(within(container).queryByText('First summary')).not.toBeInTheDocument()
    expect(within(container).getByText('Next summary')).toBeVisible()
    expect(within(container).getByText('A body without a heading.')).toBeVisible()
    expect(container.querySelectorAll('[data-markdown-leading-adornment]')).toHaveLength(1)
    rerender(<MarkdownViewer markdown={'A body without a heading.\n\nSecond paragraph.'} />)
    await waitFor(() => expect(container.querySelector('[data-markdown-leading-adornment]')).not.toBeInTheDocument())
    expect(within(container).getByText('Second paragraph.')).toBeVisible()
    unmount()
    expect(container).toBeEmptyDOMElement()
  })
})
