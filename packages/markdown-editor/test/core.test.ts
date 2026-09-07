import { afterEach, describe, expect, it } from 'vitest'

import {
  canonicalizeMarkdown,
  createMarkdownEditor,
  markdownCommands,
  parseMarkdown,
  serializeMarkdown,
} from '../src/index.js'
import type { MarkdownAdapters } from '../src/index.js'

const fixture = `# 공통 문법

본문의 **굵은 글씨**와 [링크](https://example.com).

- [x] 완료된 작업
- 중첩 항목

| 이름 | 값 |
| --- | --- |
| A | B |

인라인 수식 $E=mc^2$.

\`\`\`mermaid
flowchart TD
  A --> B
\`\`\`

<div data-kind="raw">
원문 HTML
</div>

<Callout tone={warning}>MDX 원문</Callout>`

const editors: ReturnType<typeof createMarkdownEditor>[] = []

afterEach(() => {
  editors.splice(0).forEach(editor => editor.destroy())
})

describe('Markdown conformance core', () => {
  it('parses common Markdown/GFM/math/Mermaid/raw constructs into one semantic document', () => {
    const document = parseMarkdown(fixture)
    const types = document.content?.map(node => node.type)

    expect(types).toEqual([
      'heading',
      'paragraph',
      'taskList',
      'bulletList',
      'table',
      'paragraph',
      'codeBlock',
      'rawMarkdownBlock',
      'rawMarkdownBlock',
    ])
    expect(document.content?.find(node => node.type === 'codeBlock')?.attrs?.language).toBe('mermaid')
    expect(document.content?.filter(node => node.type === 'rawMarkdownBlock')).toHaveLength(2)
  })

  it('supports parse, edit, and serialize without losing the document meaning', () => {
    const document = parseMarkdown(fixture)
    const heading = document.content?.[0]
    if (!heading?.content?.[0]) throw new Error('fixture heading was not parsed')
    heading.content[0].text = '수정된 제목'

    const serialized = serializeMarkdown(document)

    expect(serialized).toContain('# 수정된 제목')
    expect(serialized).toContain('```mermaid')
    expect(serialized).toContain('<Callout tone={warning}>MDX 원문</Callout>')
    expect(serialized).toContain('<div data-kind="raw">')
  })

  it('has canonical idempotence for the full conformance fixture', () => {
    const canonical = canonicalizeMarkdown(fixture)

    expect(canonicalizeMarkdown(canonical)).toBe(canonical)
  })

  it('exposes product-neutral adapter contracts without implementing product behavior', () => {
    const adapters: MarkdownAdapters = {
      upload: {
        upload: async file => ({ url: `https://cdn.example/${file.size}` }),
      },
      search: {
        search: async query => [{ id: query, title: query }],
      },
      targetResolver: {
        resolve: async target => target,
      },
    }

    expect(adapters.upload).toBeDefined()
    expect(adapters.search).toBeDefined()
    expect(adapters.targetResolver).toBeDefined()
  })

  it('owns Markdown commands and undo state on the Tiptap editor', () => {
    const editor = createMarkdownEditor({ initialMarkdown: '초안' })
    editors.push(editor)
    const commands = markdownCommands(editor)

    expect(commands.setMarkdown('**강조**')).toBe(true)
    expect(editor.getMarkdown()).toContain('**강조**')
    expect(commands.insertMarkdown(' 추가')).toBe(true)
    expect(commands.undo()).toBe(true)
    expect(editor.getMarkdown()).toBe('초안')
  })
})
