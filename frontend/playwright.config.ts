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
const recoveryScenario = process.env.AKB_FE_E2E_SCENARIO === "document-edit-recovery";
const imageRenderingScenario = process.env.AKB_FE_E2E_SCENARIO === "markdown-image-rendering";

// Mock mode owns its Vite webServer and browser MSW worker. Real mode consumes
// the already-ready frontend origin from the repository runtime descriptor.
//
// The full browser regression covers the real runtime and isolated HTTP
// fixtures. Keep one worker and serialization for the shared backend.
export default defineConfig({
  testDir: "./e2e",
  ...(recoveryScenario
    ? { testMatch: /document-edit-recovery\.spec\.ts/ }
    : imageRenderingScenario
      ? { testMatch: /markdown-image-rendering\.spec\.ts/ }
      : {}),
  fullyParallel: false,                  // single backend, serialize
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // The serial real-runtime suite shares a 75-minute job with API setup, checks
  // and cleanup. Give the browser run its own deadline for diagnostic uploads.
  globalTimeout: process.env.CI ? (mockMode ? 40 : 50) * 60 * 1000 : undefined,
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
          command: "pnpm run dev:e2e:mock",
          url: `${baseURL}/__akb_mock__/health`,
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
