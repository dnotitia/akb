import { expect, test } from "@playwright/test";

for (const width of [375, 767, 768, 874, 1024, 1440, 2560]) {
  test(`Account settings dialog ${width}px`, async ({ page }, testInfo) => {
    test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(() => {
      localStorage.setItem("akb_token", "settings-fixture");
      localStorage.setItem("akb_theme", "light");
    });
    await page.route("**/api/v1/**", route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
      if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "settings-user", username: "fixture", display_name: "Example User", email: "fixture@example.invalid", is_admin: true, auth_method: "local" } });
      if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [] } });
      // Model a legacy server explicitly; a generic 200 is an invalid contract
      // and correctly moves focus to the issuance error summary.
      if (path.endsWith("/auth/tokens/capabilities")) return route.fulfill({ status: 404, json: { detail: "Not Found" } });
      return route.fulfill({ json: { tokens: [], users: [], items: [], unread_count: 0, supported: true } });
    });
    await page.goto("/settings");
    await expect(page.getByLabel("Display name", { exact: true })).toBeVisible();
    await expect(page.getByTestId("profile-identity")).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("profile.png"), fullPage: true, animations: "disabled" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width >= 768) {
      const dialog = page.getByTestId("account-settings-dialog");
      const bounds = (await dialog.boundingBox())!;
      const rail = (await page.getByRole("complementary", { name: "Settings navigation" }).boundingBox())!;
      expect(rail.y).toBeGreaterThan(bounds.y);
      expect(rail.width).toBe(208);
      expect(bounds.width).toBeLessThanOrEqual(1040);
      await page.getByLabel("Display name", { exact: true }).fill("Unsaved name");
      await page.getByRole("tab", { name: "Appearance", exact: true }).click();
      const confirmation = page.getByRole("dialog", { name: "Discard unsaved changes?" });
      await expect(confirmation).toBeVisible();
      await confirmation.getByRole("button", { name: "Keep editing" }).click();
      await expect(page.getByLabel("Display name", { exact: true })).toHaveValue("Unsaved name");
      await page.getByRole("tab", { name: "Appearance", exact: true }).click();
      await page.getByRole("button", { name: "Discard changes", exact: true }).click();
    } else {
      await page.getByRole("button", { name: "Settings section" }).click();
      await page.getByRole("menuitemradio", { name: "Appearance", exact: true }).click();
    }
    await expect(page.getByRole("radio", { name: "Dark", exact: true })).toBeVisible();
    if (width >= 768) await expect(page.getByRole("tab", { name: "Appearance", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.screenshot({ path: testInfo.outputPath("settings-light.png"), fullPage: true, animations: "disabled" });
    await page.getByRole("radio", { name: "Dark", exact: true }).locator("..").click();
    await expect(page.locator("html")).toHaveClass(/dark/);
    // Compare both surfaces in one settled read during the theme change.
    await expect.poll(() => page.getByTestId("account-settings-dialog").evaluate(node =>
      getComputedStyle(node).backgroundColor === getComputedStyle(document.body).backgroundColor)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("settings-dark.png"), fullPage: true, animations: "disabled" });
    if (width >= 1024) {
      await page.getByRole("button", { name: "Close settings", exact: true }).click();
      await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
      const settingsButton = page.getByTestId("app-sidebar").getByRole("button", { name: "Settings", exact: true });
      await expect(settingsButton).toHaveAttribute("aria-haspopup", "dialog");
      expect((await settingsButton.boundingBox())!.height).toBe(40);
      await settingsButton.hover();
      await expect(page.getByRole("tooltip", { name: "Settings", exact: true })).toBeVisible();
      await settingsButton.click();
      await page.getByRole("tab", { name: "Appearance", exact: true }).click();
      await expect(page.getByRole("radio", { name: "Dark", exact: true })).toBeChecked();
      await page.screenshot({ path: testInfo.outputPath("settings-compact.png"), fullPage: true, animations: "disabled" });
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.goto("/settings?tab=tokens");
    await expect(page.getByRole("heading", { name: "1. Choose your AI tool" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 3 })).toHaveCount(1);
    await page.getByRole("button", { name: "Continue with Claude Code", exact: true }).click();
    await expect(page.getByRole("heading", { name: "2. Add AKB to Claude Code" })).toBeFocused();
    await page.getByRole("radio", { name: "Use an existing key", exact: true }).locator("..").click();
    const key = page.getByLabel("Full connection key", { exact: true });
    await key.fill("akb_settings_fixture_key");
    await page.getByRole("button", { name: "Check in Claude Code", exact: true }).click();
    await expect(page.getByRole("heading", { name: "3. Check the connection" })).toBeFocused();
    await expect(page.getByRole("heading", { level: 3 })).toHaveCount(1);
    await expect(page.getByRole("checkbox", { name: "I received an AKB response in my AI tool" })).not.toBeChecked();
    await page.getByRole("button", { name: "Back to setup", exact: true }).click();
    await expect(key).toHaveValue("akb_settings_fixture_key");
    await page.screenshot({ path: testInfo.outputPath("ai-connections.png"), fullPage: true, animations: "disabled" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
