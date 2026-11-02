import { Node, type MarkdownToken } from '@tiptap/core'

type FootnoteToken = MarkdownToken & { label?: string }

/** Footnotes are schema content, so identifiers and definition links survive editing. */
export const MarkdownFootnoteReference = Node.create({
  name: 'footnoteReference', group: 'inline', inline: true, atom: true,
  addAttributes: () => ({ label: { default: '' } }),
  parseHTML: () => [{ tag: 'sup[data-markdown-footnote-reference]', getAttrs: element => ({ label: element.getAttribute('data-markdown-footnote-reference') }) }],
  renderHTML: ({ node }) => ['sup', { 'data-markdown-footnote-reference': node.attrs.label }, ['a', { 'data-footnote-link': node.attrs.label }, `[^${node.attrs.label}]`]],
  addNodeView() {
    return ({ node }) => {
      const dom = document.createElement('sup'), link = document.createElement('a')
      dom.dataset.markdownFootnoteReference = String(node.attrs.label)
      link.dataset.footnoteLink = String(node.attrs.label)
      link.textContent = `[^${node.attrs.label}]`
      dom.append(link)
      return { dom, ignoreMutation: () => true }
    }
  },
  markdownTokenizer: {
    name: 'footnoteReference', level: 'inline', start: source => source.indexOf('[^'),
    tokenize(source) {
      // Explicit and reference-style links keep their ordinary link labels.
      const match = /^\[\^([^\]\s]{1,200})\](?!\(|\[)/u.exec(source)
      if (match) return { type: 'footnoteReference', raw: match[0], label: match[1] }
    },
  },
  parseMarkdown: (token: FootnoteToken) => ({ type: 'footnoteReference', attrs: { label: token.label } }),
  renderMarkdown: node => `[^${node.attrs?.label ?? ''}]`,
})

export const MarkdownFootnoteDefinition = Node.create({
  name: 'footnoteDefinition', group: 'block', content: 'block+', defining: true,
  addAttributes: () => ({ label: { default: '' } }),
  parseHTML: () => [{ tag: 'section[data-markdown-footnote-definition]', getAttrs: element => ({ label: element.getAttribute('data-markdown-footnote-definition') }) }],
  renderHTML: ({ node }) => ['section', { 'data-markdown-footnote-definition': node.attrs.label, 'aria-label': `Footnote ${node.attrs.label}` }, 0],
  markdownTokenizer: {
    name: 'footnoteDefinition', level: 'block',
    start: source => /(?:^|\n)\[\^[^\]\s]{1,200}\]:/u.exec(source)?.index ?? -1,
    tokenize(source, _tokens, lexer) {
      const match = /^\[\^([^\]\s]{1,200})\]:[ \t]*([^\n]*)(?:\n|$)/u.exec(source)
      if (!match) return
      let raw = match[0], body = match[2] ?? ''
      const rest = source.slice(raw.length).split('\n')
      for (let index = 0; index < rest.length; index++) {
        const line = rest[index] ?? ''
        if (/^(?: {4}|\t)/.test(line)) {
          raw += line + (index < rest.length - 1 ? '\n' : '')
          body += '\n' + line.replace(/^(?: {4}|\t)/, '')
        } else if (!line.trim() && /^(?: {4}|\t)/.test(rest[index + 1] ?? '')) {
          raw += line + '\n'; body += '\n'
        } else break
      }
      return { type: 'footnoteDefinition', raw, label: match[1], tokens: lexer.blockTokens(body) }
    },
  },
  parseMarkdown: (token: FootnoteToken, helpers) => ({ type: 'footnoteDefinition', attrs: { label: token.label }, content: helpers.parseChildren(token.tokens ?? []) }),
  renderMarkdown: (node, helpers) => {
    const body = helpers.renderChildren(node.content ?? [], '\n\n').trimEnd()
    return `[^${node.attrs?.label ?? ''}]: ${body.replace(/\n/g, '\n    ')}`
  },
})
