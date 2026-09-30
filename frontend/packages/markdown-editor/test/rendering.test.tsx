import { act, cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MarkdownEditor, MarkdownViewer } from '../src/index.js'
import type { MarkdownReferenceAdapter, MarkdownTargetResolution } from '../src/index.js'

const MIXED_MARKDOWN = [
  '# Heading',
  '',
  'Paragraph with a **strong** word.',
  '',
  '> A quoted paragraph.',
  '',
  '- ordinary item',
  '  - nested item',
  '',
  '- [ ] Parent task',
  '  - [x] Child task',
  '',
  '```typescript',
  'const answer: number = 42',
  '```',
].join('\n')

describe('Markdown block rendering', () => {
  afterEach(cleanup)

  it('shares headings, lists, quotes, checklists, and highlighted code between editor and viewer', async () => {
    const { container } = render(
      <>
        <MarkdownEditor markdown={MIXED_MARKDOWN} />
        <MarkdownViewer markdown={MIXED_MARKDOWN} />
      </>,
    )

    await waitFor(() => expect(container.querySelectorAll('.ProseMirror')).toHaveLength(2))

    const surfaces = container.querySelectorAll<HTMLElement>('.ProseMirror')
    await within(container).findAllByRole('checkbox')
    for (const surface of surfaces) {
      expect(surface.querySelector('h1')).toHaveTextContent('Heading')
      expect(surface.querySelector('blockquote')).toHaveTextContent('A quoted paragraph.')
      expect(surface.querySelectorAll('[data-markdown-task-item]')).toHaveLength(2)
      await waitFor(() =>
        expect(surface.querySelector('pre')).toHaveAttribute('data-markdown-code', 'true'),
      )
    }

    const code = surfaces[0]?.querySelector('pre')
    expect(code).toHaveAttribute('data-markdown-code', 'true')
    expect(code).toHaveAttribute('role', 'region')
    expect(code).toHaveAttribute('tabindex', '0')
    expect(code).toHaveAttribute('aria-label', 'Scrollable typescript code block')
    expect(code?.querySelector('.hljs-keyword')).toHaveTextContent('const')
  })

  it('applies the public content and read-only layout contract', async () => {
    const markdown = [
      '# Heading',
      '',
      '| Name | Value |',
      '| --- | --- |',
      '| A | B |',
      '',
      '![Diagram](https://example.com/diagram.png)',
    ].join('\n')
    const { container } = render(
      <MarkdownViewer
        markdown={markdown}
        contentClassName="viewer-content"
        contentAttributes={{ 'aria-label': 'Document body' }}
        headings={{ levelOffset: 1, ids: ['heading'] }}
        image={{ referrerPolicy: 'no-referrer' }}
        tableLayout={{
          className: 'w-max',
          wrapperClassName: 'table-scroll',
        }}
      />,
    )

    const content = await within(container).findByRole('textbox', { name: 'Document body' })
    await waitFor(() => expect(content.querySelector('h2')).toHaveAttribute('id', 'heading'))
    expect(content).toHaveClass('viewer-content')
    expect(content.querySelector('h1')).not.toBeInTheDocument()
    expect(content.querySelector('table')).toHaveClass('w-max')
    expect(content.querySelector('[data-markdown-table-wrapper="true"]')).toHaveClass('table-scroll')
    expect(content.querySelector('[data-markdown-table-wrapper="true"]')).toHaveAttribute(
      'aria-label',
      'Scrollable table',
    )
    expect(content.querySelector('img')).toHaveAttribute('referrerpolicy', 'no-referrer')
  })

  it('removes presentation-only viewer decorations when options change', async () => {
    const markdown = [
      '# Heading',
      '',
      '| Name | Value |',
      '| --- | --- |',
      '| A | B |',
      '',
      '![Diagram](https://example.com/diagram.png)',
    ].join('\n')
    const { container, rerender } = render(
      <MarkdownViewer
        markdown={markdown}
        headings={{ levelOffset: 1, ids: ['heading'] }}
        image={{ referrerPolicy: 'no-referrer' }}
        tableLayout={{ className: 'w-max', wrapperClassName: 'table-scroll' }}
      />,
    )

    await waitFor(() => {
      expect(container.querySelector('h2')).toHaveAttribute('id', 'heading')
      expect(container.querySelector('[data-markdown-table-wrapper="true"]')).toBeInTheDocument()
    })
    rerender(<MarkdownViewer markdown={markdown} />)

    await waitFor(() => {
      expect(container.querySelector('h1')).toBeInTheDocument()
      expect(container.querySelector('h2')).not.toBeInTheDocument()
      expect(container.querySelector('[data-markdown-table-wrapper="true"]')).not.toBeInTheDocument()
      expect(container.querySelector('img')).not.toHaveAttribute('referrerpolicy')
    })
  })

  it('does not mutate editable ProseMirror DOM for viewer-only layout options', async () => {
    const markdown = [
      '# Heading',
      '',
      '| Name | Value |',
      '| --- | --- |',
      '| A | B |',
    ].join('\n')
    const { container } = render(
      <MarkdownEditor
        markdown={markdown}
        headings={{ levelOffset: 1, ids: ['heading'] }}
        tableLayout={{ className: 'w-max', wrapperClassName: 'table-scroll' }}
      />,
    )

    const content = await within(container).findByRole('textbox')
    await waitFor(() => expect(content.querySelector('h1')).toHaveTextContent('Heading'))
    expect(content.querySelector('h2')).not.toBeInTheDocument()
    expect(content.querySelector('[data-markdown-table-wrapper="true"]')).not.toBeInTheDocument()
    expect(content.querySelector('table')).not.toHaveClass('w-max')
  })

  it('exposes independent, keyboard-operable task checkboxes and preserves their Markdown state', async () => {
    const user = userEvent.setup()
    let saved = MIXED_MARKDOWN
    const onChange = vi.fn((markdown: string) => {
      saved = markdown
    })
    const { container, rerender } = render(
      <MarkdownEditor markdown={saved} onChange={onChange} />,
    )
    const view = within(container)

    await waitFor(() =>
      expect(container.querySelectorAll('[data-markdown-task-item] input[type="checkbox"]'))
        .toHaveLength(2),
    )
    const checkboxes = view.getAllByRole('checkbox')
    expect(checkboxes).toHaveLength(2)
    expect(checkboxes[0]).toHaveAccessibleName('Task item checkbox for Parent task')
    expect(checkboxes[1]).toHaveAccessibleName('Task item checkbox for Child task')

    checkboxes[0]?.focus()
    expect(checkboxes[0]).toHaveFocus()
    await user.keyboard(' ')

    await waitFor(() => expect(saved).toContain('- [x] Parent task'))
    expect(saved).toContain('- [x] Child task')

    rerender(<MarkdownEditor markdown={saved} onChange={onChange} />)
    await view.findByRole('checkbox', { name: /Parent task/i })
    expect(view.getAllByRole('checkbox')[0]).toBeChecked()
    expect(view.getAllByRole('checkbox')[1]).toBeChecked()
  })

  it('reaches embedded task and code controls through the editor Tab order', async () => {
    const { container } = render(<MarkdownEditor markdown={MIXED_MARKDOWN} />)
    const editor = await within(container).findByRole('textbox')
    const parent = within(editor).getByRole('checkbox', { name: 'Task item checkbox for Parent task' })
    const child = within(editor).getByRole('checkbox', { name: 'Task item checkbox for Child task' })
    const code = within(editor).getByRole('region', { name: 'Scrollable typescript code block' })

    editor.focus()
    fireEvent.keyDown(editor, { key: 'Tab', code: 'Tab', keyCode: 9 })
    fireEvent.keyUp(editor, { key: 'Tab', code: 'Tab', keyCode: 9 })
    expect(parent).toHaveFocus()
    fireEvent.keyDown(parent, { key: 'Tab', code: 'Tab', keyCode: 9 })
    fireEvent.keyUp(parent, { key: 'Tab', code: 'Tab', keyCode: 9 })
    expect(child).toHaveFocus()
    fireEvent.keyDown(child, { key: 'Tab', code: 'Tab', keyCode: 9 })
    fireEvent.keyUp(child, { key: 'Tab', code: 'Tab', keyCode: 9 })
    expect(code).toHaveFocus()
  })

  it('follows document order across links and embedded controls while skipping noninteractive references', async () => {
    const markdown = [
      '[General](https://example.com)',
      '',
      '[Document](akb://fixture/doc/guide.md)',
      '',
      'AKB-359 @person',
      '',
      '- [ ] Task',
      '',
      '```typescript',
      'const value = 1',
      '```',
    ].join('\n')
    const adapter: MarkdownReferenceAdapter = {
      search: async () => [],
      resolve: async reference => ({
        ...reference,
        status: 'available',
        title: reference.kind === 'person' ? 'Person display name' : 'Issue display title',
        ...(reference.kind === 'issue' ? { runtimeUrl: '/issues/AKB-359' } : {}),
      }),
    }
    const { container } = render(
      <>
        <MarkdownEditor markdown={markdown} reference={{ adapter }} />
        <button type="button" data-testid="after-editor">After editor</button>
      </>,
    )
    const editor = container.querySelector<HTMLElement>('.ProseMirror')!
    await waitFor(() => expect(
      editor.querySelector('a[role="link"][data-markdown-reference-runtime-url="/issues/AKB-359"]'),
    ).toBeInTheDocument())

    const links = [...editor.querySelectorAll<HTMLAnchorElement>(
      'a[href], a[role="link"][data-markdown-reference-runtime-url]',
    )]
    const person = editor.querySelector<HTMLElement>('[data-markdown-reference-kind="person"]')!
    const checkbox = editor.querySelector<HTMLInputElement>('input[type="checkbox"]')!
    const code = editor.querySelector<HTMLElement>('pre[data-markdown-code][tabindex="0"]')!
    expect(links.map(link => link.textContent?.trim())).toEqual([
      'General',
      'Document',
      'AKB-359',
    ])
    expect(links[2]).toHaveAttribute('data-markdown-reference-title', 'Issue display title')
    expect(links[2]).toHaveAttribute('aria-label', 'AKB-359 Issue display title')
    expect(links[0]).toHaveAttribute('tabindex', '0')
    expect(links[1]).toHaveAttribute('tabindex', '0')
    expect(person).not.toHaveAttribute('href')
    expect(person).not.toHaveAttribute('tabindex')

    const tab = () => {
      const target = document.activeElement as HTMLElement
      fireEvent.keyDown(target, { key: 'Tab', code: 'Tab', keyCode: 9 })
      fireEvent.keyUp(document.activeElement as HTMLElement, { key: 'Tab', code: 'Tab', keyCode: 9 })
    }

    editor.focus()
    tab()
    expect(links[0]).toHaveFocus()
    tab()
    expect(links[1]).toHaveFocus()
    tab()
    expect(links[2]).toHaveFocus()
    tab()
    expect(checkbox).toHaveFocus()
    tab()
    expect(code).toHaveFocus()

    const shiftTab = () => {
      const target = document.activeElement as HTMLElement
      fireEvent.keyDown(target, { key: 'Tab', code: 'Tab', keyCode: 9, shiftKey: true })
      fireEvent.keyUp(document.activeElement as HTMLElement, {
        key: 'Tab',
        code: 'Tab',
        keyCode: 9,
        shiftKey: true,
      })
    }
    shiftTab()
    expect(checkbox).toHaveFocus()
    shiftTab()
    expect(links[2]).toHaveFocus()
    shiftTab()
    expect(links[1]).toHaveFocus()
    shiftTab()
    expect(links[0]).toHaveFocus()
    shiftTab()
    expect(editor).toHaveFocus()

    const finalTab = new KeyboardEvent('keydown', {
      key: 'Tab',
      code: 'Tab',
      keyCode: 9,
      bubbles: true,
      cancelable: true,
    })
    code.dispatchEvent(finalTab)
    expect(finalTab.defaultPrevented).toBe(false)
  })

  it('makes a resolved link tabbable after its asynchronous target resolution', async () => {
    const target = 'akb://fixture/doc/guide.md'
    let resolveTarget!: (resolution: MarkdownTargetResolution) => void
    const targetResolver = {
      resolve: vi.fn(() => new Promise<MarkdownTargetResolution>(resolve => {
        resolveTarget = resolve
      })),
    }
    const { container } = render(
      <MarkdownEditor
        markdown={'[Document](akb://fixture/doc/guide.md)'}
        adapters={{ targetResolver }}
      />,
    )
    const editor = container.querySelector<HTMLElement>('.ProseMirror')!
    const link = editor.querySelector<HTMLAnchorElement>('a[data-markdown-target]')!
    expect(link).toHaveAttribute('aria-disabled', 'true')
    await new Promise(resolve => setTimeout(resolve, 10))
    expect(link).not.toHaveAttribute('tabindex')

    await act(async () => {
      resolveTarget({ target, status: 'available', runtimeUrl: '/documents/guide' })
      await Promise.resolve()
    })

    await waitFor(() => expect(link).toHaveAttribute('href', '/documents/guide'))
    expect(link).toHaveAttribute('tabindex', '0')
  })

  it('keeps task state unchanged in read-only mode', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const { container } = render(
      <MarkdownEditor markdown={MIXED_MARKDOWN} readOnly onChange={onChange} />,
    )
    const view = within(container)

    const checkboxes = await view.findAllByRole('checkbox')
    expect(checkboxes[0]).toBeDisabled()
    expect(checkboxes[0]).not.toBeChecked()

    onChange.mockClear()
    await user.click(checkboxes[0] as HTMLElement)
    expect(onChange).not.toHaveBeenCalled()
  })
})
