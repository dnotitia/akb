import { defineConfig, devices } from "@playwright/test";

// Playwright runs the SPA in a real Chromium against a running backend.
// Local: `docker compose up -d` first, then `pnpm exec playwright test`.
// CI: same compose stack starts before this config picks up.
//
// We deliberately keep the suite small (`smoke.spec.ts`) — slow E2E
// loops kill iteration speed. Rich coverage lives in vitest+RTL +
// MSW. Playwright's job is "the build still actually works in a
// browser end-to-end" — not exhaustive UX coverage.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,                  // single backend, serialize
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: process.env.AKB_FRONTEND_URL || "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
