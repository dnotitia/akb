import { defineConfig, devices } from "@playwright/test";

const mode = process.env.AKB_FE_E2E_MODE;
if (mode !== "mock" && mode !== "real") {
  throw new Error("Set AKB_FE_E2E_MODE to mock or real before running Playwright");
}

const mockMode = mode === "mock";
if (!mockMode && !process.env.AKB_FRONTEND_URL) {
  throw new Error("Real Playwright mode requires AKB_FRONTEND_URL from the runtime descriptor");
}
const baseURL = mockMode
  ? "http://127.0.0.1:4173"
  : process.env.AKB_FRONTEND_URL!;
const recoveryScenario = process.env.CRABBOX_RUNTIME_SCENARIO === "document-edit-recovery";

// Mock mode owns its Vite webServer and browser MSW worker. Real mode consumes
// the already-ready frontend origin from the repository runtime descriptor.
//
// We deliberately keep the suite small (`smoke.spec.ts`) — slow E2E
// loops kill iteration speed. Rich coverage lives in vitest+RTL +
// MSW. Playwright's job is "the build still actually works in a
// browser end-to-end" — not exhaustive UX coverage.
export default defineConfig({
  testDir: "./e2e",
  ...(recoveryScenario ? { testMatch: /document-edit-recovery\.spec\.ts/ } : {}),
  fullyParallel: false,                  // single backend, serialize
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  ...(mockMode
    ? {
        webServer: {
          command:
            "VITE_AKB_TEST_MODE=mock pnpm run dev --host 127.0.0.1 --port 4173 --strictPort",
          url: baseURL,
          reuseExistingServer: false,
          timeout: 120_000,
        },
      }
    : {}),
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
