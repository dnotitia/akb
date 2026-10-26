import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Isolated HTTP fixtures; no backend writes.");

async function directoryFixture(page: Page, dark: boolean) {
  await page.addInitScript(value => {
    localStorage.setItem("akb_token", "composition-fixture");
    localStorage.setItem("akb_theme", value ? "dark" : "light");
    localStorage.setItem("akb_app_sidebar_compact", "false");
  }, dark);
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "composition-user", username: "fixture", display_name: "Workspace Reviewer", is_admin: false, auth_method: "local" } });
    if (path.endsWith("/my/vaults")) return route.fulfill({ json: { vaults: Array.from({ length: 65 }, (_, index) => ({ id: `vault-${index}`, name: `workspace-${String(index).padStart(2, "0")}`, role: index % 2 ? "reader" : "owner" })) } });
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], tokens: [], unread_count: 0, supported: true } });
  });
}

for (const width of [390, 1440]) for (const dark of [false, true]) {
  test(`vault directory remains usable at ${width}px in ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await directoryFixture(page, dark);
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/vault");
    const directory = page.getByRole("region", { name: "Your vaults" });
    const list = directory.getByRole("list", { name: "Available vaults" });
    await expect(list.getByRole("link")).toHaveCount(30);
    await list.getByRole("link").last().scrollIntoViewIfNeeded();
    await expect(list.getByRole("link")).toHaveCount(60);
    await directory.getByRole("textbox", { name: "Find a vault" }).fill("workspace-64");
    await expect(list.getByRole("link")).toHaveCount(1);
    await expect(list.getByRole("link", { name: "workspace-64" })).toHaveAttribute("href", "/vault/workspace-64");
    await directory.getByRole("button", { name: "Favorites", exact: true }).click();
    await expect(directory.getByText("No vaults match these filters")).toBeVisible();
    await directory.getByRole("button", { name: "Show all vaults", exact: true }).click();
    await expect(list.getByRole("link")).toHaveCount(30);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("vault-directory.png") });
  });
}

test("workspace sidebar keeps brand and support icon anchors stable", async ({ page }, testInfo) => {
  await directoryFixture(page, false);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/search");
  const expand = page.getByRole("button", { name: "Expand sidebar", exact: true });
  if (await expand.isVisible()) await expand.click();
  const mark = page.getByRole("link", { name: "AKB home — Agent Knowledgebase", exact: true }).locator(".brand-mark");
  const help = page.getByRole("button", { name: "Help", exact: true }).locator("svg");
  const settings = page.getByRole("button", { name: "Settings", exact: true }).locator("svg");
  await expect(mark).toBeVisible();
  const before = await Promise.all([mark, help, settings].map(locator => locator.boundingBox()));
  await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
  await expect(page.getByRole("button", { name: "Expand sidebar", exact: true })).toBeVisible();
  const after = await Promise.all([mark, help, settings].map(locator => locator.boundingBox()));
  for (let index = 0; index < before.length; index++) {
    expect(Math.abs(before[index]!.x - after[index]!.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(before[index]!.y - after[index]!.y)).toBeLessThanOrEqual(1);
  }
  await page.getByRole("button", { name: "Help", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Getting around AKB" });
  await expect(dialog).toContainText("Sidebar filters");
  await dialog.press("Escape");
  await expect(page.getByRole("button", { name: "Help", exact: true })).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("workspace-collapsed.png") });
});
