import { expect, test, type Locator, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");

async function fixture(page: Page, { compact = false, dark = false } = {}) {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
  await page.addInitScript(({ compact, dark }) => {
    localStorage.setItem("akb_token", "sidebar-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    if (!localStorage.getItem("sidebar-fixture-initialized")) {
      localStorage.setItem("akb_app_sidebar_compact", String(compact));
      localStorage.setItem("sidebar-fixture-initialized", "true");
    }
  }, { compact, dark });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "sidebar-user", username: "reader", display_name: "Workspace reader", email: "reader@example.invalid", is_admin: false, auth_method: "local", key_class: null } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "team-id", name: "team", role: "owner" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: { name: "team", role: "owner", document_count: 1, collection_count: 1 } });
    if (path.includes("/browse/")) return route.fulfill({ json: { vault: "team", path: "", items: [{ type: "collection", name: "guides", path: "guides", doc_count: 1 }] } });
    return route.fulfill({ json: { tokens: [], items: [], changes: [], history: [], templates: [], relations: [], unread_count: 0 } });
  });
}

async function expectCompact(page: Page, compact: boolean) {
  const sidebar = page.getByTestId("app-sidebar");
  await expect(sidebar).toHaveAttribute("data-compact", String(compact));
  await expect(sidebar.getByRole("button", { name: compact ? "Expand sidebar" : "Collapse sidebar", exact: true })).toHaveAttribute("aria-expanded", String(!compact));
}

for (const startingRoute of ["/", "/search"]) {
  test(`re-entering Vaults resets its manual expansion from ${startingRoute}`, async ({ page }) => {
    await fixture(page);
    await page.goto(startingRoute);
    const sidebar = page.getByTestId("app-sidebar");
    await expectCompact(page, false);
    await sidebar.getByRole("link", { name: "Vaults", exact: true }).click();
    await expectCompact(page, true);
    await sidebar.getByRole("button", { name: "Expand sidebar", exact: true }).click();
    await expectCompact(page, false);
    await sidebar.getByRole("link", { name: startingRoute === "/" ? "Home" : "Search", exact: true }).click();
    await expect(sidebar.getByRole("link", { name: startingRoute === "/" ? "Home" : "Search", exact: true })).toHaveAttribute("aria-current", "page");
    await expectCompact(page, false);
    await sidebar.getByRole("link", { name: "Vaults", exact: true }).click();
    await expectCompact(page, true);
    expect(await page.evaluate(() => localStorage.getItem("akb_app_sidebar_compact"))).toBe("false");
  });
}

test("Vault expansion survives internal routes and history, then resets across the workspace boundary", async ({ page }) => {
  await fixture(page);
  await page.goto("/vault/team");
  const sidebar = page.getByTestId("app-sidebar");
  await expectCompact(page, true);
  const sections = page.getByRole("navigation", { name: "Vault sections" });
  await sidebar.getByRole("button", { name: "Expand sidebar", exact: true }).click();
  await sections.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/team\/settings$/);
  await expect(sections.getByRole("link", { name: "Settings", exact: true })).toHaveAttribute("aria-current", "page");
  await expectCompact(page, false);
  await page.goBack();
  await expect(page).toHaveURL(/\/vault\/team$/);
  await expect(sections.getByRole("link", { name: "Overview", exact: true })).toHaveAttribute("aria-current", "page");
  await expectCompact(page, false);
  await page.goForward();
  await expect(page).toHaveURL(/\/vault\/team\/settings$/);
  await expect(sections.getByRole("link", { name: "Settings", exact: true })).toHaveAttribute("aria-current", "page");
  await expectCompact(page, false);
  await sidebar.getByRole("link", { name: "Home", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  // Browser history changes before React commits its transition. Both routes
  // have an expanded rail, so that value alone cannot prove Home rendered.
  await expect(sidebar.getByRole("link", { name: "Home", exact: true })).toHaveAttribute("aria-current", "page");
  await expectCompact(page, false);
  await page.goBack();
  await expect(page).toHaveURL(/\/vault\/team\/settings$/);
  await expect(sections.getByRole("link", { name: "Settings", exact: true })).toHaveAttribute("aria-current", "page");
  await expectCompact(page, true);
  await page.goForward();
  await expect(page).toHaveURL(/\/$/);
  await expect(sidebar.getByRole("link", { name: "Home", exact: true })).toHaveAttribute("aria-current", "page");
  await expectCompact(page, false);
});

test("ordinary route preference survives Vault toggles, later expansion, and reload", async ({ page }) => {
  await fixture(page, { compact: true });
  await page.goto("/search");
  const sidebar = page.getByTestId("app-sidebar");
  await expectCompact(page, true);
  await sidebar.getByRole("link", { name: "Vaults", exact: true }).click();
  await sidebar.getByRole("button", { name: "Expand sidebar", exact: true }).click();
  await sidebar.getByRole("link", { name: "Home", exact: true }).click();
  await expectCompact(page, true);
  await sidebar.getByRole("button", { name: "Expand sidebar", exact: true }).click();
  await sidebar.getByRole("link", { name: "Search", exact: true }).click();
  await expect(sidebar.getByRole("link", { name: "Search", exact: true })).toHaveAttribute("aria-current", "page");
  await expectCompact(page, false);
  await page.reload();
  await expectCompact(page, false);
  await sidebar.getByRole("link", { name: "Vaults", exact: true }).click();
  await expectCompact(page, true);
  await sidebar.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Settings", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/vault$/);
  // The background controls are intentionally absent from the accessibility tree.
  await expect(sidebar).toHaveAttribute("data-compact", "true");
  await page.getByRole("button", { name: "Close settings", exact: true }).click();
  await expectCompact(page, true);
});

test("Vault creation uses the ordinary preference despite its /vault prefix", async ({ page }) => {
  await fixture(page);
  await page.goto("/vault/new");
  const sidebar = page.getByTestId("app-sidebar");
  await expectCompact(page, false);
  await sidebar.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
  await sidebar.getByRole("link", { name: "Vaults", exact: true }).click();
  await expect(page).toHaveURL(/\/vault$/);
  await expectCompact(page, true);
  await sidebar.getByRole("button", { name: "Expand sidebar", exact: true }).click();
  await page.goBack();
  await expect(page).toHaveURL(/\/vault\/new$/);
  await expectCompact(page, true);
  await page.goForward();
  await expect(page).toHaveURL(/\/vault$/);
  await expectCompact(page, true);
  expect(await page.evaluate(() => localStorage.getItem("akb_app_sidebar_compact"))).toBe("true");
});

async function bounds(locator: Locator) {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  return box!;
}

for (const dark of [false, true]) {
  test(`sidebar icon anchors stay fixed when the label column collapses (${dark ? "dark" : "light"})`, async ({ page }, testInfo) => {
    await fixture(page, { dark });
    await page.goto("/");
    const sidebar = page.getByTestId("app-sidebar");
    await expectCompact(page, false);
    const icons = {
      logo: sidebar.getByRole("link", { name: "AKB home" }).locator(".brand-mark"),
      home: sidebar.getByRole("link", { name: "Home", exact: true }).locator("svg"),
      search: sidebar.getByRole("link", { name: "Search", exact: true }).locator("svg"),
      vaults: sidebar.getByRole("link", { name: "Vaults", exact: true }).locator("svg"),
      help: sidebar.getByRole("button", { name: "Help", exact: true }).locator("svg"),
      settings: sidebar.getByRole("button", { name: "Settings", exact: true }).locator("svg"),
    };
    const expanded = await Promise.all(Object.values(icons).map(bounds));
    await page.screenshot({ path: testInfo.outputPath("expanded.png"), animations: "disabled" });
    await sidebar.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
    await expectCompact(page, true);
    const collapsed = await Promise.all(Object.values(icons).map(bounds));
    await page.screenshot({ path: testInfo.outputPath("collapsed.png"), animations: "disabled" });
    for (const [index, name] of Object.keys(icons).entries()) {
      expect.soft(collapsed[index].x, `${name} x anchor`).toBeCloseTo(expanded[index].x, 5);
      expect.soft(collapsed[index].y, `${name} y anchor`).toBeCloseTo(expanded[index].y, 5);
    }
    await sidebar.getByRole("button", { name: "Expand sidebar", exact: true }).click();
    const restored = await Promise.all(Object.values(icons).map(bounds));
    expect(restored).toEqual(expanded);
  });
}
