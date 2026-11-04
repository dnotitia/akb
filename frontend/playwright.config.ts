import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, devices, type PlaywrightTestConfig } from "@playwright/test";

// A mode names the server its specs need:
//
// - mock: the Vite dev server with the browser MSW worker and the mock
//   reset/discovery endpoints. Every spec file loads here; the page.route
//   specs skip themselves because MSW answers instead. Its fixture state is
//   shared, so its specs run one at a time.
// - fixture: a production build served by `vite preview`, with no backend.
//   These specs answer their requests themselves through page.route, so they
//   shard across CI jobs. The /api proxy points at a closed port: a request a
//   spec does not answer gets a 502 and a "http proxy error" line on stderr
//   instead of a response from some backend.
// - real: the repository runtime's frontend and backend, at AKB_FRONTEND_URL.
//   Its specs write to that one backend, so they run one at a time.
//
// Fixture and real split the suite between them; mock still loads every spec.
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
// its HTTP and runs in fixture mode. A spec that depends on a backend response
// and is not listed here fails in fixture mode, where that response is a 502;
// a request no assertion depends on only leaves a proxy error line.
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

// A spec that skips itself outside real mode and is not listed above would
// run nowhere: fixture mode skips it and real mode never selects it. Refuse
// that instead of reporting a green run without it.
const e2eDir = fileURLToPath(new URL("./e2e/", import.meta.url));
const unlistedRealOnly = readdirSync(e2eDir)
  .filter((file) => file.endsWith(".spec.ts") && !serverStateMatch.test(`/${file}`))
  .filter((file) => /AKB_FE_E2E_MODE\s*!==\s*["']real["']/.test(readFileSync(e2eDir + file, "utf8")));
if (unlistedRealOnly.length > 0) {
  throw new Error(
    `${unlistedRealOnly.join(", ")} only run in real mode; add them to serverStateSpecs in playwright.config.ts`,
  );
}

// These scenarios read the mock server's fixture, so they select their spec
// in mock mode only.
const scenario = process.env.AKB_FE_E2E_SCENARIO;
const scenarioMatch =
  mode === "mock" &&
  (scenario === "document-edit-recovery" || scenario === "markdown-image-rendering")
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
