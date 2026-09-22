import { afterEach, describe, expect, it } from 'vitest'

import {
  canonicalizeMarkdown,
  createMarkdownEditor,
  extractMarkdownReferences,
  extractMarkdownTargets,
  markdownReferenceKey,
  markdownCommands,
  parseMarkdownReferenceToken,
  parseMarkdown,
  resolveMarkdownReferences,
  serializeMarkdown,
  uploadMarkdownBatch,
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

  it('keeps code fence language and content canonical when highlighting is unavailable', () => {
    const markdown = ['```not-a-registered-language', '<value>&raw</value>', '```'].join('\n')
    const editor = createMarkdownEditor({ initialMarkdown: markdown })
    editors.push(editor)

    const codeBlock = editor.state.doc.firstChild
    expect(codeBlock?.type.name).toBe('codeBlock')
    expect(codeBlock?.attrs.language).toBe('not-a-registered-language')
    expect(codeBlock?.textContent).toBe('<value>&raw</value>')
    expect(editor.getMarkdown().trim()).toBe(markdown)
  })

  it('has canonical idempotence for the full conformance fixture', () => {
    const canonical = canonicalizeMarkdown(fixture)

    expect(canonicalizeMarkdown(canonical)).toBe(canonical)
  })

  it('keeps document, file, and attachment targets through image/link editing', () => {
    const attachment = '/api/assets/00000000-0000-4000-8000-000000000001'
    const document = 'akb://vault/coll/notes/doc/guide.md'
    const file = 'akb://vault/file/00000000-0000-4000-8000-000000000002'
    const markdown = [
      `[Guide](${document})`,
      `[Download](${file})`,
      `![Diagram](${attachment})`,
      '![Remote](https://example.com/remote.png)',
      '`[ignored](akb://vault/doc/ignored.md)`',
    ].join('\n\n')

    expect(canonicalizeMarkdown(markdown)).toContain(`![Diagram](${attachment})`)
    expect(extractMarkdownTargets(markdown)).toEqual([
      { kind: 'document', target: document },
      { kind: 'file', target: file },
      { kind: 'attachment', target: attachment },
    ])
  })

  it('round-trips person syntax and issue IDs while excluding Markdown-owned regions', () => {
    const markdown = [
      '@alice @{Ada Lovelace} REEF-123 UNKNOWN-9',
      '[label @link REEF-456](https://example.com)',
      '`@code REEF-457`',
      '```text',
      '@fenced REEF-458',
      '```',
      '\\@escaped \\REEF-459 email@example.com',
    ].join('\n\n')

    expect(extractMarkdownReferences(markdown)).toEqual([
      { kind: 'person', id: 'alice', value: '@alice' },
      { kind: 'person', id: 'Ada Lovelace', value: '@{Ada Lovelace}' },
      { kind: 'issue', id: 'REEF-123', value: 'REEF-123' },
      { kind: 'issue', id: 'UNKNOWN-9', value: 'UNKNOWN-9' },
    ])
    expect(parseMarkdownReferenceToken('@alice')).toEqual({
      kind: 'person',
      id: 'alice',
      value: '@alice',
    })
    expect(parseMarkdownReferenceToken('REEF-123')).toEqual({
      kind: 'issue',
      id: 'REEF-123',
      value: 'REEF-123',
    })
    expect(parseMarkdownReferenceToken('@alice trailing')).toBeNull()

    const canonical = canonicalizeMarkdown(markdown)
    expect(canonicalizeMarkdown(canonical)).toBe(canonical)
    expect(extractMarkdownReferences(canonical)).toEqual(
      extractMarkdownReferences(markdown),
    )
    expect(canonical).toContain('`@code REEF-457`')
    expect(canonical).toContain('@fenced REEF-458')

    expect(extractMarkdownReferences('\\\\REEF-459')).toEqual([
      { kind: 'issue', id: 'REEF-459', value: 'REEF-459' },
    ])
    expect(extractMarkdownReferences('\\REEF-459')).toEqual([])
    expect(canonicalizeMarkdown('\\REEF-459')).toBe('\\REEF-459')

    const linkLabel = parseMarkdown(markdown).content?.[1]?.content?.[0]
    expect(linkLabel?.marks?.some(mark => mark.type === 'markdownReference')).toBe(false)
  })

  it('keeps reference runtime resolution outside canonical Markdown', async () => {
    const references = extractMarkdownReferences('@alice REEF-123')
    const adapter = {
      search: async () => [],
      resolve: async (reference: (typeof references)[number]) =>
        reference.kind === 'person'
          ? {
              ...reference,
              status: 'available' as const,
              title: 'Ada Lovelace',
              runtimeUrl: '/people/alice',
            }
          : {
              ...reference,
              status: 'unavailable' as const,
              reason: 'deleted' as const,
            },
    }

    const resolutions = await resolveMarkdownReferences(adapter, references)

    expect(resolutions.get(markdownReferenceKey(references[0]!))).toMatchObject({
      status: 'available',
      title: 'Ada Lovelace',
      runtimeUrl: '/people/alice',
    })
    expect(resolutions.get(markdownReferenceKey(references[1]!))).toMatchObject({
      status: 'unavailable',
      reason: 'deleted',
    })
    expect(canonicalizeMarkdown('@alice REEF-123')).toBe('@alice REEF-123')
  })

  it('edits a reference as ordinary text with undo and redo', () => {
    const editor = createMarkdownEditor({ initialMarkdown: '@alice 주변 텍스트' })
    editors.push(editor)

    editor.commands.setTextSelection({ from: 1, to: 7 })
    expect(editor.commands.insertContent('@bob')).toBe(true)
    expect(editor.getMarkdown()).toBe('@bob 주변 텍스트')
    expect(editor.commands.undo()).toBe(true)
    expect(editor.getMarkdown()).toBe('@alice 주변 텍스트')
    expect(editor.commands.redo()).toBe(true)
    expect(editor.getMarkdown()).toBe('@bob 주변 텍스트')
  })

  it('retains every per-file upload outcome for a partial batch', async () => {
    const files = [new Blob(['one']), new Blob(['two']), new Blob(['three'])]
    const adapter = {
      upload: async (file: Blob) => {
        if (file === files[1]) throw Object.assign(new Error('busy'), { retryable: true })
        return { kind: 'attachment' as const, target: `/api/assets/${file.size}` }
      },
    }

    const result = await uploadMarkdownBatch(adapter, files)

    expect(result).toMatchObject({ succeeded: 2, failed: 1, cancelled: 0, partial: true })
    expect(result.items.map(item => item.status)).toEqual(['success', 'failed', 'success'])
    expect(result.items[1]).toMatchObject({
      status: 'failed',
      error: { code: 'unknown', message: 'busy', retryable: true },
    })
  })

  it('marks the remaining files cancelled once the batch signal is aborted', async () => {
    const controller = new AbortController()
    const files = [new Blob(['one']), new Blob(['two'])]
    const adapter = {
      upload: async (file: Blob) => {
        if (file === files[0]) controller.abort()
        return { kind: 'attachment' as const, target: `/api/assets/${file.size}` }
      },
    }

    const result = await uploadMarkdownBatch(adapter, files, { signal: controller.signal })

    expect(result.succeeded).toBe(1)
    expect(result.cancelled).toBe(1)
    expect(result.items.map(item => item.status)).toEqual(['success', 'cancelled'])
  })

  it('exposes product-neutral adapter contracts without implementing product behavior', () => {
    const adapters: MarkdownAdapters = {
      upload: {
        upload: async file => ({
          kind: 'attachment',
          target: `/api/assets/${file.size}`,
        }),
      },
      search: {
        search: async query => [{
          id: query,
          title: query,
          target: `akb://vault/doc/${query}.md`,
        }],
      },
      targetResolver: {
        resolve: async target => ({
          target,
          status: 'available',
          runtimeUrl: `/runtime/${encodeURIComponent(target)}`,
        }),
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

  it('reports the canonical Markdown without the editor continuation paragraph', () => {
    const changes: string[] = []
    const target = 'https://example.com/image.png'
    const editor = createMarkdownEditor({
      onChange: markdown => changes.push(markdown),
    })
    editors.push(editor)

    expect(markdownCommands(editor).insertImage(target, 'Image')).toBe(true)
    expect(changes.at(-1)).toBe(`![Image](${target})`)
  })

  it('edits and deletes one image occurrence by document position with undo', () => {
    const target = 'https://example.com/shared.png'
    const editor = createMarkdownEditor({ initialMarkdown: '' })
    editors.push(editor)
    const commands = markdownCommands(editor)
    expect(commands.insertImage(target, 'First')).toBe(true)
    expect(commands.insertImage(target, 'Second')).toBe(true)
    expect(commands.insertImage('https://example.com/other.png', 'Other')).toBe(true)
    const positions: number[] = []
    editor.state.doc.descendants((node, position) => {
      if (node.type.name === 'image') positions.push(position)
    })

    expect(positions).toHaveLength(3)
    expect(commands.setImageAltAt(positions[1]!, 'Second updated')).toBe(true)
    expect(editor.getMarkdown()).toContain(`![First](${target})`)
    expect(editor.getMarkdown()).toContain(`![Second updated](${target})`)
    expect(editor.getMarkdown()).toContain('![Other](https://example.com/other.png)')

    expect(commands.undo()).toBe(true)
    expect(editor.getMarkdown()).toContain(`![Second](${target})`)
    expect(commands.deleteImageAt(positions[1]!)).toBe(true)
    expect(editor.getMarkdown()).not.toContain(`![Second](${target})`)
    expect(editor.getMarkdown()).toContain(`![First](${target})`)
    expect(editor.getMarkdown()).toContain('![Other](https://example.com/other.png)')
    expect(commands.undo()).toBe(true)
    expect(editor.getMarkdown()).toContain(`![Second](${target})`)
  })

  it('rejects image mutations outside the editable image node', () => {
    const editor = createMarkdownEditor({ initialMarkdown: 'Text' })
    editors.push(editor)
    const commands = markdownCommands(editor)
    expect(commands.insertImage('https://example.com/image.png', 'Image')).toBe(true)
    const imagePositions: number[] = []
    editor.state.doc.descendants((node, position) => {
      if (node.type.name === 'image') imagePositions.push(position)
    })

    expect(commands.setImageAltAt(-1, 'Updated')).toBe(false)
    expect(commands.deleteImageAt(-1)).toBe(false)
    const position = imagePositions[0]
    expect(position).toBeDefined()
    editor.setEditable(false)
    expect(commands.setImageAltAt(position!, 'Updated')).toBe(false)
    expect(commands.deleteImageAt(position!)).toBe(false)
    expect(editor.getMarkdown()).toContain('![Image](https://example.com/image.png)')
  })

  it('routes every default formatting command through the shared contract', () => {
    const editor = createMarkdownEditor({ initialMarkdown: 'text' })
    editors.push(editor)
    const commands = markdownCommands(editor)
    const resetSelection = () => {
      expect(commands.setMarkdown('text')).toBe(true)
      editor.commands.setTextSelection({ from: 1, to: 5 })
    }

    resetSelection()
    expect(commands.toggleBold()).toBe(true)
    expect(editor.getMarkdown()).toContain('**text**')
    resetSelection()
    expect(commands.toggleItalic()).toBe(true)
    expect(editor.getMarkdown()).toContain('*text*')
    resetSelection()
    expect(commands.toggleStrike()).toBe(true)
    expect(editor.getMarkdown()).toContain('~~text~~')
    resetSelection()
    expect(commands.toggleCode()).toBe(true)
    expect(editor.getMarkdown()).toContain('`text`')
    resetSelection()
    expect(commands.toggleBulletList()).toBe(true)
    expect(editor.getMarkdown()).toContain('- text')
    resetSelection()
    expect(commands.toggleOrderedList()).toBe(true)
    expect(editor.getMarkdown()).toContain('1. text')
    resetSelection()
    expect(commands.toggleTaskList()).toBe(true)
    expect(editor.getMarkdown()).toContain('- [ ] text')
    resetSelection()
    expect(commands.toggleBlockquote()).toBe(true)
    expect(editor.getMarkdown()).toContain('> text')
    resetSelection()
    expect(commands.toggleCodeBlock()).toBe(true)
    expect(editor.getMarkdown()).toContain('```')
    expect(commands.setHorizontalRule()).toBe(true)
    expect(editor.getMarkdown()).toContain('---')
    expect(commands.setMarkdown('# text')).toBe(true)
    expect(commands.setParagraph()).toBe(true)
    expect(editor.getMarkdown().trim()).toBe('text')
    expect(commands.setMarkdown('text')).toBe(true)
    expect(commands.toggleHeading(2)).toBe(true)
    expect(editor.getMarkdown()).toContain('## text')
  })

  it('applies, updates, removes, and undoes links through the shared commands', () => {
    const editor = createMarkdownEditor({ initialMarkdown: 'text' })
    editors.push(editor)
    const commands = markdownCommands(editor)

    editor.commands.setTextSelection({ from: 1, to: 5 })
    expect(commands.setLink('https://old.example')).toBe(true)
    expect(editor.getMarkdown()).toBe('[text](https://old.example)')
    expect(editor.getAttributes('link').target).toBe('_blank')

    expect(commands.setLink('https://new.example')).toBe(true)
    expect(editor.getMarkdown()).toBe('[text](https://new.example)')
    expect(editor.getAttributes('link').target).toBe('_blank')

    expect(commands.unsetLink()).toBe(true)
    expect(editor.getMarkdown()).toBe('text')
    expect(commands.undo()).toBe(true)
    expect(editor.getMarkdown()).toBe('[text](https://new.example)')
    expect(commands.redo()).toBe(true)
    expect(editor.getMarkdown()).toBe('text')
  })
})
