import { expect, test } from "@playwright/test";

for (const width of [1440, 2560, 375]) {
  test(`Account settings workspace ${width}px`, async ({ page }, testInfo) => {
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
      return route.fulfill({ json: { tokens: [], users: [], items: [], unread_count: 0, supported: true } });
    });
    await page.goto("/settings");
    await expect(page.getByLabel("Display name", { exact: true })).toBeVisible();
    await expect(page.getByTestId("profile-identity")).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("profile.png"), fullPage: true, animations: "disabled" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width >= 1024) {
      const settingsLink = page.getByTestId("app-sidebar").getByRole("link", { name: "Settings", exact: true });
      await expect(settingsLink).toHaveAttribute("aria-current", "page");
      expect((await settingsLink.boundingBox())!.height).toBe(40);
      const support = (await page.getByRole("navigation", { name: "Workspace support" }).boundingBox())!;
      expect(support.y + support.height).toBe(1000);
      const rail = (await page.getByRole("complementary", { name: "Settings navigation" }).boundingBox())!;
      expect(rail.y).toBe(0);
      expect(rail.width).toBe(220);
      expect((await page.locator("header.app-header").boundingBox())!.x).toBe(rail.x + rail.width);
      await page.getByLabel("Display name", { exact: true }).fill("Unsaved name");
      await page.getByRole("tab", { name: "Appearance", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "Discard unsaved changes?" });
      await expect(dialog).toBeVisible();
      await dialog.getByRole("button", { name: "Keep editing" }).click();
      await expect(page.getByLabel("Display name", { exact: true })).toHaveValue("Unsaved name");
      await page.getByRole("tab", { name: "Appearance", exact: true }).click();
      await page.getByRole("button", { name: "Discard changes", exact: true }).click();
    } else {
      await page.getByRole("button", { name: "Settings section" }).click();
      await page.getByRole("menuitemradio", { name: "Appearance", exact: true }).click();
    }
    await expect(page.getByRole("radio", { name: "Dark", exact: true })).toBeVisible();
    if (width >= 1024) await expect(page.getByRole("tab", { name: "Appearance", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.screenshot({ path: testInfo.outputPath("settings-light.png"), fullPage: true, animations: "disabled" });
    await page.getByRole("radio", { name: "Dark", exact: true }).locator("..").click();
    await expect(page.locator("html")).toHaveClass(/dark/);
    await expect(page.getByTestId("settings-workspace")).toHaveCSS("background-color", "rgb(18, 24, 33)");
    await page.screenshot({ path: testInfo.outputPath("settings-dark.png"), fullPage: true, animations: "disabled" });
    if (width >= 1024) {
      await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
      const settingsLink = page.getByTestId("app-sidebar").getByRole("link", { name: "Settings", exact: true });
      await expect(settingsLink).toHaveAttribute("aria-current", "page");
      await expect(settingsLink).toHaveAttribute("href", "/settings?tab=preferences");
      expect((await settingsLink.boundingBox())!.height).toBe(40);
      await settingsLink.hover();
      await expect(page.getByRole("tooltip", { name: "Settings", exact: true })).toBeVisible();
      await settingsLink.click();
      await expect(page.getByRole("radio", { name: "Dark", exact: true })).toBeChecked();
      await page.screenshot({ path: testInfo.outputPath("settings-compact.png"), fullPage: true, animations: "disabled" });
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.goto("/settings?tab=tokens");
    await expect(page.getByRole("heading", { name: "1. Prepare access" })).toBeVisible();
    await expect(page.getByRole("heading", { name: /2. Configure/ })).toBeVisible();
    await expect(page.getByRole("heading", { name: "3. Try it in your agent" })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("agent-connections.png"), fullPage: true, animations: "disabled" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
