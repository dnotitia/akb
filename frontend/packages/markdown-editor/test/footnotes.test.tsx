import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { parseMarkdown, serializeMarkdown } from '../src/core.js'
import { MarkdownViewer } from '../src/react/index.js'

afterEach(cleanup)
const source = 'A note[^한글] and again[^한글]. Missing[^missing].\n\n[^한글]: **Strong** and [source](https://example.org).\n    Continued.\n\n    A second paragraph.'

it('preserves footnote identifiers, multiline definitions and links through parsing and editing', () => {
  const parsed = parseMarkdown(source)
  expect(parsed.content?.filter(node => node.type === 'footnoteDefinition')).toHaveLength(1)
  const serialized = serializeMarkdown(parsed)
  expect(serialized).toContain('[^한글]: **Strong** and [source](https://example.org).')
  expect(serialized).toContain('    A second paragraph.')
  expect(serializeMarkdown(parseMarkdown(serialized))).toBe(serialized)
  const note = parsed.content?.find(node => node.type === 'footnoteDefinition')
  note?.content?.push({ type: 'paragraph', content: [{ type: 'text', text: 'Edited note.' }] })
  expect(serializeMarkdown(parsed)).toContain('    Edited note.')
})

it('keeps code and escaped references literal', () => {
  const parsed = parseMarkdown('`[^literal]` and \\[^escaped]\n\n```md\n[^code]: unchanged\n```')
  expect(JSON.stringify(parsed)).not.toContain('footnoteReference')
  expect(JSON.stringify(parsed)).not.toContain('footnoteDefinition')
})

it('isolates two viewers, numbers repeated references and supplies exact return links', async () => {
  const { container } = render(<><MarkdownViewer markdown={source} /><MarkdownViewer markdown={source} /></>)
  await waitFor(() => expect(container.querySelectorAll('[role="doc-endnote"]')).toHaveLength(2))
  const definitions = [...container.querySelectorAll<HTMLElement>('[role="doc-endnote"]')]
  expect(new Set(definitions.map(element => element.id)).size).toBe(2)
  for (const definition of definitions) {
    const viewer = definition.closest('.ProseMirror')!
    const links = [...viewer.querySelectorAll<HTMLAnchorElement>('[role="doc-noteref"]')]
    expect(links).toHaveLength(2)
    expect(links.map(link => link.textContent)).toEqual(['1', '1'])
    expect(links.every(link => link.getAttribute('href') === `#${definition.id}`)).toBe(true)
    const returns = [...definition.querySelectorAll<HTMLAnchorElement>('[role="doc-backlink"]')]
    expect(returns.map(link => link.hash)).toEqual(links.map(link => `#${link.id}`))
    expect(viewer.querySelector('[data-footnote-link="missing"]')?.hasAttribute('href')).toBe(false)
  }
})
