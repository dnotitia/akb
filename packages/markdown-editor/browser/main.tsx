import { useState } from 'react'
import { createRoot } from 'react-dom/client'

import { MarkdownEditor, MarkdownViewer } from '../src/index.js'

const initialMarkdown = '# 시작\n\n본문'

function App() {
  const [markdown, setMarkdown] = useState(initialMarkdown)
  const [slashCount, setSlashCount] = useState(0)

  return (
    <main>
      <h1>Markdown editor conformance</h1>
      <p data-testid="slash-count">slash:{slashCount}</p>
      <output data-testid="markdown-output">{markdown}</output>
      <section aria-label="editable markdown">
        <MarkdownEditor
          markdown={markdown}
          onChange={setMarkdown}
          onSlash={() => setSlashCount(count => count + 1)}
          aria-label="Markdown editor"
        />
      </section>
      <section aria-label="read-only markdown">
        <MarkdownViewer markdown={markdown} aria-label="Markdown viewer" />
      </section>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
