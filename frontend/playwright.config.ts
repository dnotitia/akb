import { defineConfig, devices, type PlaywrightTestConfig } from "@playwright/test";

const mode = process.env.AKB_FE_E2E_MODE;
if (mode !== "mock" && mode !== "fixture" && mode !== "real") {
  throw new Error("Set AKB_FE_E2E_MODE to mock, fixture or real before running Playwright");
}
if (mode === "real" && !process.env.AKB_FRONTEND_URL) {
  throw new Error("Real Playwright mode requires AKB_FRONTEND_URL from the runtime descriptor");
}
const ci = !!process.env.CI;
const minutes = (count: number) => count * 60 * 1000;

const smokeSpec = "**/smoke.spec.ts";
const optionalLiveSpecs = ["**/*-live.spec.ts", "**/mcp-monitoring-access.spec.ts"];
const mockScenarioSpecs = new Map([
  ["document-edit-recovery", "**/document-edit-recovery.spec.ts"],
  ["markdown-image-rendering", "**/markdown-image-rendering.spec.ts"],
  ["markdown-reference-adapters", "**/markdown-link-search.spec.ts"],
]);
const mockScenarioSpec = mockScenarioSpecs.get(process.env.AKB_FE_E2E_SCENARIO ?? "empty");

const fixtureBuild = "node_modules/.cache/akb-e2e-fixture";

// CI budgets: scripts/ci/README.md records the measured durations they come
// from. Per-test limits are unchanged, and an exhausted suite budget fails.
const modes: Record<"mock" | "fixture" | "real", PlaywrightTestConfig> = {
  mock: {
    use: { baseURL: "http://127.0.0.1:4173" },
    testMatch: mockScenarioSpec,
    testIgnore: mockScenarioSpec
      ? undefined
      : [...optionalLiveSpecs, ...mockScenarioSpecs.values()],
    webServer: {
      command: "pnpm run dev:e2e:mock",
      url: "http://127.0.0.1:4173/__akb_mock__/health",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    fullyParallel: false,
    workers: 1,
    globalTimeout: ci ? minutes(10) : undefined,
  },
  fixture: {
    use: { baseURL: "http://127.0.0.1:4174" },
    webServer: {
      command:
        `pnpm exec vite build --outDir ${fixtureBuild} --emptyOutDir && ` +
        `pnpm exec vite preview --outDir ${fixtureBuild} --host 127.0.0.1 --port 4174 --strictPort`,
      env: { AKB_FRONTEND_BACKEND_URL: "http://127.0.0.1:9" },
      url: "http://127.0.0.1:4174",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    testIgnore: [smokeSpec, ...optionalLiveSpecs, ...mockScenarioSpecs.values()],
    // Lets --shard split by test rather than by file. On four vCPUs, two to
    // four workers saved a third of the time at most and slowed each test
    // enough to break timing assertions, so the default is one worker and CI
    // scales by shards. Pass --workers to trade that risk for speed locally.
    fullyParallel: true,
    workers: 1,
    globalTimeout: ci ? minutes(20) : undefined,
  },
  real: {
    use: { baseURL: process.env.AKB_FRONTEND_URL },
    fullyParallel: false,
    workers: 1,
    globalTimeout: ci ? minutes(10) : undefined,
  },
};
const selected = modes[mode];
const chromium = { name: "chromium", use: devices["Desktop Chrome"] };

export default defineConfig({
  testDir: "./e2e",
  ...selected,
  forbidOnly: ci,
  retries: ci ? 1 : 0,
  reporter: ci ? "github" : "list",
  use: {
    ...selected.use,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: mode === "real"
    ? [
        { ...chromium, testMatch: smokeSpec },
        { ...chromium, name: "chromium-live-optional", testMatch: optionalLiveSpecs },
      ]
    : [chromium],
});
