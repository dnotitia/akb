import { mkdtemp, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { spawn } from 'node:child_process'

const packageRoot = new URL('..', import.meta.url).pathname.replace(/\/$/, '')

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : undefined
      server.close(error => {
        if (error) reject(error)
        else if (port) resolve(port)
        else reject(new Error('Could not allocate a browser test port'))
      })
    })
  })
}

function run(command, args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: packageRoot,
      env: { ...process.env, ...env },
      stdio: 'inherit',
    })
    child.once('error', reject)
    child.once('exit', (code, signal) => {
      if (signal) reject(new Error(`Browser tests terminated by ${signal}`))
      else resolve(code ?? 1)
    })
  })
}

const tempRoot = await mkdtemp(join(tmpdir(), 'akb-markdown-editor-browser-'))
const port = await freePort()
const outputDir = join(tempRoot, 'playwright')
const cacheDir = join(tempRoot, 'vite-cache')

try {
  const exitCode = await run(
    'pnpm',
    ['exec', 'playwright', 'test', '--config', 'playwright.config.ts'],
    {
      MARKDOWN_EDITOR_TEST_PORT: String(port),
      PLAYWRIGHT_OUTPUT_DIR: outputDir,
      VITE_CACHE_DIR: cacheDir,
    },
  )
  process.exitCode = exitCode
} finally {
  await rm(tempRoot, { recursive: true, force: true })
}
