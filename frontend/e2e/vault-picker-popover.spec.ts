import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");

async function fixture(page: Page, dark: boolean, currentName = "workspace-01") {
  await page.addInitScript(dark => {
    localStorage.setItem("akb_token", "vault-popover-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    localStorage.setItem("akb-vault-role-scope", "editable");
  }, dark);
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "popover-user", username: "owner", email: "owner@example.invalid", display_name: "Workspace owner", auth_method: "local", key_class: null, is_admin: false } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: Array.from({ length: 40 }, (_, index) => ({ id: `vault-${index}`, name: index === 0 ? currentName : `workspace-${String(index + 1).padStart(2, "0")}`, role: index === 0 ? "owner" : index === 1 ? "reader" : "writer" })) } });
    if (path.endsWith("/info")) return route.fulfill({ json: { name: currentName, role: "owner", document_count: 0, collection_count: 0, file_count: 0, table_count: 0 } });
    if (path.includes("/browse/")) return route.fulfill({ json: { vault: "workspace-01", items: [], archive_scope: url.searchParams.get("archive_scope") || "unarchived" } });
    if (path.includes("/documents/")) return route.fulfill({ status: 404, json: { detail: "No guide" } });
    return route.fulfill({ json: { items: [], changes: [], tokens: [], templates: [], unread_count: 0 } });
  });
}

for (const width of [320, 375, 1440]) for (const dark of [false, true]) test(`Vault picker stays beside its trigger at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 568 });
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
  await fixture(page, dark);
  await page.goto("/vault/workspace-01");
  const globalVaults = width < 1024
    ? page.getByRole("navigation", { name: "Primary mobile navigation" }).getByRole("link", { name: "Vaults", exact: true })
    : page.getByTestId("app-sidebar").getByRole("link", { name: "Vaults", exact: true });
  await expect(globalVaults).toHaveAttribute("href", "/vault");
  await globalVaults.click();
  await expect(page).toHaveURL(/\/vault$/);
  await expect(page.getByRole("heading", { name: "Your vaults", exact: true })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Vaults", exact: true })).toHaveCount(0);
  await page.getByRole("list", { name: "Available vaults" }).getByRole("link", { name: "workspace-01", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/workspace-01$/);
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  const trigger = page.getByRole("button", { name: "Switch vault: workspace-01", exact: true });
  await trigger.click();
  const picker = page.getByRole("dialog", { name: "Vaults", exact: true });
  const directory = picker.getByRole("navigation", { name: "Accessible vaults" });
  await expect(picker).not.toHaveAttribute("aria-modal", "true");
  await expect(page.locator('[data-slot="dialog-overlay"]')).toHaveCount(0);
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await expect(directory.getByRole("link")).toHaveCount(39);
  await expect(picker.getByText("Can edit", { exact: true }).filter({ visible: true })).toBeVisible();
  await expect(picker.getByRole("group", { name: "Filter vaults by your role" })).toBeHidden();
  const search = picker.getByRole("searchbox", { name: "Find a vault" });
  await expect(search).toBeFocused();
  await picker.getByRole("button", { name: "Reset filters", exact: true }).click();
  await expect(directory.getByRole("link")).toHaveCount(40);
  const anchor = (await trigger.boundingBox())!;
  const popup = (await picker.boundingBox())!;
  expect(popup.x).toBeGreaterThanOrEqual(7);
  expect(popup.x + popup.width).toBeLessThanOrEqual(width - 7);
  expect(popup.y).toBeGreaterThanOrEqual(7);
  expect(popup.y + popup.height).toBeLessThanOrEqual(561);
  await expect(picker).toHaveAttribute("data-side", "bottom");
  expect(popup.y - anchor.y - anchor.height).toBeGreaterThanOrEqual(4);
  expect(popup.y - anchor.y - anchor.height).toBeLessThanOrEqual(16);
  const scroll = await picker.locator('[data-slot="vault-switcher-list"]').evaluate(element => ({ visible: element.clientHeight, total: element.scrollHeight }));
  expect(scroll.visible).toBeGreaterThan(44);
  expect(scroll.total).toBeGreaterThan(scroll.visible);
  await search.fill("workspace-40");
  await expect(directory.getByRole("link")).toHaveCount(1);
  await expect(directory.getByRole("link", { name: "workspace-40", exact: true })).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("anchored-vault-picker.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await expect(picker).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await trigger.click();
  await expect(picker).toBeVisible();
  await trigger.click();
  await expect(picker).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const width of [320, 1440]) for (const name of ["engineering-wiki", "customer-experience"]) test(`Vault name ${name} stays readable and opens the picker at ${width}px`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 740 });
  await fixture(page, false, name);
  await page.addInitScript(() => localStorage.setItem("akb.focusedNavigationWidth.v1", "224"));
  await page.goto(`/vault/${name}`);
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  const button = page.getByRole("button", { name: `Switch vault: ${name}`, exact: true });
  const label = button.locator('[data-slot="vault-identity-name"]');
  await expect(label).toHaveText(name);
  await page.evaluate(() => document.fonts.ready);
  const metrics = await label.evaluate(element => ({ visible: element.clientHeight, full: element.scrollHeight, width: element.clientWidth }));
  expect(metrics.full).toBeLessThanOrEqual(metrics.visible + 1);
  expect(metrics.visible).toBeLessThanOrEqual(40);
  expect(metrics.width).toBeGreaterThan(90);
  await label.click();
  const picker = page.getByRole("dialog", { name: "Vaults", exact: true });
  await expect(picker).toBeVisible();
  await expect(button).toHaveAttribute("aria-expanded", "true");
  await page.screenshot({ path: testInfo.outputPath("readable-vault-name.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await expect(button).toBeFocused();
});

test("Visited collections appear separately from Vault switching with real deep links", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 740 });
  await fixture(page, false);
  await page.addInitScript(() => localStorage.setItem("akb-vault-role-scope", "all"));
  await page.route("**/api/v1/browse/**", route => route.fulfill({ json: { vault: "workspace-01", items: [
    { type: "collection", name: "guides", path: "guides", document_count: 0, collection_count: 0, file_count: 0, table_count: 0 },
  ], archive_scope: "unarchived" } }));
  await page.goto("/vault/workspace-01?collection=guides");
  await expect(page.getByRole("button", { name: "guides", exact: true })).toHaveAttribute("aria-current", "page");
  const button = page.getByRole("button", { name: "Switch vault: workspace-01", exact: true });
  await button.locator('[data-slot="vault-identity-name"]').click();
  const picker = page.getByRole("dialog", { name: "Vaults", exact: true });
  const toggleRecent = picker.getByRole("button", { name: "Recent collections in workspace-01", exact: true });
  await expect(toggleRecent).toHaveAttribute("aria-expanded", "false");
  await toggleRecent.click();
  const recent = picker.getByRole("navigation", { name: "Recent collections in workspace-01", exact: true });
  const link = recent.getByRole("link", { name: "guides", exact: true });
  await expect(link).toHaveAttribute("href", "/vault/workspace-01?collection=guides");
  await expect(picker.getByRole("navigation", { name: "Accessible vaults" }).getByRole("link")).toHaveCount(40);
  const search = picker.getByRole("searchbox", { name: "Find a vault" });
  await search.fill("workspace-40");
  await expect(recent).toHaveCount(0);
  await search.clear();
  await expect(toggleRecent).toHaveAttribute("aria-expanded", "false");
  await toggleRecent.click();
  await expect(link).toBeVisible();
  await link.click();
  await expect(picker).toHaveCount(0);
  await expect(page).toHaveURL(/\/vault\/workspace-01\?collection=guides$/);
});

for (const width of [375, 1440]) for (const dark of [false, true]) test(`Recent collections retain primary Vault space at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 568 });
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
  await fixture(page, dark);
  await page.addInitScript(() => {
    localStorage.setItem("akb-vault-role-scope", "all");
    localStorage.setItem("akb.recentCollections.v1:popover-user", JSON.stringify(Array.from({ length: 12 }, (_, index) => ({
      vault: "workspace-01", path: `teams/platform/architecture/decisions/long-collection-name-${index}-with-a-full-readable-path`, viewedAt: new Date(Date.now() - (index + 1) * 1_000).toISOString(),
    }))));
  });
  await page.goto("/vault/workspace-01");
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  const trigger = page.getByRole("button", { name: "Switch vault: workspace-01", exact: true });
  await trigger.click();
  const picker = page.getByRole("dialog", { name: "Vaults", exact: true });
  const directory = picker.getByRole("navigation", { name: "Accessible vaults", exact: true });
  const toggle = picker.getByRole("button", { name: "Recent collections in workspace-01", exact: true });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  const first = directory.getByRole("link").first();
  const before = (await first.boundingBox())!;
  expect(before.height).toBe(width < 640 ? 44 : 36);
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const recent = picker.getByRole("navigation", { name: "Recent collections in workspace-01", exact: true });
  const links = recent.getByRole("link");
  expect(await links.count()).toBeGreaterThan(0);
  expect(await links.count()).toBeLessThanOrEqual(3);
  expect((await first.boundingBox())!.y).toBe(before.y);
  await expect(first).toBeInViewport();
  const visibleHeight = await picker.locator('[data-slot="vault-switcher-list"]').evaluate(element => element.clientHeight);
  expect(visibleHeight).toBeGreaterThanOrEqual(width < 640 ? 88 : 108);
  const heights = await links.evaluateAll(elements => elements.map(element => element.getBoundingClientRect().height));
  expect(Math.max(...heights)).toBeLessThanOrEqual(52);
  await links.first().focus();
  await expect(page.getByRole("tooltip")).toContainText("teams / platform / architecture / decisions");
  await picker.getByRole("button", { name: "Create vault", exact: true }).focus();
  await expect(picker.getByRole("button", { name: "Create vault", exact: true })).toBeInViewport();
  const bounds = (await picker.boundingBox())!;
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(561);
  await page.screenshot({ path: testInfo.outputPath("compact-picker-with-recent.png"), fullPage: true });
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await trigger.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
});

test.describe("Short landscape picker", () => {
  test.use({ hasTouch: true, isMobile: true });
  for (const width of [667, 812]) test(`keeps Vaults and controls reachable at ${width}x375`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 375 });
    await fixture(page, false);
    await page.addInitScript(() => {
      localStorage.setItem("akb-vault-role-scope", "all");
      localStorage.setItem("akb.recentCollections.v1:popover-user", JSON.stringify([
        { vault: "workspace-01", path: "teams/platform/decisions", viewedAt: new Date(Date.now() - 1000).toISOString() },
      ]));
    });
    await page.goto("/vault/workspace-01");
    await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
    await page.getByRole("button", { name: "Switch vault: workspace-01", exact: true }).click();
    const picker = page.getByRole("dialog", { name: "Vaults", exact: true });
    const search = picker.getByRole("searchbox", { name: "Find a vault" });
    const create = picker.getByRole("button", { name: "Create vault", exact: true });
    await expect(picker.getByRole("button", { name: "Recent collections in workspace-01", exact: true })).toBeHidden();
    await expect(picker.getByRole("navigation", { name: "Accessible vaults", exact: true }).getByRole("link").first()).toBeInViewport();
    await picker.getByRole("button", { name: "Filter by role", exact: true }).click();
    await expect(picker).toHaveAttribute("data-scroll-mode", "whole");
    await create.focus();
    await expect(create).toBeInViewport();
    await search.focus();
    await expect(search).toBeInViewport();
    await picker.getByRole("button", { name: "Can edit", exact: true }).click();
    await expect(picker.getByRole("button", { name: "Recent collections in workspace-01", exact: true })).toHaveCount(0);
    await create.focus();
    await expect(create).toBeInViewport();
    await search.focus();
    await search.fill("workspace-40");
    await picker.getByRole("navigation", { name: "Accessible vaults", exact: true }).getByRole("link", { name: "workspace-40", exact: true }).focus();
    await expect(picker.getByRole("link", { name: "workspace-40", exact: true })).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath("short-landscape-picker.png") });
  });
});
