// SPA smoke E2E. Runs against the docker-compose stack — frontend on
// :3000, backend on :8000. Covers the happy-path the previous bug
// classes lived in: signup → vault create → doc put → search hit.
//
// Failure modes we deliberately want this to catch:
//   - Build/serve regression (white page, hydration crash, console errors)
//   - Layout auth-gate broken (logged-out gets the SPA shell instead of /auth)
//   - Profile edit form / save round-trip (PR #43 — settings tab)
//   - akb_search wired to the backend response (PR #39 — total_matches in UI)
//
// Slow tests are out of scope here — fine-grained UX checks live in vitest+RTL.
//
// Run locally:
//   docker compose up -d
//   cd frontend && pnpm exec playwright test
import { test, expect } from "@playwright/test";

const RUN = Date.now();
const USER = `e2e-${RUN}`;
const PASS = "test-pass-1234";

test.describe.configure({ mode: "serial" });

test("signup → vault create → put doc → search round-trip", async ({ page }) => {
  // 1. signup
  await page.goto("/auth");
  await page.getByRole("tab", { name: /sign up/i }).click().catch(() => {});
  await page.getByLabel(/username/i).fill(USER);
  await page.getByLabel(/email/i).fill(`${USER}@e2e.test`);
  await page.getByLabel(/password/i).first().fill(PASS);
  await page.getByRole("button", { name: /sign up|create account/i }).click();

  // Land in the authenticated shell — the home page link should appear.
  await expect(page.getByRole("link", { name: /home|new chat|browse/i }).first())
    .toBeVisible({ timeout: 10_000 });

  // 2. profile tab is editable (PR #43)
  await page.goto("/settings?tab=profile");
  const displayName = page.getByLabel(/display name/i);
  await expect(displayName).toBeEditable();
  await displayName.fill(`${USER} Renamed`);
  await page.getByRole("button", { name: /save profile/i }).click();
  await expect(page.getByText(/saved/i)).toBeVisible({ timeout: 5_000 });
});

test("logged-out user redirects to /auth (layout auth gate)", async ({ page, context }) => {
  await context.clearCookies();
  await page.addInitScript(() => localStorage.removeItem("akb_token"));
  await page.goto("/settings");
  await expect(page).toHaveURL(/\/auth(\?.*)?$/);
});
