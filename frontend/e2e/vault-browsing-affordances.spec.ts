import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");

const longTitle = "A practical guide to preserving reading context, keyboard focus, and unsaved drafts while navigating between collections";
const vaults = [
  { id: "alpha", name: "alpha-engineering", role: "owner", description: "Architecture decisions, delivery guides, and team onboarding." },
  { id: "bravo", name: "bravo-research", role: "reader", description: "Customer interviews and research observations." },
  { id: "charlie", name: "charlie-archive", role: "owner", status: "archived", description: "Completed project references." },
  { id: "delta", name: "delta-product", role: "writer", description: "Product discovery and specifications." },
  { id: "echo", name: "echo-platform-architecture-and-delivery", role: "owner", description: "Operational knowledge and runbooks." },
  { id: "foxtrot", name: "foxtrot-design", role: "admin", description: "Shared design principles and reviews." },
];

async function fixture(page: Page, dark = false) {
  await page.addInitScript(dark => {
    localStorage.setItem("akb_token", "browsing-affordances-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    localStorage.setItem("akb.focusedNavigationWidth.v1", "256");
  }, dark);
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "browsing-user", username: "reader", email: "reader@example.invalid", display_name: "Workspace Reader", auth_method: "local", key_class: null, is_admin: false } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults } });
    if (path.endsWith("/info")) return route.fulfill({ json: { name: "alpha-engineering", role: "owner", document_count: 2, collection_count: 1, table_count: 0, file_count: 0 } });
    if (path.includes("/browse/")) return route.fulfill({ json: { vault: "alpha-engineering", items: [
      { type: "collection", name: "guides", path: "guides", doc_count: 2 },
      { type: "document", name: longTitle, path: "guides/long-title.md" },
      { type: "document", name: "Short title", path: "guides/short.md" },
    ], archive_scope: url.searchParams.get("archive_scope") || "unarchived" } });
    if (path.includes("/documents/")) return route.fulfill({ status: 404, json: { detail: "No guide" } });
    return route.fulfill({ json: { tokens: [], items: [], changes: [], templates: [], unread_count: 0 } });
  });
}

for (const width of [320, 768, 874, 1440]) for (const dark of [false, true]) test(`Vault cards retain readable order and controls at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 1000 });
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
  await fixture(page, dark);
  await page.goto("/vault");
  const list = page.getByRole("list", { name: "Available vaults" });
  const cards = list.locator('[data-slot="vault-directory-card"]');
  await expect(cards).toHaveCount(6);
  const columns = await list.evaluate(element => getComputedStyle(element).gridTemplateColumns.split(" ").length);
  expect(columns).toBe(width === 320 ? 1 : width === 768 ? 2 : width === 874 ? 3 : 4);
  await expect(list.getByRole("link")).toHaveText(vaults.map(vault => new RegExp(vault.name)));
  const heights = await cards.evaluateAll(elements => elements.map(element => element.getBoundingClientRect().height));
  expect(Math.max(...heights) - Math.min(...heights)).toBeLessThan(2);
  expect(Math.max(...heights)).toBeLessThan(160);
  await expect(list.getByText("Open vault", { exact: true })).toHaveCount(0);
  await expect(list.getByText("Owner", { exact: true })).toHaveCount(0);
  await expect(list.getByText("Read only", { exact: true })).toBeVisible();
  await expect(list.getByText("Archived", { exact: true })).toBeVisible();
  const first = list.getByRole("link", { name: "alpha-engineering", exact: true });
  await expect(first).toHaveAttribute("href", "/vault/alpha-engineering");
  await expect(first.getByRole("button")).toHaveCount(0);
  await expect(first.locator('[data-slot="vault-card-mark"] .lucide-box')).toBeVisible();
  const heading = (await first.getByRole("heading").boundingBox())!;
  const star = (await list.getByRole("button", { name: "Add alpha-engineering to favorites", exact: true }).boundingBox())!;
  expect(heading.y).toBeGreaterThanOrEqual(star.y + star.height);
  const longName = list.getByRole("heading", { name: "echo-platform-architecture-and-delivery", exact: true });
  const longFavorite = list.getByRole("button", { name: "Add echo-platform-architecture-and-delivery to favorites", exact: true });
  const longHeadingBounds = (await longName.boundingBox())!;
  const longStarBounds = (await longFavorite.boundingBox())!;
  expect(longHeadingBounds.y).toBeGreaterThanOrEqual(longStarBounds.y + longStarBounds.height);
  await page.screenshot({ path: testInfo.outputPath("vault-box-grid.png"), animations: "disabled", fullPage: true });
  await first.focus();
  await page.keyboard.press("Tab");
  const favorite = list.getByRole("button", { name: "Add alpha-engineering to favorites", exact: true });
  await expect(favorite).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(list.getByRole("button", { name: "Remove alpha-engineering from favorites", exact: true })).toHaveAttribute("aria-pressed", "true");
  const filters = page.getByRole("group", { name: "Vault list filter" });
  await filters.getByRole("button", { name: "Favorites", exact: true }).click();
  await expect(cards).toHaveCount(1);
  await filters.getByRole("button", { name: "All", exact: true }).click();
  await page.getByRole("textbox", { name: "Find a vault", exact: true }).fill("Customer interviews");
  await expect(cards).toHaveCount(1);
  await expect(list.getByRole("link", { name: "bravo-research", exact: true })).toBeVisible();
  await page.getByRole("textbox", { name: "Find a vault", exact: true }).clear();
  await expect(cards).toHaveCount(6);
  const views = page.getByRole("group", { name: "Vault view", exact: true });
  await views.getByRole("button", { name: "List view", exact: true }).click();
  await expect(list).toHaveAttribute("data-view", "list");
  await expect(list.getByRole("link")).toHaveText(vaults.map(vault => new RegExp(vault.name)));
  await expect(list.getByRole("button", { name: "Remove alpha-engineering from favorites", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.screenshot({ path: testInfo.outputPath("vault-list-directory.png"), animations: "disabled", fullPage: true });
  await page.reload();
  await expect(views.getByRole("button", { name: "List view", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(list).toHaveAttribute("data-view", "list");
  await views.getByRole("button", { name: "Grid view", exact: true }).click();
  await expect(list).toHaveAttribute("data-view", "grid");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const box = (await first.boundingBox())!;
  await first.click({ position: { x: box.width / 2, y: 6 } });
  await expect(page).toHaveURL(/\/vault\/alpha-engineering$/);
  await page.goBack();
  await expect(list).toBeVisible();
  const restoredBox = (await first.boundingBox())!;
  await first.click({ position: { x: restoredBox.width - 6, y: restoredBox.height / 2 } });
  await expect(page).toHaveURL(/\/vault\/alpha-engineering$/);
  await page.screenshot({ path: testInfo.outputPath("vault-card-directory.png"), animations: "disabled", fullPage: true });
});

async function openExplorer(page: Page, dark = false, mobile = false) {
  await fixture(page, dark);
  await page.goto("/vault/alpha-engineering?collection=guides");
  if (mobile) {
    const toggle = page.getByRole("button", { name: "Open vault navigation", exact: true });
    if (await toggle.isVisible()) await toggle.click();
  }
  const explorer = page.getByRole("navigation", { name: "alpha-engineering explorer", exact: true });
  await expect(explorer).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  return explorer.locator('a[href="/vault/alpha-engineering/doc/guides%2Flong-title.md"]');
}

for (const dark of [false, true]) test(`Overflowing document titles reveal on native hover and focus ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: "no-preference", colorScheme: dark ? "dark" : "light" });
  const link = await openExplorer(page, dark);
  await expect(link).toHaveAccessibleName(`Document: ${longTitle}`);
  const label = link.locator('[data-slot="overflow-reveal-title"]');
  const moving = label.locator('[data-slot="overflow-reveal-text"]');
  await expect(label).toHaveAttribute("data-overflow", "true");
  await expect(label).toHaveAttribute("data-reveal", "idle");
  const before = await link.boundingBox();
  const icon = await link.locator("svg").first().boundingBox();
  await page.locator("#main").focus();
  await link.hover();
  await expect(label).toHaveAttribute("data-reveal", "running");
  await expect(moving).toHaveCSS("animation-iteration-count", "1");
  await expect.poll(() => moving.evaluate(element => new DOMMatrixReadOnly(getComputedStyle(element).transform).m41)).toBeLessThan(-1);
  expect(await link.boundingBox()).toEqual(before);
  expect(await link.locator("svg").first().boundingBox()).toEqual(icon);
  await page.screenshot({ path: testInfo.outputPath("title-hover-reveal.png"), animations: "allow" });
  await page.keyboard.press("Escape");
  await expect(label).toHaveAttribute("data-reveal", "idle");
  await expect(page.locator("#main")).toBeFocused();
  await page.locator("#global-search-trigger").hover();
  await expect(label).toHaveAttribute("data-reveal", "idle");
  await expect(moving).toHaveCSS("transform", "none");
  await link.focus();
  await expect(label).toHaveAttribute("data-reveal", "running");
  await page.keyboard.press("Escape");
  await expect(label).toHaveAttribute("data-reveal", "idle");
  await expect(link).toBeFocused();
  const short = page.getByRole("link", { name: "Document: Short title", exact: true });
  await short.focus();
  const shortLabel = short.locator('[data-slot="overflow-reveal-title"]');
  await expect(shortLabel).toHaveAttribute("data-overflow", "false");
  await expect(shortLabel.locator('[data-slot="overflow-reveal-text"]')).toHaveCSS("transform", "none");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Actions for Short title", exact: true })).toBeFocused();
});

test("Reduced motion provides a static full document title on keyboard focus", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const link = await openExplorer(page, false, true);
  await link.focus();
  const label = link.locator('[data-slot="overflow-reveal-title"]');
  await expect(label).toHaveAttribute("data-reveal", "static");
  await expect(label.locator('[data-slot="overflow-reveal-text"]')).toHaveCSS("transform", "none");
  const fullTitle = page.locator('[data-slot="overflow-reveal-tooltip"]').filter({ hasText: longTitle });
  await expect(fullTitle).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("static-full-title.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await expect(fullTitle).toHaveCount(0);
  await expect(link).toBeFocused();
});

test.describe("Touch document navigation", () => {
  test.use({ hasTouch: true, isMobile: true, viewport: { width: 375, height: 812 } });
  test("the first tap opens the document without a hover-only interception", async ({ page }) => {
    const link = await openExplorer(page, false, true);
    await expect(link).toHaveAccessibleName(`Document: ${longTitle}`);
    await link.tap();
    await expect(page).toHaveURL(/\/vault\/alpha-engineering\/doc\/guides%2Flong-title\.md$/);
  });
});
