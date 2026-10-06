import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { renderToString } from 'react-dom/server'
import { useEffect } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  MarkdownEditingSurface,
  MarkdownLocaleProvider,
  MarkdownSurface,
  MarkdownToolbar,
  useMarkdownCommands,
  useMarkdownEditor,
  useMarkdownState,
} from '../src/index.js'
import type {
  MarkdownEditorHandle,
  MarkdownReferenceAdapter,
  MarkdownReferenceCandidate,
  MarkdownSearchAdapter,
  MarkdownSearchResult,
  MarkdownUploadAdapter,
} from '../src/index.js'

afterEach(cleanup)

const GUIDE: MarkdownSearchResult = {
  id: 'guide',
  title: 'Guide',
  target: 'akb://team/doc/guide.md',
  kind: 'document',
}

const REFERENCE: MarkdownReferenceCandidate = {
  id: 'guide',
  kind: 'document',
  title: 'Guide',
  target: 'akb://team/doc/guide.md',
}

interface EditorProps {
  locale: 'en' | 'ko'
  initialMarkdown?: string
  onChange?: (markdown: string, editor: MarkdownEditorHandle) => void
  onSourceChange?: (markdown: string, editor: MarkdownEditorHandle) => void
  onEditor?: (editor: MarkdownEditorHandle | null) => void
  searchAdapter?: MarkdownSearchAdapter
  referenceAdapter?: MarkdownReferenceAdapter
  uploadAdapter?: MarkdownUploadAdapter
  imageMenu?: boolean
}

function LocalePage(props: EditorProps) {
  return (
    <MarkdownLocaleProvider locale={props.locale}>
      <LocaleEditor {...props} />
    </MarkdownLocaleProvider>
  )
}

function LocaleEditor({
  initialMarkdown = '# Original\n\nBody',
  onChange,
  onSourceChange,
  onEditor,
  searchAdapter,
  referenceAdapter,
  uploadAdapter,
  imageMenu = false,
}: Omit<EditorProps, 'locale'>) {
  const editor = useMarkdownEditor({
    initialMarkdown,
    onChange,
    reference: referenceAdapter ? { adapter: referenceAdapter } : undefined,
  })
  const commands = useMarkdownCommands(editor)
  const state = useMarkdownState(editor)

  useEffect(() => {
    onEditor?.(editor)
    return () => onEditor?.(null)
  }, [editor, onEditor])

  return (
    <>
      <MarkdownEditingSurface
        editor={editor}
        markdown={initialMarkdown}
        onSourceChange={onSourceChange}
        table={{}}
        imageMenu={imageMenu ? {} : undefined}
        imageUpload={uploadAdapter ? { adapter: uploadAdapter } : undefined}
        toolbar={(
          <MarkdownToolbar
            editor={editor}
            link={searchAdapter ? {
              searchAdapter,
              searchContext: { vault: 'team' },
              searchLabels: {
                inputLabel: 'Search Vault resources',
                inputPlaceholder: 'Find a document or file',
              },
            } : undefined}
          />
        )}
      >
        <MarkdownSurface editor={editor} editable />
      </MarkdownEditingSurface>
      <output data-testid="canonical-markdown">{state?.markdown ?? ''}</output>
      <output data-testid="history-state">
        {`${state?.canUndo ?? false}:${state?.canRedo ?? false}`}
      </output>
      <button type="button" onClick={() => commands.insertMarkdown('/table')}>Open slash menu</button>
      <button type="button" onClick={() => commands.insertMarkdown('@')}>Open reference menu</button>
      <button type="button" onClick={() => commands.insertTable()}>Insert test table</button>
    </>
  )
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(resolvePromise => { resolve = resolvePromise })
  return { promise, resolve }
}

describe('shared Markdown UI locale', () => {
  it('updates common controls and Source copy while preserving the editor, draft, focus, and callback count', async () => {
    const onChange = vi.fn()
    const onSourceChange = vi.fn()
    let currentHandle: MarkdownEditorHandle | null = null
    const onEditor = (handle: MarkdownEditorHandle | null) => { currentHandle = handle }
    const ui = render(
      <LocalePage locale="en" onChange={onChange} onSourceChange={onSourceChange} onEditor={onEditor} />,
    )

    await waitFor(() => expect(screen.getByRole('toolbar', { name: 'Text formatting' })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Source' }))
    const source = await screen.findByRole('textbox', { name: 'Markdown source' }) as HTMLTextAreaElement
    fireEvent.change(source, { target: { value: '# Draft\n\nBody' } })
    source.setSelectionRange(3, 5)
    source.focus()

    const originalHandle = currentHandle
    const originalEditorMarkdown = screen.getByTestId('canonical-markdown').textContent
    const sourceChangeCount = onSourceChange.mock.calls.length
    const changeCount = onChange.mock.calls.length

    ui.rerender(
      <LocalePage locale="ko" onChange={onChange} onSourceChange={onSourceChange} onEditor={onEditor} />,
    )

    const koreanSource = screen.getByRole('textbox', { name: 'Markdown 원문' }) as HTMLTextAreaElement
    expect(koreanSource).toBe(source)
    expect(currentHandle).toBe(originalHandle)
    expect(koreanSource).toHaveValue('# Draft\n\nBody')
    expect(koreanSource.selectionStart).toBe(3)
    expect(koreanSource.selectionEnd).toBe(5)
    expect(koreanSource).toHaveFocus()
    expect(koreanSource).toHaveAttribute('placeholder', 'Markdown 원문을 입력하세요…')
    expect(screen.getByTestId('canonical-markdown').textContent).toBe(originalEditorMarkdown)
    expect(onSourceChange).toHaveBeenCalledTimes(sourceChangeCount)
    expect(onChange).toHaveBeenCalledTimes(changeCount)

    fireEvent.click(screen.getByRole('button', { name: '시각 편집' }))
    await waitFor(() => expect(screen.getByRole('toolbar', { name: '서식 도구' })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: '굵게' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '다시 실행' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('canonical-markdown').textContent).toContain('# Draft'))
    const beforeFormat = screen.getByTestId('canonical-markdown').textContent
    fireEvent.click(screen.getByRole('button', { name: '제목 2' }))
    await waitFor(() => expect(screen.getByTestId('canonical-markdown').textContent).not.toBe(beforeFormat))
    const afterFormat = screen.getByTestId('canonical-markdown').textContent
    fireEvent.click(screen.getByRole('button', { name: '실행 취소' }))
    await waitFor(() => expect(screen.getByTestId('canonical-markdown').textContent).not.toBe(afterFormat))
    expect(screen.getByTestId('history-state')).toHaveTextContent(':true')
    fireEvent.click(screen.getByRole('button', { name: '다시 실행' }))
    await waitFor(() => expect(screen.getByTestId('canonical-markdown').textContent).toBe(afterFormat))
  })

  it('keeps each provider independent and renders its explicit locale on the server', () => {
    const html = renderToString(
      <>
        <MarkdownLocaleProvider locale="en">
          <MarkdownToolbar editor={null} />
        </MarkdownLocaleProvider>
        <MarkdownLocaleProvider locale="ko">
          <MarkdownToolbar editor={null} />
        </MarkdownLocaleProvider>
      </>,
    )

    expect(html).toContain('aria-label="Text formatting"')
    expect(html).toContain('aria-label="서식 도구"')
    expect(html).toContain('aria-label="Paragraph"')
    expect(html).toContain('aria-label="문단"')
  })

  it('relabels an open table toolbar without losing its focused control or target', async () => {
    const user = userEvent.setup()
    const ui = render(<LocalePage locale="en" initialMarkdown="" />)
    await user.click(await screen.findByRole('button', { name: 'Insert test table' }))
    const table = await screen.findByRole('table', { name: 'Editable table' })

    const toolbar = within(table).getByRole('toolbar', { name: 'Table actions' })
    const addRow = within(toolbar).getByRole('button', { name: 'Add row after selected row' })
    await waitFor(() => expect(addRow).toBeEnabled())
    addRow.focus()
    const originalRowCount = within(table).getAllByRole('row').length

    ui.rerender(<LocalePage locale="ko" initialMarkdown="" />)

    const koreanTable = await screen.findByRole('table', { name: '편집 가능한 표' })
    const koreanToolbar = within(koreanTable).getByRole('toolbar', { name: '표 작업' })
    const koreanAddRow = within(koreanToolbar).getByRole('button', { name: '선택한 행 아래에 행 추가' })
    expect(koreanTable).toBe(table)
    expect(koreanToolbar).toBe(toolbar)
    expect(koreanAddRow).toBe(addRow)
    expect(koreanAddRow).toHaveFocus()

    await user.keyboard('{Enter}')
    await waitFor(() => expect(within(koreanTable).getAllByRole('row')).toHaveLength(originalRowCount + 1))
  })

  it('relocalizes an open link dialog and validation error while preserving its input', async () => {
    const user = userEvent.setup()
    const ui = render(<LocalePage locale="en" initialMarkdown="Body" />)
    const insertLink = await screen.findByRole('button', { name: 'Insert link' })
    insertLink.focus()
    await user.keyboard('{Enter}')

    const dialog = await screen.findByRole('dialog', { name: 'Insert link' })
    const url = within(dialog).getByRole('textbox', { name: 'URL' })
    await user.clear(url)
    await user.type(url, 'javascript:alert(1)')
    await user.click(within(dialog).getByRole('button', { name: 'Insert link' }))
    const error = await within(dialog).findByRole('alert')
    expect(error).toHaveTextContent('Enter an http(s), email, phone, anchor, or relative URL.')
    await waitFor(() => expect(url).toHaveFocus())

    ui.rerender(<LocalePage locale="ko" initialMarkdown="Body" />)

    const koreanDialog = await screen.findByRole('dialog', { name: '링크 삽입' })
    const koreanUrl = within(koreanDialog).getByRole('textbox', { name: 'URL' })
    const koreanError = within(koreanDialog).getByRole('alert')
    expect(koreanDialog).toBe(dialog)
    expect(koreanUrl).toBe(url)
    expect(koreanUrl).toHaveValue('javascript:alert(1)')
    expect(koreanUrl).toHaveFocus()
    expect(koreanError).toBe(error)
    expect(koreanError).toHaveTextContent('http(s), 이메일, 전화번호, 앵커 또는 상대 경로 주소를 입력하세요.')
  })

  it('relocalizes an open image dialog and validation error without discarding its draft', async () => {
    const user = userEvent.setup()
    const markdown = '![Existing](https://example.com/existing.png)'
    const ui = render(<LocalePage locale="en" initialMarkdown={markdown} imageMenu />)
    await user.click(await screen.findByRole('button', { name: 'Edit image description: Existing' }))

    const dialog = await screen.findByRole('dialog', { name: 'Image description' })
    const description = within(dialog).getByRole('textbox', { name: 'Description' })
    await user.clear(description)
    await user.type(description, 'Updated alt text')

    ui.rerender(<LocalePage locale="ko" initialMarkdown={markdown} imageMenu />)

    const koreanDialog = await screen.findByRole('dialog', { name: '이미지 설명' })
    const koreanDescription = within(koreanDialog).getByRole('textbox', { name: '설명' })
    expect(koreanDialog).toBe(dialog)
    expect(koreanDescription).toBe(description)
    expect(koreanDescription).toHaveValue('Updated alt text')
    expect(koreanDescription).toHaveFocus()

    await user.clear(koreanDescription)
    const koreanSave = within(koreanDialog).getByRole('button', { name: '설명 저장' })
    await user.click(koreanSave)
    const koreanError = await within(koreanDialog).findByRole('alert')
    expect(koreanError).toHaveTextContent('이미지를 볼 수 없는 독자도 이해할 수 있도록 설명을 입력하세요.')

    ui.rerender(<LocalePage locale="en" initialMarkdown={markdown} imageMenu />)

    const englishDialog = await screen.findByRole('dialog', { name: 'Image description' })
    const englishSave = within(englishDialog).getByRole('button', { name: 'Save description' })
    expect(englishDialog).toBe(dialog)
    expect(within(englishDialog).getByRole('alert')).toBe(koreanError)
    expect(within(englishDialog).getByRole('alert'))
      .toHaveTextContent('Describe the image so it remains understandable without sight.')
    expect(englishSave).toBe(koreanSave)
    expect(englishSave).toHaveFocus()
  })

  it('updates task checkbox names across locales and later task edits', async () => {
    const markdown = '- [ ] Parent task\n  - [x] Child task'
    const ui = render(<LocalePage locale="en" initialMarkdown={markdown} />)
    await screen.findByRole('checkbox', { name: 'Task item checkbox for Parent task' })
    screen.getByRole('checkbox', { name: 'Task item checkbox for Child task' })

    ui.rerender(<LocalePage locale="ko" initialMarkdown={markdown} />)

    const koreanParent = await screen.findByRole('checkbox', { name: '작업 항목 선택란: Parent task' })
    screen.getByRole('checkbox', { name: '작업 항목 선택란: Child task' })

    fireEvent.click(koreanParent)
    await waitFor(() => expect(screen.getByTestId('canonical-markdown')).toHaveTextContent('[x] Parent task'))
    expect(await screen.findByRole('checkbox', { name: '작업 항목 선택란: Parent task' })).toBeChecked()
  })

  it('updates an open link-search portal without cancelling or repeating its request', async () => {
    const pending = deferred<readonly MarkdownSearchResult[]>()
    const searchAdapter: MarkdownSearchAdapter = {
      search: vi.fn((_query, context) => {
        expect(context?.signal).toBeInstanceOf(AbortSignal)
        return pending.promise
      }),
    }
    const ui = render(<LocalePage locale="en" searchAdapter={searchAdapter} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Insert link' }))
    const search = await screen.findByRole('combobox', { name: 'Search Vault resources' }) as HTMLInputElement
    fireEvent.change(search, { target: { value: 'guide' } })
    await waitFor(() => expect(searchAdapter.search).toHaveBeenCalledTimes(1))
    search.focus()
    const signal = vi.mocked(searchAdapter.search).mock.calls[0]?.[1]?.signal

    ui.rerender(<LocalePage locale="ko" searchAdapter={searchAdapter} />)

    const koreanSearch = await screen.findByRole('combobox', { name: 'Search Vault resources' }) as HTMLInputElement
    expect(koreanSearch).toBe(search)
    expect(koreanSearch).toHaveValue('guide')
    expect(koreanSearch).toHaveFocus()
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('검색 중…'))
    expect(searchAdapter.search).toHaveBeenCalledTimes(1)
    expect(signal?.aborted).toBe(false)

    await act(async () => pending.resolve([GUIDE]))
    expect(await screen.findByRole('option', { name: 'Guide (문서)' })).toBeInTheDocument()
    expect(searchAdapter.search).toHaveBeenCalledTimes(1)
  })

  it('relabels a pending reference search without losing the query or late results', async () => {
    const pending = deferred<readonly MarkdownReferenceCandidate[]>()
    let signal: AbortSignal | undefined
    const referenceAdapter: MarkdownReferenceAdapter = {
      search: vi.fn((_query, context) => {
        signal = context?.signal
        return pending.promise
      }),
    }
    const ui = render(<LocalePage locale="en" initialMarkdown="" referenceAdapter={referenceAdapter} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open reference menu' }))
    const menu = await screen.findByRole('listbox', { name: 'Insert reference' })
    await waitFor(() => expect(referenceAdapter.search).toHaveBeenCalledTimes(1))

    ui.rerender(<LocalePage locale="ko" initialMarkdown="" referenceAdapter={referenceAdapter} />)

    expect(await screen.findByRole('listbox', { name: '참조 삽입' })).toBe(menu)
    expect(menu).toHaveTextContent('검색 중…')
    expect(referenceAdapter.search).toHaveBeenCalledTimes(1)
    expect(signal?.aborted).toBe(false)

    await act(async () => pending.resolve([REFERENCE]))
    expect(await screen.findByRole('option', { name: 'Guide' })).toBeInTheDocument()
    expect(menu.querySelector('[data-reference-section="document"]')).toHaveAttribute('aria-label', '문서')
  })

  it('keeps slash query and active command while relabeling the open menu', async () => {
    const ui = render(<LocalePage locale="en" initialMarkdown="" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open slash menu' }))
    const menu = await screen.findByRole('listbox', { name: 'Insert block' })
    await waitFor(() => expect(menu.querySelector('[data-slash-command="table"]')).toBeInTheDocument())
    fireEvent.pointerMove(menu.querySelector('[data-slash-command="table"]')!)
    const activeCommand = menu.querySelector('[aria-selected="true"]')?.getAttribute('data-slash-command')

    ui.rerender(<LocalePage locale="ko" initialMarkdown="" />)

    const koreanMenu = await screen.findByRole('listbox', { name: '블록 삽입' })
    expect(koreanMenu).toBe(menu)
    expect(koreanMenu.querySelector('[aria-selected="true"]')?.getAttribute('data-slash-command'))
      .toBe(activeCommand)
    expect(koreanMenu).toHaveTextContent('표')
    expect(koreanMenu).toHaveTextContent('기본 표를 삽입합니다')
    expect(screen.getByTestId('canonical-markdown')).toHaveTextContent('/table')
    fireEvent.click(koreanMenu.querySelector<HTMLButtonElement>('[data-slash-command="table"]')!)
    await waitFor(() => expect(screen.getByTestId('canonical-markdown')).toHaveTextContent('|'))
  })

  it('keeps an in-flight upload alive across a locale change', async () => {
    const pending = deferred<{ target: string; kind: 'attachment'; alt: string }>()
    const uploadAdapter: MarkdownUploadAdapter = {
      upload: vi.fn((_file, context) => {
        expect(context?.signal).toBeInstanceOf(AbortSignal)
        return pending.promise
      }),
    }
    const ui = render(<LocalePage locale="en" initialMarkdown="" uploadAdapter={uploadAdapter} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Insert image' })).toBeEnabled())
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
    fireEvent.change(input, {
      target: { files: [new File(['image'], 'diagram.png', { type: 'image/png' })] },
    })
    await waitFor(() => expect(uploadAdapter.upload).toHaveBeenCalledTimes(1))
    const signal = vi.mocked(uploadAdapter.upload).mock.calls[0]?.[1]?.signal

    ui.rerender(<LocalePage locale="ko" initialMarkdown="" uploadAdapter={uploadAdapter} />)

    expect(await screen.findByText('diagram.png 확인 중')).toBeInTheDocument()
    expect(uploadAdapter.upload).toHaveBeenCalledTimes(1)
    expect(signal?.aborted).toBe(false)

    await act(async () => pending.resolve({
      kind: 'attachment',
      target: 'akb://team/file/diagram',
      alt: 'diagram',
    }))
    await waitFor(() => expect(screen.getByTestId('canonical-markdown')).toHaveTextContent('![diagram](akb://team/file/diagram)'))
  })

  it('relabels a failed upload and retries the same file after a locale change', async () => {
    const markdown = ''
    const uploadAdapter: MarkdownUploadAdapter = {
      upload: vi.fn()
        .mockRejectedValueOnce(Object.assign(new Error('offline'), {
          code: 'unavailable',
          retryable: true,
        }))
        .mockResolvedValueOnce({
          kind: 'attachment',
          target: 'akb://team/file/retried',
          alt: 'retried',
        }),
    }
    const ui = render(<LocalePage locale="en" initialMarkdown={markdown} uploadAdapter={uploadAdapter} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Insert image' })).toBeEnabled())
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!
    const file = new File(['image'], 'retry.png', { type: 'image/png' })
    fireEvent.change(input, { target: { files: [file] } })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Image upload failed')

    ui.rerender(<LocalePage locale="ko" initialMarkdown={markdown} uploadAdapter={uploadAdapter} />)

    const koreanAlert = screen.getByRole('alert')
    expect(koreanAlert).toBe(alert)
    expect(koreanAlert).toHaveTextContent('이미지 업로드 실패')
    await userEvent.setup().click(within(koreanAlert).getByRole('button', { name: '다시 시도' }))
    await waitFor(() => expect(uploadAdapter.upload).toHaveBeenCalledTimes(2))
    expect(vi.mocked(uploadAdapter.upload).mock.calls[1]?.[0]).toBe(file)
    await waitFor(() => expect(screen.getByTestId('canonical-markdown')).toHaveTextContent('![retried](akb://team/file/retried)'))
  })
})
