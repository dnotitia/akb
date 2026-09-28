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
  /\bDEFAULT_MARKDOWN_(?:SLASH_COMMAND_MESSAGES|REFERENCE_LABELS|IMAGE_(?:MENU|UPLOAD)_LABELS|TABLE_LABELS)\b/,
  /\bMarkdown(?:Code|Image|ImageMenu|ImageUpload|Reference|SlashCommand|Table|Link)Labels\b/,
  /\bMarkdown(?:CodeOptions|SlashCommandMessages)\b/,
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
  if (!('MarkdownLocaleProvider' in exports)) {
    throw new Error(`${entry} is missing MarkdownLocaleProvider`)
  }
  for (const name of forbiddenRuntimeExports) {
    if (name in exports) {
      throw new Error(`${entry} exposes a forbidden runtime export: ${name}`)
    }
  }
}

for (const path of declarationPaths) {
  const declaration = await readFile(path, 'utf8')
  const hasLocaleContract = path === 'dist/react/index.d.ts'
    ? /MarkdownLocaleProvider/.test(declaration) && /MarkdownLocale/.test(declaration)
    : /react\/index/.test(declaration)
  if (!hasLocaleContract) {
    throw new Error(`${path} is missing the public locale provider contract`)
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
  'MarkdownLocaleProvider',
  'useMarkdownEditor',
]) {
  if (!(name in root)) throw new Error(`root is missing the maintained export: ${name}`)
}

console.log('public API declarations and runtime exports are closed')
