// The theme preference and Keycloak's fallback when a theme is missing.
import { expect, test } from "@playwright/test";
import { authUrl, signIn, watchErrors } from "./lib";

test("the app's theme hint survives a form post", async ({ page }) => {
  const settled = watchErrors(page);
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto(authUrl("akb-web", { theme: "dark" }));
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("html")).toHaveClass(/pf-v5-theme-dark/);
  // The failed sign-in posts the form; the hint is not on the next URL.
  await signIn(page, "ada", "not-the-password");
  await expect(page.locator("#input-error-username")).toBeVisible();
  expect(page.url()).not.toContain("ui_theme");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  settled();
});

test("the toggle cycles system, light, dark and remembers the choice", async ({ page }) => {
  const settled = watchErrors(page);
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto(authUrl("akb-web"));
  const html = page.locator("html");
  const toggle = page.locator("#akb-theme-toggle");
  await expect(html).toHaveAttribute("data-theme-preference", "system");
  await expect(html).toHaveAttribute("data-theme", "dark");
  await expect(html).toHaveAttribute("data-theme-control-ready", "true");
  await expect(toggle).toHaveAttribute("aria-label", "Theme: System. Change theme");

  await toggle.click();
  await expect(html).toHaveAttribute("data-theme", "light");
  await expect(toggle).toHaveAttribute("aria-label", "Theme: Light. Change theme");
  await toggle.click();
  await expect(html).toHaveAttribute("data-theme", "dark");
  await expect(html).toHaveAttribute("data-theme-preference", "dark");

  await page.emulateMedia({ colorScheme: "light" });
  await page.reload();
  await expect(html).toHaveAttribute("data-theme", "dark");
  await toggle.click();
  await expect(html).toHaveAttribute("data-theme-preference", "system");
  await expect(html).toHaveAttribute("data-theme", "light");
  expect(await page.evaluate(() => localStorage.getItem("akb_theme"))).toBeNull();
  settled();
});

test("a realm pointing at a missing theme still signs people in", async ({ page }) => {
  // Keycloak falls back to its built-in theme (and logs an error), so a
  // tenant whose Keycloak has not received the theme yet is not locked out.
  const response = await page.goto(authUrl("missing-theme"));
  expect(response?.status()).toBe(200);
  await expect(page.locator("#kc-form-login")).toBeVisible();
  await expect(page.locator(".akb-shell")).toHaveCount(0);
});
