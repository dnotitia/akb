import { expect, test, type Locator, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns read-only HTTP fixtures; never writes user data.");

const vaults = Array.from({ length: 40 }, (_, index) => `team-${String(index + 1).padStart(2, "0")}-production-knowledge-base`);

async function mockWorkspace(page: Page) {
  await page.addInitScript(() => localStorage.setItem("akb_token", "search-density-fixture"));
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "scope-density-reader", username: "reader", display_name: "Reader", email: "reader@example.invalid", auth_method: "local", is_admin: false } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: vaults.map(name => ({ id: name, name, role: "reader" })) } });
    if (path.endsWith("/search")) return route.fulfill({ json: { results: [], total: 0, returned: 0 } });
    return route.fulfill({ json: { items: [], history: [], relations: [], subscribed: false, unread_count: 0 } });
  });
}

async function assertQuerySpace(scope: Locator, query: Locator, count: number, width: number) {
  await expect(scope.getByRole("button", { name: /^Remove .* from search scope$/ })).toHaveCount(Math.min(count, 2));
  await expect(query).toBeInViewport();
  const scopeBox = (await scope.boundingBox())!;
  const queryBox = (await query.boundingBox())!;
  expect(scopeBox.height).toBeLessThanOrEqual(44);
  expect(queryBox.width).toBeGreaterThanOrEqual(width === 375 ? 120 : 240);
  if (width > 375) {
    // Selected scopes cannot turn the desktop field into a stack.
    expect(Math.abs(queryBox.y + queryBox.height / 2 - scopeBox.y - scopeBox.height / 2)).toBeLessThanOrEqual(4);
  } else {
    expect(queryBox.y).toBeGreaterThanOrEqual(scopeBox.y + scopeBox.height);
    expect(queryBox.y + queryBox.height - scopeBox.y).toBeLessThanOrEqual(88);
  }
}

for (const width of [375, 1440, 2560]) {
  test(`Search scope preserves query width with 2, 3 and 40 long Vault names at ${width}px`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await mockWorkspace(page);
    for (const count of [2, 3, 40]) {
      await page.goto(`/search?q=guide&v=${encodeURIComponent(vaults.slice(0, count).join(","))}`);
      const query = page.getByRole("searchbox", { name: "Search query", exact: true });
      const scope = page.getByRole("group", { name: "Vault search scope" });
      await expect(query).toHaveValue("guide");
      await assertQuerySpace(scope, query, count, width);
      if (count > 2) await expect(scope.getByText(`+${count - 2}`, { exact: true })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await page.screenshot({ path: testInfo.outputPath("page-forty-scopes.png") });

    await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: /^Search scope:/ }).click();
    for (const name of vaults) await page.getByRole("menuitemcheckbox", { name, exact: true }).click();
    await page.keyboard.press("Escape");
    const query = dialog.getByRole("combobox");
    await query.fill("guide");
    const scope = dialog.getByRole("group", { name: "Vault search scope" });
    await assertQuerySpace(scope, query, 40, width);
    await expect(scope.getByText("+38", { exact: true })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Close search" })).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath("modal-forty-scopes.png") });
    await scope.getByRole("button", { name: /^Search scope:/ }).click();
    await page.getByRole("searchbox", { name: "Filter vaults" }).fill("team-40");
    const last = page.getByRole("menuitemcheckbox", { name: vaults[39], exact: true });
    await expect(last).toHaveAttribute("aria-checked", "true");
    await last.click();
    await page.keyboard.press("Escape");
    await expect(scope.getByText("+37", { exact: true })).toBeVisible();
    await expect(query).toHaveValue("guide");
  });
}
