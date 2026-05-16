// SPA smoke E2E. Runs against the docker-compose stack — frontend on
// :3000, backend on :8000. Covers the happy-path the previous bug
// classes lived in: signup → land in shell → profile edit round-trip.
//
// Failure modes this should catch:
//   - Build/serve regression (white page, hydration crash, console errors)
//   - Layout auth-gate broken (logged-out gets SPA shell instead of /auth)
//   - Profile edit form / save round-trip (PR #43 — settings tab)
//
// Selector notes (from frontend/src/pages/auth.tsx, settings.tsx):
//   - Tabs render "Log in" / "Register" — NOT "Sign in/up".
//   - Submit button text is "Create Account" (register) or
//     "Enter the Base" (login). During submit it flips to "Signing in…".
//   - Profile labels are uppercase ("DISPLAY NAME", "EMAIL").
//   - Save confirmation is the literal string "Saved" (not "saved").
//
// Run locally:
//   docker compose up -d
//   cd frontend && pnpm exec playwright test
import { test, expect } from "@playwright/test";

const RUN = Date.now();
const USER = `e2e-${RUN}`;
const PASS = "test-pass-1234";

test.describe.configure({ mode: "serial" });

test("signup → land in shell → profile edit round-trip", async ({ page }) => {
  // ── 1. Register ────────────────────────────────────────────
  await page.goto("/auth");
  await page.getByRole("tab", { name: /^register$/i }).click();
  await page.getByLabel("Username").fill(USER);
  await page.getByLabel("Email").fill(`${USER}@e2e.test`);
  await page.getByLabel("Password").fill(PASS);
  await page.getByRole("button", { name: /create account/i }).click();

  // The authenticated shell sets up nav links — "Home" is the
  // first NavLink in components/layout.tsx.
  await expect(page.getByRole("link", { name: "Home" })).toBeVisible({
    timeout: 10_000,
  });

  // ── 2. Profile edit (PR #43 regression guard) ──────────────
  await page.goto("/settings?tab=profile");
  const displayName = page.getByLabel("DISPLAY NAME");
  await expect(displayName).toBeEditable();
  await displayName.fill(`${USER} Renamed`);
  await page.getByRole("button", { name: /save profile/i }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible({
    timeout: 5_000,
  });
});

test("logged-out user redirects to /auth (layout auth gate)", async ({
  page,
  context,
}) => {
  await context.clearCookies();
  await page.addInitScript(() => localStorage.removeItem("akb_token"));
  await page.goto("/settings");
  await expect(page).toHaveURL(/\/auth(\?.*)?$/);
});
