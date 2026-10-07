import { defineConfig, devices } from "@playwright/test";

// The login theme against a real Keycloak. run.sh starts and seeds it; this
// config only drives the browser. Every test makes its own Keycloak state, so
// they can run side by side against the one server.
export default defineConfig({
  testDir: ".",
  testMatch: /\.spec\.ts$/,
  outputDir: "../../test-results/keycloak-theme",
  fullyParallel: true,
  workers: process.env.CI ? 2 : 4,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? "github" : "list",
  timeout: 120_000,
  use: {
    baseURL: process.env.AKB_THEME_KEYCLOAK ?? "http://localhost:18480",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
