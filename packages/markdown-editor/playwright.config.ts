import { defineConfig } from '@playwright/test'

const port = Number(process.env.MARKDOWN_EDITOR_TEST_PORT ?? 4173)
const baseURL = `http://127.0.0.1:${port}/browser/`

export default defineConfig({
  testDir: './test/browser',
  timeout: 30_000,
  fullyParallel: true,
  reporter: 'line',
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? 'test-results',
  webServer: {
    command: `pnpm exec vite --host 127.0.0.1 --port ${port} --strictPort`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
  use: {
    baseURL,
    headless: true,
  },
})
