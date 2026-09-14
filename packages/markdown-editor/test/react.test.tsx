import { render, waitFor } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import {
  MarkdownEditor,
  MarkdownViewer,
  useMarkdownCommands,
  useMarkdownEditor,
  useMarkdownState,
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

  it('routes slash keyboard input to the shared callback while preserving the slash', async () => {
    const user = userEvent.setup()
    const slashCalls: number[] = []
    const { container } = render(
      <MarkdownEditor markdown="본문" onSlash={({ position }) => slashCalls.push(position)} />,
    )

    await waitFor(() => expect(container.querySelector('.ProseMirror')).toBeInTheDocument())
    const editor = container.querySelector('.ProseMirror') as HTMLElement
    editor.focus()
    await user.keyboard('/')

    expect(slashCalls).toHaveLength(1)
    expect(editor).toHaveTextContent('/본문')
  })

  it('updates state hooks and commands through the same editor instance', async () => {
    const { getByRole, getByTestId } = render(<HookProbe />)

    await waitFor(() => expect(getByTestId('hook-state')).toHaveTextContent('초안'))
    await userEvent.setup().click(getByRole('button', { name: 'set' }))
    await waitFor(() => expect(getByTestId('hook-state')).toHaveTextContent('명령'))
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
})
