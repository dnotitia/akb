import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['test/**/*.test.{ts,tsx}'],
    exclude: ['test/browser/**'],
    setupFiles: ['./test/setup.ts'],
    restoreMocks: true,
  },
})
