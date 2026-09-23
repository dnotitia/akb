import { readFile } from 'node:fs/promises'

const declarationPaths = ['dist/index.d.ts', 'dist/react/index.d.ts']
const forbiddenDeclarationPatterns = [
  /@tiptap\//,
  /\bEditorContent\b/,
  /\bserializeEditorMarkdown\b/,
  /\bcreateMarkdownEditor\b/,
  /\bmarkdownCommands\b/,
  /\beditorMarkdown\b/,
  /\bJSONContent\b/,
  /\bEditorOptions\b/,
  /\bFocusPosition\b/,
]

for (const path of declarationPaths) {
  const declaration = await readFile(path, 'utf8')
  for (const pattern of forbiddenDeclarationPatterns) {
    if (pattern.test(declaration)) {
      throw new Error(`${path} exposes a forbidden engine contract: ${pattern}`)
    }
  }
}

const root = await import('../dist/index.js')
const react = await import('../dist/react/index.js')
const forbiddenRuntimeExports = new Set([
  'EditorContent',
  'createMarkdownEditor',
  'createMarkdownExtensions',
  'createMarkdownReferenceExtension',
  'createMarkdownSlashCommandExtension',
  'editorMarkdown',
  'markdownCommands',
  'MarkdownImage',
  'MarkdownReference',
  'RawMarkdownBlock',
  'RawMarkdownInline',
  'serializeEditorMarkdown',
])

for (const [entry, exports] of Object.entries({ root, react })) {
  for (const name of forbiddenRuntimeExports) {
    if (name in exports) {
      throw new Error(`${entry} exposes a forbidden runtime export: ${name}`)
    }
  }
}

for (const name of [
  'parseMarkdown',
  'serializeMarkdown',
  'canonicalizeMarkdown',
  'extractMarkdownTargets',
  'MarkdownEditor',
  'MarkdownSurface',
  'MarkdownEditingSurface',
  'MarkdownToolbar',
  'useMarkdownEditor',
]) {
  if (!(name in root)) throw new Error(`root is missing the maintained export: ${name}`)
}

console.log('public API declarations and runtime exports are closed')
