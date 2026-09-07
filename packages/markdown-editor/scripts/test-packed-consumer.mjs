import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { spawn } from 'node:child_process'

const packageRoot = new URL('..', import.meta.url).pathname.replace(/\/$/, '')

function run(command, args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      env: { ...process.env, CI: '1' },
      stdio: 'inherit',
    })
    child.once('error', reject)
    child.once('exit', (code, signal) => {
      if (signal) reject(new Error(`${command} terminated by ${signal}`))
      else if (code !== 0) reject(new Error(`${command} exited with ${code}`))
      else resolve()
    })
  })
}

await run('pnpm', ['run', 'build'], packageRoot)

const tempRoot = await mkdtemp(join(tmpdir(), 'akb-markdown-editor-consumer-'))
const packDir = join(tempRoot, 'pack')
const consumerDir = join(tempRoot, 'consumer')
await Promise.all([
  import('node:fs/promises').then(({ mkdir }) => mkdir(packDir, { recursive: true })),
  import('node:fs/promises').then(({ mkdir }) => mkdir(consumerDir, { recursive: true })),
])

try {
  await run('pnpm', ['pack', '--pack-destination', packDir], packageRoot)
  const tarball = (await readdir(packDir)).find(name => name.endsWith('.tgz'))
  if (!tarball) throw new Error('pnpm pack did not create a tarball')

  const tarballPath = join(packDir, tarball)
  const packageJson = {
    name: 'markdown-editor-packed-consumer',
    private: true,
    type: 'module',
    scripts: { build: 'vite build', typecheck: 'tsc --noEmit' },
    dependencies: {
      '@akb/markdown-editor': `file:${tarballPath}`,
      react: '19.2.4',
      'react-dom': '19.2.4',
    },
    devDependencies: {
      '@types/react': '19.2.14',
      '@types/react-dom': '19.2.3',
      '@vitejs/plugin-react': '6.0.1',
      typescript: '5.9.3',
      vite: '8.0.3',
    },
  }
  await writeFile(join(consumerDir, 'package.json'), `${JSON.stringify(packageJson, null, 2)}\n`)
  await writeFile(
    join(consumerDir, 'tsconfig.json'),
    `${JSON.stringify(
      {
        compilerOptions: {
          target: 'ES2022',
          module: 'NodeNext',
          moduleResolution: 'NodeNext',
          jsx: 'react-jsx',
          strict: true,
          skipLibCheck: true,
          noEmit: true,
        },
        include: ['src'],
      },
      null,
      2,
    )}\n`,
  )
  await writeFile(
    join(consumerDir, 'vite.config.ts'),
    "import react from '@vitejs/plugin-react'\nimport { defineConfig } from 'vite'\nexport default defineConfig({ plugins: [react()] })\n",
  )
  await import('node:fs/promises').then(({ mkdir }) => mkdir(join(consumerDir, 'src'), { recursive: true }))
  await writeFile(
    join(consumerDir, 'index.html'),
    '<div id="root"></div><script type="module" src="/src/main.tsx"></script>\n',
  )
  await writeFile(
    join(consumerDir, 'src/main.tsx'),
    `import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MarkdownEditor, MarkdownViewer, canonicalizeMarkdown, parseMarkdown } from '@akb/markdown-editor'

const source = '# Packed consumer'
const parsed = parseMarkdown(source)
const canonical = canonicalizeMarkdown(source)

function App() {
  const [markdown, setMarkdown] = useState(canonical)
  return <main data-node-type={parsed.type}><MarkdownEditor markdown={markdown} onChange={setMarkdown} /><MarkdownViewer markdown={markdown} /></main>
}

createRoot(document.getElementById('root')!).render(<App />)
`,
  )

  await run('pnpm', ['install', '--no-frozen-lockfile', '--ignore-scripts'], consumerDir)
  await run('pnpm', ['run', 'typecheck'], consumerDir)
  await run('pnpm', ['run', 'build'], consumerDir)

  const buildIndex = await readFile(join(consumerDir, 'dist/index.html'), 'utf8')
  if (!buildIndex.includes('/assets/')) {
    throw new Error('Packed consumer build did not emit Vite assets')
  }
} finally {
  await rm(tempRoot, { recursive: true, force: true })
}
