import { defineConfig, devices, type PlaywrightTestConfig } from "@playwright/test";

// A mode names the server a spec needs, and each spec belongs to one mode:
//
// - mock: the Vite dev server with the browser MSW worker and the mock
//   reset/discovery endpoints. Its fixture state is shared, so its specs run
//   one at a time.
// - fixture: a production build served by `vite preview`, with no backend.
//   These specs answer every request themselves through page.route, so they
//   shard across CI jobs. The /api proxy points at a closed port: a request a
//   spec does not answer gets a 502 and a "http proxy error" line on stderr
//   instead of reaching some backend.
// - real: the repository runtime's frontend and backend, at AKB_FRONTEND_URL.
//   Its specs write to that one backend, so they run one at a time.
const mode = process.env.AKB_FE_E2E_MODE;
if (mode !== "mock" && mode !== "fixture" && mode !== "real") {
  throw new Error("Set AKB_FE_E2E_MODE to mock, fixture or real before running Playwright");
}
if (mode === "real" && !process.env.AKB_FRONTEND_URL) {
  throw new Error("Real Playwright mode requires AKB_FRONTEND_URL from the runtime descriptor");
}
const ci = !!process.env.CI;
const minutes = (count: number) => count * 60 * 1000;

// Specs that need state outside the page: the runtime backend (real mode) or
// the mock server's shared fixture state (mock mode). Every other spec owns
// its HTTP and runs in fixture mode. A new spec that needs a backend and is
// not listed here fails in fixture mode on the closed proxy port.
const serverStateSpecs = [
  "smoke",
  "search-live",
  "pat-issuance-live",
  "account-security-live",
  "profile-photo-live",
  "document-edit-recovery",
  "markdown-image-rendering",
  "markdown-link-search",
];
const serverStateMatch = new RegExp(`/(${serverStateSpecs.join("|")})\\.spec\\.ts$`);

const scenario = process.env.AKB_FE_E2E_SCENARIO;
const scenarioMatch =
  scenario === "document-edit-recovery" || scenario === "markdown-image-rendering"
    ? new RegExp(`/${scenario}\\.spec\\.ts$`)
    : undefined;

const fixtureBuild = "node_modules/.cache/akb-e2e-fixture";

// CI budgets: scripts/ci/README.md records the measured durations they come
// from. Per-test limits are unchanged, and an exhausted suite budget fails.
const modes: Record<"mock" | "fixture" | "real", PlaywrightTestConfig> = {
  mock: {
    use: { baseURL: "http://127.0.0.1:4173" },
    webServer: {
      command: "VITE_AKB_TEST_MODE=mock pnpm run dev --host 127.0.0.1 --port 4173 --strictPort",
      url: "http://127.0.0.1:4173",
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
    testIgnore: serverStateMatch,
    // Lets --shard split by test rather than by file. On a 4-vCPU runner more
    // than one worker slowed each test enough to break timing assertions, so
    // CI scales by shards and keeps one worker per job.
    fullyParallel: true,
    workers: ci ? 1 : undefined,
    globalTimeout: ci ? minutes(20) : undefined,
  },
  real: {
    use: { baseURL: process.env.AKB_FRONTEND_URL },
    testMatch: serverStateMatch,
    fullyParallel: false,
    workers: 1,
    globalTimeout: ci ? minutes(10) : undefined,
  },
};
const selected = modes[mode];

export default defineConfig({
  testDir: "./e2e",
  ...selected,
  ...(scenarioMatch ? { testMatch: scenarioMatch, testIgnore: undefined } : {}),
  forbidOnly: ci,
  retries: ci ? 1 : 0,
  reporter: ci ? "github" : "list",
  use: {
    ...selected.use,
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
