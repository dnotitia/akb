import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MarkdownEditor, MarkdownViewer } from '../src/index.js'

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
    const codeOptions = {
      labels: { region: (language?: string) => `Code region${language ? `: ${language}` : ''}` },
    }
    const { container } = render(
      <>
        <MarkdownEditor markdown={MIXED_MARKDOWN} code={codeOptions} />
        <MarkdownViewer markdown={MIXED_MARKDOWN} code={codeOptions} />
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
    expect(code).toHaveAttribute('aria-label', 'Code region: typescript')
    expect(code?.querySelector('.hljs-keyword')).toHaveTextContent('const')
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
