import { expect, test, type Locator, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");

// Browser-owned HTTP fixtures: no mutations or requests reach a user's Vault.
const title = "문서 읽기 작업공간 — Architecture and operating decisions for a shared knowledge workspace";
const path = "guides/team/operations/reading.md";
const lastEdited = "2026-09-14T09:42:30Z";
const createdAt = "2026-08-25T01:05:00Z";
const body = [
  "# Authored heading",
  "읽기 흐름을 유지하면서 팀의 지식을 탐색합니다. ".repeat(12),
  "- First item\n- Second item",
  "| Name | Role |\n| --- | --- |\n| Reader | Read documents |",
  "```text\n" + "long_code_".repeat(100) + "\n```",
  "![Small diagram](/reading-fixture.svg)",
].join("\n\n");

async function fixture(page: Page, dark = false, folded = false, content = body, publicSlug?: string, metadata: { summary?: string; status?: string; title?: string } = {}) {
  await page.addInitScript(({ dark, folded }) => {
    localStorage.setItem("akb_token", "resource-reading-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    if (folded) {
      localStorage.setItem("akb.treeVisible", "0");
      localStorage.setItem("akb.vaultRailCollapsed", "1");
    }
  }, { dark, folded });
  await page.route("**/reading-fixture.svg", route => route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40"><rect width="80" height="40" fill="teal"/></svg>' }));
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (pathname.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "reading-fixture", username: "fixture", display_name: "Reading reviewer", email: "reader@example.invalid", auth_method: "local", is_admin: false } });
    if (pathname.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "reading-vault", name: "fixture", role: "writer" }] } });
    if (pathname.endsWith("/info")) return route.fulfill({ json: { name: "fixture", role: "writer", is_archived: false, is_external_git: false, document_count: 1 } });
    if (pathname.endsWith("/graph/overview")) return route.fulfill({ json: { nodes: [], edges: [], nodes_total: 0, edges_total: 0, returned: 0, truncated: false } });
    if (pathname.includes("/browse/")) return route.fulfill({ json: { items: [
      { type: "collection", path: "guides", name: "Guides" },
      { type: "collection", path: "guides/team", name: "Team practices" },
      { type: "collection", path: "guides/team/operations", name: "Operations" },
      { type: "document", path, name: title },
    ] } });
    if (pathname.endsWith("/search")) return route.fulfill({ json: { query: "reading", total: 1, results: [{ title, uri: `akb://fixture/coll/guides/team/operations/doc/reading.md`, vault: "fixture", path, source_type: "document", score: 1 }] } });
    if (pathname.includes("/documents/fixture/") && !pathname.endsWith("/history")) return route.fulfill({ json: { uri: `akb://fixture/coll/guides/team/operations/doc/reading.md`, path, title, content, status: "active", current_commit: "aaaaaaaaaaaaa", updated_at: lastEdited, created_at: createdAt, summary: "An orientation to the resource reading workspace.", tags: ["guide"], is_public: Boolean(publicSlug), public_slug: publicSlug, ...metadata } });
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
}

async function expectQuietResourceTrail(trail: Locator, kind: "document" | "file" | "table") {
  await expect(trail.locator("a .lucide-box")).toBeVisible();
  await expect(trail.locator("a svg:not(.lucide-box)")).toHaveCount(0);
  const current = trail.locator('[aria-current="page"]');
  const glyph = trail.locator({ document: ".lucide-file-text", file: ".lucide-file", table: ".lucide-table-2" }[kind]);
  await expect(glyph).toBeInViewport({ ratio: 1 });
  const textBounds = (await current.boundingBox())!;
  const glyphBounds = (await glyph.boundingBox())!;
  const trailBounds = (await trail.boundingBox())!;
  // Kind belongs to the title, not before it or stranded at the tools edge.
  const gap = glyphBounds.x - textBounds.x - textBounds.width;
  expect(gap).toBeGreaterThanOrEqual(4);
  expect(gap).toBeLessThanOrEqual(8);
  expect(textBounds.width).toBeGreaterThan(30);
  expect(glyphBounds.x + glyphBounds.width).toBeLessThanOrEqual(trailBounds.x + trailBounds.width + 1);
  expect(Math.abs(glyphBounds.y + glyphBounds.height / 2 - textBounds.y - textBounds.height / 2)).toBeLessThanOrEqual(1);
}

async function expectQuietVaultTrail(page: Page, label: string) {
  const trail = page.getByRole("navigation", { name: "Current page", exact: true });
  await expect(trail.locator('[aria-current="page"]')).toHaveText(label);
  await expect(trail.locator("svg")).toHaveCount(1);
  const vault = trail.getByRole("link", { name: "fixture", exact: true });
  await expect(vault).toHaveAttribute("href", "/vault/fixture");
  await expect(vault.locator(".lucide-box")).toBeVisible();
  const current = trail.locator('[aria-current="page"]');
  await expect(current).toBeInViewport({ ratio: 1 });
  const textBounds = (await current.boundingBox())!;
  const vaultBounds = (await vault.boundingBox())!;
  const gap = textBounds.x - vaultBounds.x - vaultBounds.width;
  expect(gap).toBeGreaterThan(8);
  expect(gap).toBeLessThan(24);
}

const vaultDestinations = [
  ["Overview", "/vault/fixture"],
  ["Graph", "/vault/fixture/graph"],
  ["Public links", "/vault/fixture/publications"],
  ["Members", "/vault/fixture/members"],
  ["Settings", "/vault/fixture/settings"],
] as const;

for (const width of [375, 1440, 2560]) for (const dark of [false, true]) {
  test(`Vault quick search stays scoped and returns to its trigger at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, dark);
    await page.goto("/vault/fixture/members");
    const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
    const trigger = page.getByRole("button", { name: "Search knowledge", exact: true });
    await expect(trigger).toBeInViewport({ ratio: 1 });
    await expect(navigation.getByRole("link", { name: "Search", exact: true })).toHaveCount(0);
    await expect(page.getByRole("banner").getByRole("button", { name: "Search knowledge", exact: true })).toHaveCount(1);
    await expect(navigation.locator("..").getByRole("button", { name: /Search/ })).toHaveCount(0);
    const searchBox = (await trigger.boundingBox())!;
    expect(searchBox.x + searchBox.width).toBeLessThanOrEqual(width);
    await expect(trigger).toHaveCSS("border-top-width", "1px");
    if (width >= 1440) {
      await expect(trigger.getByText("Search knowledge…", { exact: true })).toBeVisible();
      expect(searchBox.width).toBe(256);
    } else {
      expect(searchBox.width).toBeGreaterThanOrEqual(36);
      expect(searchBox.height).toBeGreaterThanOrEqual(36);
    }
    await page.screenshot({ path: testInfo.outputPath("vault-search-entry.png") });
    await trigger.focus();
    await expect(trigger).not.toHaveCSS("box-shadow", "none");
    await trigger.press("Enter");
    const panel = page.getByTestId("global-search-dialog");
    const input = panel.getByRole("combobox", { name: "Search in fixture", exact: true });
    await expect(input).toBeFocused();
    await expect(panel.getByRole("button", { name: "Search scope: fixture", exact: true })).toBeVisible();
    await expect(page).toHaveURL(/\/vault\/fixture\/members$/);
    const request = page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/search"));
    await input.fill("reading");
    expect(new URL((await request).url()).searchParams.getAll("vault")).toEqual(["fixture"]);
    await expect(panel.getByRole("option").first()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("vault-search-results.png") });
    const scope = panel.getByRole("button", { name: "Search scope: fixture", exact: true });
    await scope.click();
    await expect(page.getByRole("menuitemradio", { name: /fixture/ })).toHaveAttribute("aria-checked", "true");
    await page.keyboard.press("Escape");
    await expect(panel).toBeVisible();
    await expect(scope).toBeFocused();
    await scope.click();
    const expandedRequest = page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/search") && !new URL(request.url()).searchParams.has("vault"));
    await page.getByRole("menuitemradio", { name: /All vaults/ }).click();
    await expandedRequest;
    const allInput = panel.getByRole("combobox", { name: "Search all accessible vaults", exact: true });
    await expect(allInput).toHaveValue("reading");
    await expect(panel.getByRole("option").first()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("all-vault-search-results.png") });
    await panel.getByRole("button", { name: "Search scope: All vaults", exact: true }).click();
    await page.getByRole("menuitemradio", { name: /fixture/ }).click();
    await expect(input).toHaveValue("reading");
    await expect(panel.getByRole("option").first()).toBeVisible();
    await input.press("Enter");
    const preview = page.getByTestId("document-preview-dialog");
    await expect(preview).toBeVisible();
    await expect(preview.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(preview).toHaveCount(0);
    await expect(page).toHaveURL(/\/vault\/fixture\/members$/);
    await expect(trigger).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

for (const scenario of [
  { width: 375, height: 812, dark: false, route: "/vault/fixture/members" },
  { width: 1440, height: 900, dark: false, route: "/vault/fixture/members" },
  { width: 1440, height: 900, dark: true, route: "/vault/fixture/members" },
  { width: 667, height: 375, dark: false, route: "/vault/fixture/members" },
  { width: 1440, height: 900, dark: false, route: "/" },
]) test(`quick search selects another accessible Vault at ${scenario.width}x${scenario.height} ${scenario.dark ? "dark" : "light"} from ${scenario.route}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: scenario.width, height: scenario.height });
  await fixture(page, scenario.dark);
  await page.route("**/api/v1/my/vaults", route => route.fulfill({ json: { vaults: [
    { id: "reading-vault", name: "fixture", role: "writer" },
    { id: "beta-vault", name: "팀-beta", role: "reader" },
    ...Array.from({ length: 40 }, (_, i) => ({ id: `vault-${i}`, name: `Knowledge ${i}`, role: "reader" })),
  ] } }));
  await page.goto(scenario.route);
  const trigger = page.getByRole("button", { name: "Search knowledge", exact: true });
  await trigger.click();
  const panel = page.getByTestId("global-search-dialog");
  const query = panel.getByRole("combobox");
  await query.fill("reading");
  await panel.getByRole("button", { name: "Documents", exact: true }).click();
  await panel.getByRole("button", { name: /^Search scope:/ }).click();
  const menu = page.getByRole("menu", { name: /^Search scope:/ });
  const filter = menu.getByRole("searchbox", { name: "Filter vaults" });
  await expect(filter).toBeFocused();
  await expect(menu).toBeInViewport({ ratio: 1 });
  await page.screenshot({ path: testInfo.outputPath("vault-scope-directory.png") });
  await filter.fill("missing-vault");
  await expect(menu.getByText("No vaults match this filter.")).toBeVisible();
  await filter.fill("beta");
  await expect(menu.getByRole("menuitemradio", { name: "팀-beta", exact: true })).toBeVisible();
  await filter.press("ArrowUp");
  await expect(menu.getByRole("menuitemradio", { name: "팀-beta", exact: true })).toBeFocused();
  const changed = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname.endsWith("/search") && url.searchParams.get("vault") === "팀-beta" && url.searchParams.get("source_type") === "document";
  });
  await page.keyboard.press("Enter");
  await changed;
  await expect(page).toHaveURL(scenario.route);
  await expect(query).toHaveValue("reading");
  await expect(query).toHaveAccessibleName("Search in 팀-beta");
  const selectedScope = panel.getByRole("button", { name: "Search scope: 팀-beta", exact: true });
  await expect(selectedScope).toBeFocused();
  await selectedScope.click();
  await expect(menu.getByRole("menuitemradio", { name: "팀-beta", exact: true })).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(panel).toBeVisible();
  await expect(selectedScope).toBeFocused();
  await panel.getByRole("button", { name: "Continue in search page", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/%ED%8C%80-beta\/search\?q=reading&source=document$/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const width of [375, 1440, 2560]) test(`Vault quick search carries its query and kind into advanced search at ${width}px`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 900 });
  await fixture(page);
  await page.goto("/vault/fixture/members");
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  const panel = page.getByTestId("global-search-dialog");
  await panel.getByRole("button", { name: "Documents", exact: true }).click();
  await panel.getByRole("combobox").fill("reading");
  await panel.getByRole("button", { name: "Continue in search page", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture\/search\?q=reading&source=document$/);
  const result = page.getByRole("list", { name: "Semantic search results" }).getByRole("link").filter({ hasText: title });
  await expect(result).toBeVisible();
  await expect(page.getByText("1 top result loaded", { exact: true })).toBeVisible();
  await expect(page.getByTestId("search-workspace").getByRole("searchbox", { name: "Search query" })).toHaveValue("reading");
  await expect(page.getByRole("button", { name: "Search knowledge", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("vault-search-workspace.png") });
  await result.click();
  const preview = page.getByTestId("document-preview-dialog");
  await expect(preview).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(preview).toHaveCount(0);
  await expect(page).toHaveURL(/\/vault\/fixture\/search\?q=reading&source=document$/);
  await expect(result).toBeFocused();
  await expect(page.getByRole("searchbox", { name: "Search query" })).toHaveCount(1);
  await page.goBack();
  await expect(page).toHaveURL(/\/vault\/fixture\/members$/);
});

for (const dark of [false, true]) test(`Vault header keeps a visible divider (${dark ? "dark" : "light"})`, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await fixture(page, dark);
  for (const path of ["/vault", "/vault/fixture/members"]) {
    await page.goto(path);
    const header = page.locator(".app-header");
    await expect(header.getByRole("button", { name: "Search knowledge", exact: true })).toBeVisible();
    await expect(header).toHaveCSS("border-bottom-width", "1px");
    await expect(header).not.toHaveCSS("border-bottom-color", "rgba(0, 0, 0, 0)");
  }
});

for (const width of [375, 1440]) test(`search scope keeps a long Vault name accessible at ${width}px`, async ({ page }, testInfo) => {
  const vault = "팀-" + "knowledge-workspace-".repeat(6);
  await page.setViewportSize({ width, height: 900 });
  await fixture(page);
  await page.route("**/api/v1/my/vaults", route => route.fulfill({ json: { vaults: [{ id: "long-vault", name: vault, role: "reader" }] } }));
  await page.goto(`/vault/${encodeURIComponent(vault)}/members`);
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  const panel = page.getByTestId("global-search-dialog");
  const scope = panel.getByRole("button", { name: `Search scope: ${vault}`, exact: true });
  await expect(scope).toBeInViewport({ ratio: 1 });
  await expect(panel.getByRole("combobox")).toBeInViewport({ ratio: 1 });
  await scope.click();
  await expect(page.getByRole("menuitemradio", { name: /Current vault/ })).toBeInViewport({ ratio: 1 });
  await expect(page.getByRole("menuitemradio", { name: /All vaults/ })).toBeInViewport({ ratio: 1 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("long-vault-search-scope.png") });
});

test("Vault quick search keeps its controls reachable in a short landscape viewport", async ({ page }) => {
  await page.setViewportSize({ width: 667, height: 375 });
  await fixture(page);
  await page.route("**/api/v1/search?**", route => route.fulfill({ json: {
    query: "reading", total: 12, results: Array.from({ length: 12 }, (_, i) => ({
      title: `Reading document ${i + 1}`, uri: `akb://fixture/coll/guides/doc/reading-${i}.md`,
      path: `guides/reading-${i}.md`, vault: "fixture", source_type: "document",
    })),
  } }));
  await page.goto("/vault/fixture/members");
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  const panel = page.getByTestId("global-search-dialog");
  await panel.getByRole("combobox").fill("reading");
  await expect(panel.getByRole("option")).toHaveCount(12);
  await expect(panel.getByRole("button", { name: "Continue in search page", exact: true })).toBeInViewport({ ratio: 1 });
  await expect(panel.getByRole("button", { name: "Close search", exact: true })).toBeInViewport({ ratio: 1 });
  await panel.getByRole("combobox").press("End");
  for (let i = 0; i < 11; i++) await page.keyboard.press("ArrowDown");
  await expect(panel.getByRole("option").last()).toBeInViewport({ ratio: 1 });
});

for (const scope of ["vault", "all"] as const) for (const destination of ["result", "advanced"] as const) test(`Vault quick search guards a dirty editor before opening ${scope} ${destination}`, async ({ page }) => {
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}?view=edit`);
  const editor = page.getByRole("textbox", { name: "Document body (markdown)" });
  const draft = "Do not lose this draft when searching within the Vault.";
  await editor.fill(draft);
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  const panel = page.getByTestId("global-search-dialog");
  await panel.getByRole("combobox").fill("reading");
  if (scope === "all") {
    await panel.getByRole("button", { name: "Search scope: fixture", exact: true }).click();
    await page.getByRole("menuitemradio", { name: /All vaults/ }).click();
  }
  if (destination === "result") await panel.getByRole("option").first().click();
  else await panel.getByRole("button", { name: "Continue in search page", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "Leave this document?", exact: true });
  await expect(confirmation).toBeVisible();
  await expect(panel).toHaveCount(0);
  await expect(page).toHaveURL(/\?view=edit$/);
  await confirmation.getByRole("button", { name: "Keep editing", exact: true }).click();
  await expect(editor).toContainText(draft);
  await expect(page.getByTestId("document-preview-dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  if (scope === "all") {
    await panel.getByRole("button", { name: "Search scope: fixture", exact: true }).click();
    await page.getByRole("menuitemradio", { name: /All vaults/ }).click();
  }
  if (destination === "result") await panel.getByRole("option").first().click();
  else await panel.getByRole("button", { name: "Continue in search page", exact: true }).click();
  await confirmation.getByRole("button", { name: "Leave document", exact: true }).click();
  if (destination === "result") {
    const preview = page.getByTestId("document-preview-dialog");
    await expect(preview).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(preview).toHaveCount(0);
    await expect(page).toHaveURL(/\?view=edit$/);
    await expect(editor).toContainText(draft);
  } else {
    await expect(page).toHaveURL(url => url.pathname === (scope === "all" ? "/search" : "/vault/fixture/search") && url.searchParams.get("q") === "reading");
  }
});

async function openVaultDestination(page: Page, name: string) {
  const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
  const link = navigation.getByRole("link", { name, exact: true });
  if (await link.isVisible()) await link.click();
  else {
    await navigation.getByRole("button", { name: "More vault pages", exact: true }).click();
    await page.getByRole("menuitem", { name, exact: true }).click();
  }
}

async function expectVaultDestinationsReachable(page: Page) {
  const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
  await expect(navigation).toBeVisible();
  const expected = vaultDestinations.map(([, href]) => href).sort();
  const more = navigation.getByRole("button", { name: "More vault pages", exact: true });
  // A ResizeObserver can replace More with inline links between the visibility
  // read and the click. Retry the complete accessibility check against the
  // settled layout instead of waiting forever for the vanished old control.
  await expect(async () => {
  if (await more.isVisible()) {
    await expect(more).toHaveText("More");
    if (!await page.getByRole("menu", { name: "More vault pages", exact: true }).isVisible()) await more.click({ timeout: 1000 });
    await expect(page.getByRole("menu", { name: "More vault pages", exact: true })).toBeVisible();
    // Read inline and overflow links in one DOM snapshot. Text scaling can move
    // a link between them while the disclosure is opening.
    await expect.poll(() => page.locator('[data-slot="vault-section-navigation"] > a, [role="menuitem"][data-vault-destination]')
      .evaluateAll(links => links.map(link => link.getAttribute("href")).sort())).toEqual(expected);
    await page.keyboard.press("Escape");
    await expect(more).toBeFocused();
  } else {
    await expect.poll(() => navigation.getByRole("link")
      .evaluateAll(links => links.map(link => link.getAttribute("href")).sort())).toEqual(expected);
  }
  }).toPass({ timeout: 10000 });
}

for (const width of [375, 1440, 2560]) for (const dark of [false, true]) {
  test(`shared header distinguishes global tools from Vault navigation at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, dark);
    await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
    await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
    const header = page.getByRole("banner");
    const search = header.getByRole("button", { name: "Search knowledge", exact: true });
    const notifications = header.getByRole("button", { name: /^Notifications,/ });
    const account = header.getByRole("button", { name: "Account menu — Reading reviewer", exact: true });
    for (const control of [search, notifications, account]) {
      await expect(control).toBeInViewport({ ratio: 1 });
      expect((await control.boundingBox())!.height).toBe(36);
    }
    const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
    if (width >= 1440) {
      await expect(search).toHaveText("Search knowledge…");
      await expect(account.getByText("Reading reviewer", { exact: true })).toBeVisible();
      const headerColor = await header.evaluate(el => getComputedStyle(el).backgroundColor);
      expect(await navigation.locator("..").evaluate(el => getComputedStyle(el).backgroundColor)).toBe(headerColor);
      expect(await page.locator('[aria-label="Document workspace"]').evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe(headerColor);
      expect(await account.evaluate(el => getComputedStyle(el).boxShadow)).toBe("none");
    }
    await expectVaultDestinationsReachable(page);
    const originalUrl = page.url();
    await search.click();
    const dialog = page.getByRole("dialog", { name: "Search in fixture", exact: true });
    await expect(dialog).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(search).toBeFocused();
    expect(page.url()).toBe(originalUrl);
    const before = (await header.boundingBox())!;
    await account.click();
    await expect(page.getByRole("menu")).toBeVisible();
    expect(await page.locator("body").getAttribute("data-scroll-locked")).toBeNull();
    await page.keyboard.press("Escape");
    await expect(account).toBeFocused();
    expect(await header.boundingBox()).toEqual(before);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await account.blur();
    await page.screenshot({ path: testInfo.outputPath("shared-header.png") });
  });
}

test("a short resource title keeps its kind marker attached on a wide header", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page, false, false, "test", undefined, { title: "API Gateway" });
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const trail = page.getByRole("navigation", { name: "Resource location", exact: true });
  await expect(trail).toContainText("API Gateway");
  await expectQuietResourceTrail(trail, "document");
  await page.screenshot({ path: testInfo.outputPath("breadcrumb-short-title.png") });
});

test("Vault destinations stay together instead of splitting across the workspace", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
  await expect(navigation.getByRole("link", { name: "Settings", exact: true })).toBeVisible();
  await expect.poll(async () => {
    const publicLinks = (await navigation.getByRole("link", { name: "Public links", exact: true }).boundingBox())!;
    const members = (await navigation.getByRole("link", { name: "Members", exact: true }).boundingBox())!;
    return members.x - (publicLinks.x + publicLinks.width);
  }).toBeLessThanOrEqual(24);
  // A document is not Overview: its breadcrumb/tree, not a false selected tab,
  // identifies the current resource.
  await expect(navigation.locator('[aria-current="page"]')).toHaveCount(0);
});

for (const width of [320, 1440]) test(`long summaries preserve draft state and publishing controls at ${width}px`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 900 });
  const summary = "An extended description of this document. ".repeat(40);
  await fixture(page, false, false, "test", "existing-link", { summary, status: "draft" });
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const context = page.locator('[data-slot="resource-command-row"]');
  await expect(context.getByText("Draft", { exact: true })).toBeVisible();
  await expect(context.getByRole("button", { name: "Public link", exact: true })).toBeInViewport({ ratio: 1 });
  await expect(context.getByRole("button", { name: `Actions for ${title}`, exact: true })).toBeInViewport({ ratio: 1 });
  const time = context.locator("time");
  await expect(time).toBeVisible();
  if (width === 1440) {
    expect(await time.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await page.getByRole("button", { name: "Read document summary" }).click();
    const dialog = page.getByRole("dialog", { name: "Document summary", exact: true });
    await expect(dialog).toContainText(summary.trim());
    await page.keyboard.press("Escape");
  } else await expect(page.getByRole("button", { name: "Read document summary" })).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("short-draft.png") });
});

test("document loading reserves the context and viewer frame without shifting reading tools", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  let resolveDocument!: () => void;
  const pendingDocument = new Promise<void>(resolve => { resolveDocument = resolve; });
  await page.route("**/api/v1/documents/fixture/**", async route => {
    await pendingDocument;
    await route.fallback();
  });
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const frame = page.locator('[data-slot="document-viewer-frame"]');
  await expect(frame).toBeVisible();
  const before = (await frame.boundingBox())!;
  resolveDocument();
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  const after = (await frame.boundingBox())!;
  expect(after.y).toBeCloseTo(before.y, 0);
  expect(after.height).toBeCloseTo(before.height, 0);
});

for (const width of [375, 768, 1440, 2560]) for (const dark of [false, true]) {
  test(`resource reading ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await fixture(page, dark, width === 2560);
    await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
    await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
    const location = page.getByRole("navigation", { name: "Resource location", exact: true });
    await expect(location).toBeVisible();
    await expect(location).toContainText(title);
    await expectQuietResourceTrail(location, "document");
    await expect(page.locator("h1#doc-title")).toHaveCount(1);
    await expect(page.locator('[data-slot="resource-command-row"]')).toHaveCount(1);
    const readingTools = page.getByRole("group", { name: "Document reading tools", exact: true });
    await expect(readingTools.getByRole("tab", { name: "Preview", exact: true })).toBeVisible();
    await expect(readingTools.getByRole("button", { name: "Copy markdown", exact: true })).toBeVisible();
    const contextBounds = (await page.locator('[data-slot="resource-command-row"]').boundingBox())!;
    const toolBounds = (await readingTools.boundingBox())!;
    expect(toolBounds.y).toBeGreaterThanOrEqual(contextBounds.y + contextBounds.height);
    const modeBounds = (await readingTools.getByRole("tablist").boundingBox())!;
    const actionBounds = (await readingTools.getByRole("group", { name: "Document actions", exact: true }).boundingBox())!;
    expect(modeBounds.x + modeBounds.width).toBeLessThan(actionBounds.x);
    await expect(readingTools.getByRole("button", { name: "Publish", exact: true })).toHaveCount(0);
    const commandMetrics = await page.locator('[data-slot="resource-command-row"] [data-reader-control]').evaluateAll(elements => elements.map(element => ({
      height: element.getBoundingClientRect().height,
      font: getComputedStyle(element).fontSize,
    })));
    expect(new Set(commandMetrics.map(item => item.height)).size).toBe(1);
    expect(commandMetrics.every(item => item.font === "14px")).toBe(true);
    const commandWidth = (await page.locator('[data-slot="resource-command-row"]').boundingBox())!.width;
    expect(commandMetrics.every(item => item.height === (commandWidth >= 768 ? 32 : 44))).toBe(true);
    const editTime = page.locator('[data-slot="resource-command-row"] time');
    await expect(editTime).toHaveAttribute("datetime", new Date(lastEdited).toISOString());
    const exactEditTime = await page.evaluate(value => new Date(value).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric", hour: "numeric",
      minute: "2-digit", second: "2-digit", timeZoneName: "short",
    }), lastEdited);
    await expect(editTime.locator(".sr-only")).toHaveText(`Last edited: ${exactEditTime}`);
    if (commandWidth >= 512) await expect(editTime).toBeVisible();
    await expect(page.getByRole("group", { name: "Document actions", exact: true })).toBeVisible();
    await expect(page.getByRole("group", { name: "Publishing and more options", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Open document info", exact: true })).toHaveCount(0);
    const collections = page.locator('[data-slot="vault-collections-sidebar"]');
    await expect(page.getByRole("button", { name: /^Explore vault:/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^(More|Vault pages) in fixture/ })).toHaveCount(0);
    const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
    await expectVaultDestinationsReachable(page);
    await expect(navigation.locator('[aria-current="page"]')).toHaveCount(0);
    expect((await navigation.boundingBox())!.height).toBe(width >= 1024 ? 40 : 44);
    await expect(navigation.getByRole("link", { name: "Overview", exact: true })).toBeInViewport();
    await expect(page.getByRole("navigation", { name: "Vault management", exact: true })).toHaveCount(0);
    await expect(page.getByText("In this vault", { exact: true })).toHaveCount(0);
    await expect(page.locator('[data-slot="resource-command-row"]').getByRole("navigation", { name: "Resource location", exact: true })).toHaveCount(0);
    await expect(location.getByRole("link", { name: "fixture", exact: true })).toHaveAttribute("href", "/vault/fixture");
    if (width >= 1024) {
      const search = page.getByRole("button", { name: "Search knowledge", exact: true });
      await expect(page.locator(".app-header").getByRole("navigation", { name: "Resource location", exact: true })).toBeVisible();
      const locationBox = (await location.boundingBox())!;
      const searchBox = (await search.boundingBox())!;
      expect(Math.abs(locationBox.y + locationBox.height / 2 - searchBox.y - searchBox.height / 2)).toBeLessThan(1);
      expect(locationBox.x + locationBox.width).toBeLessThanOrEqual(searchBox.x);
      expect(searchBox.width).toBe(256);
    } else {
      await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
    }
    if (width === 2560) await page.getByRole("button", { name: "Expand collections", exact: true }).click();
    await expect(collections.getByRole("tree", { name: "fixture explorer", exact: true })).toBeVisible();
    await expect(collections.getByRole("button", { name: "Collapse collections", exact: true })).toBeInViewport();
    await expect(collections.getByRole("link", { name: /^(Search|Graph|Public links|Members|Settings)$/ })).toHaveCount(0);
    if (width < 1024) {
      await page.keyboard.press("Escape");
      await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toBeFocused();
    }
    if (width === 2560) await page.getByRole("button", { name: "Collapse collections", exact: true }).click();
    for (const name of ["Edit", "Copy markdown", "Publish", `Actions for ${title}`]) {
      await expect(page.getByRole("region", { name: "Document workspace" }).getByRole("button", { name, exact: true })).toBeInViewport();
    }
    await expect(page.getByRole("tab", { name: "Raw", exact: true })).toBeInViewport();
    const flow = page.locator(".document-reading-flow .ProseMirror");
    const measures = await flow.evaluate(root => {
      const box = (selector: string) => {
        const el = root.querySelector(selector)!;
        const rect = el.getBoundingClientRect();
        return { x: rect.x, width: rect.width, top: rect.top, margin: getComputedStyle(el).marginTop };
      };
      return { heading: box("h2"), paragraph: box("p"), table: box(".akb-md-table"), list: box("ul") };
    });
    for (const box of [measures.paragraph, measures.table, measures.list]) {
      expect(box.x).toBeCloseTo(measures.heading.x, 0);
      expect(box.width).toBeCloseTo(measures.heading.width, 0);
    }
    expect(measures.heading.margin).toBe("0px");
    expect(measures.paragraph.width).toBeLessThanOrEqual(1025);
    const innerTable = await flow.locator("table").boundingBox();
    // The wrapper owns a 1px border on either side.
    expect(Math.abs(innerTable!.x - measures.table.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(innerTable!.width - measures.table.width)).toBeLessThanOrEqual(2);
    const code = page.getByRole("region", { name: "Scrollable code block" });
    expect(await code.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
    const image = await flow.getByRole("img", { name: "Small diagram" }).boundingBox();
    expect(image!.width).toBe(80);
    expect(image!.height).toBe(40);
    if (width === 375) {
      const commands = await page.locator('[data-slot="resource-command-row"]').boundingBox();
      expect(commands!.height).toBeLessThanOrEqual(54);
    }
    if (width >= 1440) expect(measures.heading.top).toBeLessThanOrEqual(220);
    await expect(page.getByRole("region", { name: "Scrollable table" })).toHaveAttribute("tabindex", "0");
    await expect(page.getByRole("region", { name: "Scrollable code block" })).toHaveAttribute("tabindex", "0");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("reader.png") });
    const currentTitle = location.locator('[aria-current="page"]');
    await expect(currentTitle).not.toHaveAttribute("aria-haspopup");
    await currentTitle.click();
    await expect(page.getByRole("menu")).toHaveCount(0);
    await location.getByRole("link", { name: "fixture", exact: true }).focus();
    await currentTitle.focus();
    if (await currentTitle.evaluate(element => element.scrollWidth > element.clientWidth + 1)) {
      await expect(page.getByRole("tooltip")).toHaveText(title);
    }
    await page.keyboard.press("Escape");
    await location.getByRole("button", { name: "Show collection ancestry" }).click();
    await expect(page.getByRole("menuitem", { name: "Team practices" })).toHaveAttribute("href", /collection=guides%2Fteam/);
    await expect(page.getByRole("menuitem", { name: "Operations" })).toHaveAttribute("href", /collection=guides%2Fteam%2Foperations/);
    await page.keyboard.press("Escape");
    if (width === 2560) {
      await page.getByRole("region", { name: "Document workspace" }).getByRole("button", { name: `Actions for ${title}` }).click();
      await page.getByRole("menuitemradio", { name: "Wide", exact: true }).click();
      await expect(page.locator(".document-reading-wide")).toBeVisible();
      const wide = await flow.locator("p").first().boundingBox();
      expect(wide!.width).toBeGreaterThan(measures.paragraph.width);
    }
    const actions = page.getByRole("region", { name: "Document workspace" }).getByRole("button", { name: `Actions for ${title}` });
    await actions.click();
    for (const name of ["Document info", "Table of contents", "Relations", "History", "Version history"]) {
      await expect(page.getByRole("menuitem", { name, exact: true })).toHaveCount(0);
    }
    await page.keyboard.press("Escape");
    await expect(actions).toBeFocused();
    const rail = page.locator('[data-slot="document-context-rail"]');
    for (const [name, icon] of [["Document info", "info"], ["Table of contents", "list-tree"], ["Relations", "link-2"], ["Version history", "history"]]) {
      const trigger = rail.getByRole("button", { name, exact: true });
      await expect(trigger).toBeInViewport();
      await expect(trigger.locator("svg")).toHaveAttribute("aria-hidden", "true");
      await expect(trigger.locator("svg")).toHaveClass(new RegExp(`lucide-${icon}(?: |$)`));
      await expect(trigger).toHaveAttribute("aria-expanded", "false");
    }
    const canvas = page.locator("#document-reading-canvas");
    const beforeInfo = (await canvas.boundingBox())!;
    const floating = await rail.evaluate(element => {
      const bounds = element.parentElement!.getBoundingClientRect();
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
      return bounds.width >= 48 * rem && bounds.height >= 20 * rem;
    });
    if (width === 375) expect(floating).toBe(false);
    if (width === 2560) expect(floating).toBe(true);
    const info = rail.getByRole("button", { name: "Document info", exact: true });
    await info.click();
    const panel = page.getByRole(floating ? "complementary" : "dialog", { name: "Document info", exact: true });
    await expect(panel).toHaveAttribute("data-mode", floating ? "floating" : "overlay");
    await expect(panel.getByRole("heading", { name: "Document info", exact: true })).toBeVisible();
    await expect(panel.getByText(title, { exact: true })).toBeVisible();
    await expect(panel.locator(`time[datetime="${new Date(lastEdited).toISOString()}"]`)).toBeVisible();
    await expect(panel.locator(`time[datetime="${new Date(createdAt).toISOString()}"]`)).toBeVisible();
    const close = panel.getByRole("button", { name: "Close document panel", exact: true });
    await expect(close).toBeFocused();
    await expect(page.getByRole("group", { name: "Document context", exact: true })).toHaveCount(1);
    const afterInfo = (await canvas.boundingBox())!;
    expect(afterInfo).toEqual(beforeInfo);
    if (floating) {
      const panelBox = (await panel.boundingBox())!;
      expect(panelBox.x).toBeGreaterThan(afterInfo.x);
      expect(panelBox.width).toBeGreaterThanOrEqual(360);
      expect(panelBox.width).toBeLessThanOrEqual(400);
      expect(panelBox.x + panelBox.width).toBeLessThanOrEqual((await rail.boundingBox())!.x);
      expect(panelBox.y + panelBox.height).toBeLessThanOrEqual(beforeInfo.y + beforeInfo.height);
    } else {
      await expect(panel).toHaveAttribute("aria-modal", "true");
      await page.keyboard.press("Shift+Tab");
      expect(await panel.evaluate(element => element.contains(document.activeElement))).toBe(true);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("document-context-info.png") });
    const controls = floating ? rail : panel;
    await controls.getByRole("button", { name: "Table of contents", exact: true }).click();
    const outline = page.getByRole(floating ? "complementary" : "dialog", { name: "On this page", exact: true });
    await expect(outline.getByRole("navigation", { name: "Document outline", exact: true })).toBeVisible();
    await expect(outline.getByRole("link", { name: "Authored heading", exact: true })).toBeVisible();
    const outlineControls = floating ? rail : outline;
    await outlineControls.getByRole("button", { name: "Relations", exact: true }).click();
    const relations = page.getByRole(floating ? "complementary" : "dialog", { name: "Relations", exact: true });
    await expect(relations.getByText("No relations yet.", { exact: true })).toBeVisible();
    await (floating ? rail : relations).getByRole("button", { name: "Version history", exact: true }).click();
    await expect(page.getByRole(floating ? "complementary" : "dialog", { name: "Version history", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(rail.getByRole("button", { name: "Version history", exact: true })).toBeFocused();
    await expect(page.locator('[data-slot="document-context-panel"]')).toHaveCount(0);
    expect((await canvas.boundingBox())!.width).toBe(beforeInfo.width);
    await info.click();
    await page.getByRole("button", { name: "Close document panel", exact: true }).click();
    await expect(info).toBeFocused();
    if (floating) {
      await info.click();
      await info.click();
      await expect(page.locator('[data-slot="document-context-panel"]')).toHaveCount(0);
      await expect(info).toBeFocused();
    } else {
      await rail.getByRole("button", { name: "Table of contents", exact: true }).click();
      await page.getByRole("dialog", { name: "On this page", exact: true }).getByRole("link", { name: "Authored heading", exact: true }).click();
      await expect(page.locator('[data-slot="document-context-panel"]')).toHaveCount(0);
      await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeFocused();
      await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeInViewport();
    }
  });
}

test("context tools remain reachable in a short landscape reader", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 280 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const rail = page.locator('[data-slot="document-context-rail"]');
  const history = rail.getByRole("button", { name: "Version history", exact: true });
  await expect(history).toBeVisible();
  expect(await rail.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
  await history.focus();
  await expect(history).toBeInViewport();
  await page.keyboard.press("Enter");
  const panel = page.getByRole("dialog", { name: "Version history", exact: true });
  await expect(panel.getByRole("button", { name: "Close document panel" })).toBeInViewport();
  await page.keyboard.press("Escape");
  await expect(history).toBeFocused();
});

test("a deep document reader opens every Vault page directly and browser Back restores its context", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
  await expect(navigation).toBeVisible();
  await expect(navigation.locator('[aria-current="page"]')).toHaveCount(0);
  const initialPosition = (await navigation.boundingBox())!;
  for (const [name, href] of vaultDestinations) {
    await openVaultDestination(page, name);
    await expect(page).toHaveURL(new RegExp(`${href}$`));
    await expect(navigation.getByRole("link", { name, exact: true })).toHaveAttribute("aria-current", "page");
    await expect(navigation.locator('[aria-current="page"]')).toHaveCount(1);
    expect(await navigation.boundingBox()).toEqual(initialPosition);
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`/doc/${encodeURIComponent(path)}$`));
    await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
    await expect(navigation.locator('[aria-current="page"]')).toHaveCount(0);
    await expect(page.locator(".app-header").getByRole("navigation", { name: "Resource location", exact: true })).toContainText(title);
  }
  await navigation.getByRole("link", { name: "Overview", exact: true }).focus();
  for (const [name] of vaultDestinations.slice(1)) {
    await page.keyboard.press("Tab");
    await expect(navigation.getByRole("link", { name, exact: true })).toBeFocused();
  }
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
  await expect(navigation.getByRole("link", { name: "Settings", exact: true })).toHaveAttribute("aria-current", "page");
  expect(await navigation.boundingBox()).toEqual(initialPosition);
  await page.screenshot({ path: testInfo.outputPath("vault-settings-navigation.png") });
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  await expect(navigation).toBeVisible();
  await expect(page.locator(".app-header").getByRole("navigation", { name: "Resource location", exact: true })).toBeVisible();
  await expect(page.locator('[data-slot="resource-command-row"]').getByRole("navigation", { name: "Resource location", exact: true })).toHaveCount(0);
  // Clicking the already selected Vault must navigate, not be swallowed as a
  // no-op merely because the document belongs to the same Vault.
  await page.getByRole("navigation", { name: "Vaults", exact: true }).getByRole("link", { name: "fixture, writer", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  await expect(navigation).toBeVisible();
  await page.goBack();
  await page.getByRole("navigation", { name: "Resource location", exact: true }).getByRole("link", { name: "fixture", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
});

test("maximum saved rail widths use a temporary drawer without changing preferences", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1024, height: 1000 });
  await fixture(page);
  await page.addInitScript(() => {
    localStorage.setItem("akb.vaultRailWidth.v2", "320");
    localStorage.setItem("akb.treeWidth.v2", "480");
    localStorage.setItem("akb.vaultRailCollapsed", "0");
    localStorage.setItem("akb.treeVisible", "1");
  });
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  await expect(page.locator('[data-navigation-mode="overlay"]')).toBeVisible();
  await expect(page.locator(".app-header").getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
  const location = page.getByRole("navigation", { name: "Resource location", exact: true });
  expect((await location.boundingBox())!.width).toBeGreaterThan(100);
  await expect(page.getByRole("button", { name: "Search knowledge", exact: true })).toBeInViewport();
  const open = page.getByRole("button", { name: "Open vault navigation", exact: true });
  await open.click();
  await expect(page.locator("#vault-workspace-navigation")).toBeInViewport();
  const collections = page.locator('[data-slot="vault-collections-sidebar"]');
  await expect(collections.getByRole("tree", { name: "fixture explorer", exact: true })).toBeVisible();
  await expect(collections.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(open).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("maximum-rails-compact.png") });
  // A Collection breadcrumb must reveal its destination even when the saved
  // desktop rails currently require the temporary drawer.
  await location.getByRole("link", { name: "Operations", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture\?collection=guides%2Fteam%2Foperations$/);
  await expect(page.locator("#vault-workspace-navigation")).toBeInViewport();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toBeFocused();
  await page.setViewportSize({ width: 1920, height: 1000 });
  await expect(page.locator('[data-navigation-mode="inline"]')).toBeVisible();
  await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toBeHidden();
  expect(await page.evaluate(() => ["akb.vaultRailWidth.v2", "akb.treeWidth.v2", "akb.vaultRailCollapsed", "akb.treeVisible"].map(key => localStorage.getItem(key)))).toEqual(["320", "480", "0", "1"]);
});

for (const dark of [false, true]) test(`working-page navigation stays outside content scrolling and independent from collection collapse in ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await fixture(page, dark);
  await page.goto("/vault/fixture");
  const collections = page.locator('[data-slot="vault-collections-sidebar"]');
  const tree = collections.getByRole("tree", { name: "fixture explorer", exact: true });
  await expect(tree).toBeVisible();
  const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
  const position = (await navigation.boundingBox())!;
  expect(position.height).toBe(40);
  const rightInset = await navigation.evaluate(element => parseFloat(getComputedStyle(element).paddingRight));
  const settingsPosition = (await navigation.getByRole("link", { name: "Settings", exact: true }).boundingBox())!;
  const publicPosition = (await navigation.getByRole("link", { name: "Public links", exact: true }).boundingBox())!;
  const membersPosition = (await navigation.getByRole("link", { name: "Members", exact: true }).boundingBox())!;
  expect(settingsPosition.x + settingsPosition.width).toBeLessThanOrEqual(position.x + position.width - rightInset);
  expect(settingsPosition.x - membersPosition.x - membersPosition.width).toBeLessThanOrEqual(8);
  expect(membersPosition.x - publicPosition.x - publicPosition.width).toBeLessThanOrEqual(24);
  const managementRow = (await collections.locator('[data-slot="collection-management-row"]').boundingBox())!;
  expect(position.y + position.height).toBe(managementRow.y + managementRow.height);
  for (const [name, href] of vaultDestinations) {
    await openVaultDestination(page, name);
    await expect(page).toHaveURL(new RegExp(`${href}$`));
    await expect(navigation.getByRole("link", { name, exact: true })).toHaveAttribute("aria-current", "page");
    await expect(tree).toBeVisible();
    await expectQuietVaultTrail(page, name);
    expect(await navigation.boundingBox()).toEqual(position);
    await expect(page.locator(".app-header").getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
    await expect(collections.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
    await expect(page.locator('[data-slot="vault-route-viewport"]').getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
  }
  const viewport = page.locator('[data-slot="vault-route-viewport"]');
  await viewport.evaluate(element => { element.scrollTop = element.scrollHeight; });
  expect(await navigation.boundingBox()).toEqual(position);
  await page.screenshot({ path: testInfo.outputPath("working-page-navigation.png") });
  await page.getByRole("button", { name: "Collapse collections", exact: true }).click();
  const expand = page.getByRole("button", { name: "Expand collections", exact: true });
  await expect(expand).toBeInViewport();
  await expect(collections).toBeHidden();
  await expect(navigation).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("akb.treeVisible"))).toBe("0");
  await page.goBack();
  await expect(expand).toBeInViewport();
  await expect(collections).toBeHidden();
  await expect(navigation.getByRole("link", { name: "Members", exact: true })).toHaveAttribute("aria-current", "page");
  await expand.click();
  await expect(tree).toBeVisible();
  expect(await navigation.boundingBox()).toEqual(position);
  expect(await page.evaluate(() => localStorage.getItem("akb.treeVisible"))).toBe("1");
  await page.goto("/vault/fixture/activity");
  await expect(navigation).toBeVisible();
  await expect(navigation.getByRole("link")).toHaveCount(5);
  await expect(navigation.locator('[aria-current="page"]')).toHaveCount(0);
});

test("Vault navigation measures overflow from content width without changing the viewport", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture");
  const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
  const content = navigation.locator("..");
  // Constrain the actual content container while the desktop viewport and
  // saved sidebars stay put. ResizeObserver must redistribute real links.
  await expect(page.locator('[data-navigation-mode="inline"]')).toBeVisible();
  await content.evaluate(element => { element.style.maxWidth = "340px"; });
  const more = navigation.getByRole("button", { name: "More vault pages", exact: true });
  await expect(more).toBeVisible();
  const compact = (await navigation.boundingBox())!;
  expect(compact.height).toBe(40);
  await expectVaultDestinationsReachable(page);
  await navigation.screenshot({ path: testInfo.outputPath("content-width-compact.png") });
  await content.evaluate(element => { element.style.maxWidth = "850px"; });
  await expect(more).toBeHidden();
  await expect(navigation.getByRole("link")).toHaveCount(5);
  const wide = (await navigation.boundingBox())!;
  expect(wide.width).toBeGreaterThan(compact.width);
  expect(wide.height).toBe(compact.height);
  expect(page.viewportSize()).toEqual({ width: 1440, height: 1000 });
  const settings = navigation.getByRole("link", { name: "Settings", exact: true });
  const wideSettings = (await settings.boundingBox())!;
  const rightInset = await navigation.evaluate(element => parseFloat(getComputedStyle(element).paddingRight));
  expect(wideSettings.x + wideSettings.width).toBeLessThanOrEqual(wide.x + wide.width - rightInset);
  const wideMembers = (await navigation.getByRole("link", { name: "Members", exact: true }).boundingBox())!;
  expect(wideSettings.x - wideMembers.x - wideMembers.width).toBeLessThanOrEqual(8);
  await content.evaluate(element => { element.style.maxWidth = "340px"; });
  await expect(more).toBeVisible();
  expect(await navigation.boundingBox()).toEqual(compact);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const width of [1440, 768]) {
  test(`short-height collection sidebar remains reachable without an empty outer gutter at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await fixture(page);
    await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
    await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
    if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
    const collections = page.locator('[data-slot="vault-collections-sidebar"]');
    const collapse = collections.getByRole("button", { name: "Collapse collections", exact: true });
    const tree = collections.getByRole("tree", { name: "fixture explorer", exact: true });
    await expect(collapse).toBeInViewport();
    await expect(collections.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
    const emptyGutter = () => collections.evaluate(element => ({
      style: getComputedStyle(element).scrollbarGutter,
      overflow: element.scrollHeight > element.clientHeight,
      reservedWidth: element.getBoundingClientRect().width - element.clientWidth,
    }));
    await expect.poll(emptyGutter).toEqual({ style: "auto", overflow: false, reservedWidth: 0 });

    await collapse.focus();
    await page.setViewportSize({ width, height: width >= 1024 ? 240 : 360 });
    await expect.poll(async () => (await collections.boundingBox())!.height).toBe(width >= 1024 ? 240 : 248);
    // The restored sidebar has no footer destinations. Its header remains
    // reachable while focus reveals a document inside the independent tree.
    const document = tree.getByRole("treeitem", { name: `Document: ${title}`, exact: true });
    await document.focus();
    await expect(document).toBeInViewport();
    await expect.poll(() => tree.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    await collapse.focus();
    await expect(collapse).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("short-height-collections.png") });

    await page.setViewportSize({ width, height: 1000 });
    await expect(collapse).toBeInViewport();
    await expect.poll(emptyGutter).toEqual({ style: "auto", overflow: false, reservedWidth: 0 });
  });
}

test("resizing an open Vault overflow closes it without reopening or losing keyboard focus", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
  const more = navigation.getByRole("button", { name: "More vault pages", exact: true });
  await more.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("End");
  await expect(page.getByRole("menuitem", { name: "Settings", exact: true })).toBeFocused();
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(navigation.getByRole("link", { name: "Settings", exact: true })).toBeFocused();
  await page.setViewportSize({ width: 375, height: 900 });
  await expect(more).toHaveAttribute("aria-expanded", "false");
  await expect(more).toBeFocused();
  await expect(page.getByRole("menu")).toHaveCount(0);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(more).toBeFocused();
});

test("Vault navigation adapts to enlarged text and a short landscape viewport", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await fixture(page);
  await page.goto("/vault/fixture/settings");
  const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
  await expect(navigation).toBeVisible();
  await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
  await expectVaultDestinationsReachable(page);
  await expect(navigation.getByRole("link", { name: "Settings", exact: true })).toHaveAttribute("aria-current", "page");
  const sameRow = () => navigation.evaluate(element => {
    const box = element.getBoundingClientRect();
    return Array.from(element.querySelectorAll<HTMLElement>(":scope > a, :scope > button")).every(control => {
      const rect = control.getBoundingClientRect();
      return rect.y === box.y && rect.right <= box.right && control.scrollWidth <= control.clientWidth;
    });
  });
  await expect.poll(sameRow).toBe(true);
  await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
  await page.setViewportSize({ width: 844, height: 390 });
  await expectVaultDestinationsReachable(page);
  await expect.poll(sameRow).toBe(true);
  await expect(navigation).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const width of [375, 768]) {
  test(`working-page navigation stays on one row and tracks its active destination at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await fixture(page);
    await page.goto("/vault/fixture");
    const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
    await expect(navigation).toBeVisible();
    await expectVaultDestinationsReachable(page);
    await expect(navigation.getByRole("link", { name: "Overview", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(navigation.locator('[aria-current="page"]')).toHaveCount(1);
    await expectQuietVaultTrail(page, "Overview");
    const navBox = (await navigation.boundingBox())!;
    expect(navBox.height).toBe(44);
    for (const control of await navigation.getByRole("link").or(navigation.getByRole("button")).all()) {
      await expect(control).toBeInViewport();
      const box = (await control.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
      expect(box.y).toBe(navBox.y);
      expect(box.height).toBe(44);
      expect(await control.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    }
    const more = navigation.getByRole("button", { name: "More vault pages", exact: true });
    if (width === 375) {
      await expect(more).toBeVisible();
      await more.focus();
      await page.keyboard.press("Enter");
      const menu = page.getByRole("menu");
      await expect(menu).toBeVisible();
      await expect(menu.getByRole("menuitem").first()).toBeFocused();
      await page.screenshot({ path: testInfo.outputPath("vault-overflow-menu-mobile.png") });
      await page.keyboard.press("End");
      await expect(menu.getByRole("menuitem", { name: "Settings", exact: true })).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
      await expect(menu).toHaveCount(0);
      await expect(navigation.getByRole("link", { name: "Settings", exact: true })).toHaveAttribute("aria-current", "page");
      await expect(navigation.getByRole("link", { name: "Overview", exact: true })).toBeInViewport();
      await expectQuietVaultTrail(page, "Settings");
      await expectVaultDestinationsReachable(page);
      await page.goBack();
      await expect(navigation.getByRole("link", { name: "Overview", exact: true })).toHaveAttribute("aria-current", "page");
    } else await expect(more).toBeHidden();
    await openVaultDestination(page, "Members");
    await expect(page).toHaveURL(/\/vault\/fixture\/members$/);
    await expect(navigation.getByRole("link", { name: "Members", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(navigation.locator('[aria-current="page"]')).toHaveCount(1);
    await expect(navigation.getByRole("link", { name: "Overview", exact: true })).toBeInViewport();
    expect(await navigation.boundingBox()).toEqual(navBox);
    await expect(page.getByTestId("members-workspace-frame")).toBeVisible();
    await expectQuietVaultTrail(page, "Members");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("working-navigation-mobile.png") });
  });
}

for (const width of [640, 768]) {
  test(`narrow app header keeps every control fully inside ${width}px with a long account name`, async ({ page }) => {
    await page.setViewportSize({ width, height: 720 });
    await fixture(page);
    const displayName = "Reading reviewer with a deliberately long display name";
    await page.route("**/api/v1/auth/me", route => route.fulfill({ json: {
      user_id: "reading-fixture", username: "fixture", display_name: displayName,
      email: "reader@example.invalid", auth_method: "local", is_admin: false,
    } }));
    await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
    await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
    const header = page.locator(".app-header");
    const mobileNavigation = header.getByRole("navigation", { name: "Primary mobile navigation", exact: true });
    const logo = header.getByRole("link", { name: "AKB home", exact: true });
    const search = header.getByRole("button", { name: "Search knowledge", exact: true });
    const account = header.getByRole("button", { name: `Account menu — ${displayName}`, exact: true });
    await expect(logo).toContainText("AKB");
    const controls = [
      logo,
      search,
      mobileNavigation.getByRole("link", { name: "Home", exact: true }),
      mobileNavigation.getByRole("link", { name: "Vaults", exact: true }),
      header.getByRole("button", { name: /^Notifications/ }),
      account,
];
    const headerBox = (await header.boundingBox())!;
    let previousRight = 0;
    for (const control of controls) {
      await expect(control).toBeVisible();
      const box = (await control.boundingBox())!;
      // Partial visibility passes toBeInViewport, so check all four edges and
      // neighboring controls explicitly instead of accepting a clipped button.
      expect(box.x).toBeGreaterThanOrEqual(previousRight);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
      expect(box.y).toBeGreaterThanOrEqual(headerBox.y);
      expect(box.y + box.height).toBeLessThanOrEqual(headerBox.y + headerBox.height);
      expect(box.width).toBeGreaterThanOrEqual(32);
      previousRight = box.x + box.width;
    }
    await account.click();
    await expect(page.getByRole("menu")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(account).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("preview owns local location, restores Search, and promotes Raw to the Vault", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/search?q=reading&source=document");
  const result = page.getByRole("link", { name: new RegExp("문서 읽기 작업공간") }).first();
  await result.click();
  const reader = page.getByTestId("document-preview-dialog");
  await expect(reader.getByRole("navigation", { name: "Resource location", exact: true })).toContainText(title);
  await expect(reader.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
  await expect(reader.locator('[data-slot="resource-command-row"]').getByRole("navigation", { name: "Resource location", exact: true })).toHaveCount(0);
  await expect(page.locator('.app-header [aria-label="Current page"]')).toHaveText("Search");
  await reader.getByRole("tab", { name: "Raw", exact: true }).click();
  await expect(reader.getByTestId("doc-raw")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(reader).toHaveCount(0);
  await expect(page).toHaveURL(/\/search\?q=reading&source=document/);
  await expect(result).toBeFocused();
  await result.click();
  await reader.getByRole("tab", { name: "Raw", exact: true }).click();
  await reader.getByRole("button", { name: "Open document in vault" }).click();
  await expect(reader).toHaveCount(0);
  await expect(page.getByTestId("doc-raw")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Resource location", exact: true })).toContainText(title);
  await expectVaultDestinationsReachable(page);
  await expect(page.getByRole("navigation", { name: "Vault sections", exact: true }).locator('[aria-current="page"]')).toHaveCount(0);
});

test("Vault Search preview hides inactive section navigation and restores it on close", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture/search?q=reading&source=document");
  const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
  await expect(navigation.locator('[aria-current="page"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Search knowledge", exact: true })).toBeVisible();
  const initialPosition = await navigation.boundingBox();
  const result = page.getByRole("link", { name: /문서 읽기 작업공간/ }).first();
  await result.click();
  const preview = page.getByTestId("document-preview-dialog");
  await expect(preview).toBeVisible();
  await expect(preview.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
  await expect(navigation).toHaveCount(0);
  await preview.getByRole("tab", { name: "Raw", exact: true }).click();
  await expect(navigation).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(preview).toHaveCount(0);
  await expect(page).toHaveURL(/\/vault\/fixture\/search\?q=reading&source=document$/);
  await expect(result).toBeFocused();
  await expect(navigation.locator('[aria-current="page"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Search knowledge", exact: true })).toBeVisible();
  expect(await navigation.boundingBox()).toEqual(initialPosition);
});

for (const width of [375, 1440]) test(`read-mode segments retain manual keyboard activation at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const rendered = page.getByRole("tab", { name: "Preview", exact: true });
  const raw = page.getByRole("tab", { name: "Raw", exact: true });
  await expect(rendered).toHaveAttribute("aria-selected", "true");
  await rendered.focus();
  await page.keyboard.press("ArrowRight");
  await expect(raw).toBeFocused();
  await expect(rendered).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("doc-raw")).toHaveCount(0);
  await page.keyboard.press("Enter");
  await expect(raw).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("doc-raw")).toBeVisible();
  await page.keyboard.press("Home");
  await expect(rendered).toBeFocused();
  await expect(raw).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Space");
  await expect(rendered).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  for (const tab of [rendered, raw]) {
    expect(await tab.evaluate(element => getComputedStyle(element, "::after").content)).toBe("none");
    expect(await tab.evaluate(element => parseFloat(getComputedStyle(element).borderRadius))).toBeGreaterThan(0);
  }
  expect(await rendered.evaluate(element => getComputedStyle(element).backgroundColor)).not.toBe(await raw.evaluate(element => getComputedStyle(element).backgroundColor));
});

for (const width of [375, 1440]) test(`modified keyboard Vault links keep a dirty editor in its original tab at ${width}px`, async ({ page, context }) => {
  // Popups must also remain isolated from the real local backend. Page-owned
  // fixtures take precedence in the original tab; the new tab only tests linking.
  await context.route("**/api/v1/**", route => route.fulfill({ json: {} }));
  await page.setViewportSize({ width, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}?view=edit`);
  const editor = page.getByRole("textbox", { name: "Document body (markdown)" });
  await editor.fill("Keep editing here while checking Vault settings in another tab.");
  await expect(page.getByText("Draft saved locally", { exact: true })).toBeVisible();
  const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
  let destination = navigation.getByRole("link", { name: "Settings", exact: true });
  if (!await destination.count()) {
    await navigation.getByRole("button", { name: "More vault pages", exact: true }).click();
    destination = page.getByRole("menuitem", { name: "Settings", exact: true });
  }
  await destination.focus();
  // Browser-native new-tab gestures do not necessarily retain a JS opener.
  const popupPromise = context.waitForEvent("page");
  await page.keyboard.press("ControlOrMeta+Enter");
  const popup = await popupPromise;
  await expect(popup).toHaveURL(/\/vault\/fixture\/settings$/);
  await popup.close();
  await expect(page).toHaveURL(/\?view=edit$/);
  await expect(page.getByRole("dialog", { name: "Leave this document?", exact: true })).toHaveCount(0);
  await expect(editor).toContainText("Keep editing here");
});

for (const width of [375, 1440]) test(`Vault navigation protects an unsaved editor draft at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}?view=edit`);
  const editor = page.getByRole("textbox", { name: "Document body (markdown)" });
  const draft = "An unsaved draft kept while reviewing another Vault page.";
  await editor.fill(draft);
  await expect(page.getByText("Draft saved locally", { exact: true })).toBeVisible();
  await openVaultDestination(page, "Settings");
  const confirmation = page.getByRole("dialog", { name: "Leave this document?", exact: true });
  await expect(confirmation).toBeVisible();
  await expect(page).toHaveURL(/\?view=edit$/);
  await expect(page.getByRole("menu")).toHaveCount(0);
  await confirmation.getByRole("button", { name: "Keep editing", exact: true }).click();
  await expect(confirmation).toHaveCount(0);
  await expect(editor).toContainText(draft);
  await openVaultDestination(page, "Settings");
  await confirmation.getByRole("button", { name: "Leave document", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
  await expect(page.getByRole("navigation", { name: "Vault sections", exact: true }).getByRole("link", { name: "Settings", exact: true })).toHaveAttribute("aria-current", "page");
});

test("toolbar publishing opens a review, never publishes on disclosure, and restores focus", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  let mutations = 0;
  page.on("request", request => {
    if (new URL(request.url()).pathname.includes("/publications") && request.method() !== "GET") mutations += 1;
  });
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const publish = page.getByRole("region", { name: "Document workspace" }).getByRole("button", { name: "Publish", exact: true });
  await expect(publish).not.toHaveAttribute("aria-disabled", "true");
  await publish.click();
  const review = page.getByRole("dialog", { name: "Publish document" });
  await expect(review).toHaveAttribute("data-side", "bottom");
  await expect(page.locator('[data-slot="dialog-overlay"]')).toHaveCount(0);
  await expect(review.getByText(/without signing in/)).toBeVisible();
  await expect(review.getByRole("button", { name: "Publish", exact: true })).toBeVisible();
  expect(mutations).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("publish-review.png") });
  await review.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(publish).toBeFocused();
  expect(mutations).toBe(0);
});

for (const scenario of [
  { name: "desktop", width: 1440, height: 1000, dark: false, preview: false },
  { name: "dark", width: 1440, height: 1000, dark: true, preview: false },
  { name: "mobile", width: 375, height: 812, dark: false, preview: false },
  { name: "landscape", width: 844, height: 390, dark: false, preview: false },
  { name: "preview", width: 1440, height: 780, dark: false, preview: true },
]) test(`publishing popover retains access limits through retry and opens the ready link in ${scenario.name}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: scenario.width, height: scenario.height });
  await fixture(page, scenario.dark);
  const payloads: unknown[] = [];
  await page.route("**/api/v1/publications/**", async route => {
    if (route.request().method() === "POST") {
      payloads.push(route.request().postDataJSON());
      if (payloads.length === 1) return route.fulfill({ status: 503, json: { detail: "Publication temporarily unavailable." } });
      return route.fulfill({ json: {
        slug: "new-public-link", resource_type: "document",
        resource_uri: "akb://fixture/coll/guides/team/operations/doc/reading.md",
        share_url: `${new URL(page.url()).origin}/p/new-public-link`, vault: "fixture", title,
        mode: "live", expires_at: "2026-09-28T00:00:00Z", max_views: 25, view_count: 0,
        allow_embed: true, section_filter: null, password_protected: true, created_at: lastEdited, snapshot_at: null,
      } });
    }
    return route.fulfill({ json: { publications: [] } });
  });
  if (scenario.preview) {
    await page.goto("/search?q=reading&source=document");
    await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
  } else await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const trigger = page.getByRole("button", { name: "Publish", exact: true });
  await expect(trigger).toBeVisible();
  const canvas = page.locator("#document-reading-canvas");
  const before = await canvas.boundingBox();
  const overlayCount = await page.locator('[data-slot="dialog-overlay"]').count();
  await trigger.click();
  const panel = page.getByRole("dialog", { name: "Publish document", exact: true });
  await expect(panel).toBeVisible();
  await expect(page.locator('[data-slot="dialog-overlay"]')).toHaveCount(overlayCount);
  await panel.getByRole("checkbox", { name: /Require password/ }).check();
  const password = panel.getByLabel("Publication password", { exact: true });
  await password.fill("fixture-passphrase");
  await expect(password).toBeInViewport({ ratio: 1 });
  await panel.getByRole("button", { name: "Show password", exact: true }).click();
  await expect(password).toHaveAttribute("type", "text");
  await panel.getByRole("button", { name: "7 days", exact: true }).click();
  await panel.getByLabel("Max views", { exact: true }).fill("25");
  await expect(panel.getByLabel("Max views", { exact: true })).toBeInViewport({ ratio: 1 });
  const submit = panel.getByRole("button", { name: "Publish", exact: true });
  await expect(submit).toBeInViewport();
  const box = (await panel.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(7);
  expect(box.x + box.width).toBeLessThanOrEqual(scenario.width - 7);
  expect(box.y).toBeGreaterThanOrEqual(7);
  expect(box.y + box.height).toBeLessThanOrEqual(scenario.height - 7);
  expect(await canvas.boundingBox()).toEqual(before);
  await page.screenshot({ path: testInfo.outputPath("publish-options-popover.png") });
  expect(payloads).toHaveLength(0);
  await submit.click();
  await expect(panel.getByRole("alert")).toContainText("Publication temporarily unavailable.");
  await expect(panel.getByRole("alert")).toBeInViewport();
  await expect(password).toHaveValue("fixture-passphrase");
  await expect(panel.getByLabel("Max views", { exact: true })).toHaveValue("25");
  await submit.click();
  const ready = page.getByRole("dialog", { name: "Public link", exact: true });
  await expect(ready).toBeVisible();
  const publicUrl = ready.getByRole("textbox", { name: "Public URL", exact: true });
  await expect(publicUrl).toHaveValue(`${new URL(page.url()).origin}/p/new-public-link`);
  await expect(publicUrl).toBeFocused();
  await expect(ready.getByRole("button", { name: "Copy link", exact: true })).toBeVisible();
  expect(payloads).toEqual([1, 2].map(() => ({
    resource_type: "document", uri: "akb://fixture/coll/guides/team/operations/doc/reading.md", title,
    password: "fixture-passphrase", expires_in: "7d", max_views: 25, // pragma: allowlist secret — synthetic publication fixture
  })));
  expect(await canvas.boundingBox()).toEqual(before);
  await page.keyboard.press("Escape");
  await expect(ready).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Public link", exact: true })).toBeFocused();
  if (scenario.preview) await expect(page.getByTestId("document-preview-dialog")).toBeVisible();
});

test("shared file publishing form reveals errors within its short dialog", async ({ page }) => {
  await page.setViewportSize({ width: 844, height: 390 });
  await fixture(page);
  await page.route("**/reading-file.txt", route => route.fulfill({ contentType: "text/plain", body: "A readable attachment." }));
  await page.route("**/api/v1/files/fixture/f-reading", route => route.fulfill({ json: {
    uri: "akb://fixture/coll/guides/team/operations/file/f-reading", name: "Reading notes.txt",
    collection: "guides/team/operations", mime_type: "text/plain", size_bytes: 22,
  } }));
  await page.route("**/api/v1/files/fixture/f-reading/download", route => route.fulfill({ json: {
    name: "Reading notes.txt", mime_type: "text/plain", download_url: "/reading-file.txt", size_bytes: 22,
  } }));
  await page.route("**/api/v1/publications/fixture/create", route => route.fulfill({ status: 503, json: { detail: "Publication temporarily unavailable." } }));
  await page.goto("/vault/fixture/file/f-reading");
  await page.getByRole("button", { name: "Actions for Reading notes.txt", exact: true }).click();
  await page.getByRole("menuitem", { name: "Publish file", exact: true }).click();
  const panel = page.getByRole("dialog", { name: "Publish file", exact: true });
  await panel.getByRole("checkbox", { name: /Require password/ }).check();
  await panel.getByLabel("Publication password", { exact: true }).fill("fixture-passphrase");
  await panel.getByLabel("Max views", { exact: true }).fill("25");
  const before = await page.locator('[data-slot="resource-command-row"]').boundingBox();
  await panel.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(panel.getByRole("alert")).toContainText("Publication temporarily unavailable.");
  await expect(panel.getByRole("alert")).toBeInViewport({ ratio: 1 });
  await expect(panel.getByLabel("Max views", { exact: true })).toHaveValue("25");
  expect(await page.locator('[data-slot="resource-command-row"]').boundingBox()).toEqual(before);
});

for (const dark of [false, true]) test(`public-link popover anchors to its button without changing the reader in ${dark ? "dark" : "light"} mode`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page, dark, false, body, "guide-public");
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const trigger = page.getByRole("button", { name: "Public link", exact: true });
  await expect(trigger).toBeVisible();
  const canvas = page.locator("#document-reading-canvas");
  const before = await canvas.boundingBox();
  const scrollBefore = await canvas.evaluate(element => element.scrollTop);
  await trigger.click();
  const panel = page.getByRole("dialog", { name: "Public link", exact: true });
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute("data-side", "bottom");
  const anchor = (await trigger.boundingBox())!;
  const popup = (await panel.boundingBox())!;
  expect(Math.abs(popup.y - anchor.y - anchor.height - 8)).toBeLessThan(1);
  expect(Math.abs(popup.x + popup.width - anchor.x - anchor.width)).toBeLessThan(1);
  expect(popup.width).toBe(340);
  expect(await canvas.boundingBox()).toEqual(before);
  expect(await canvas.evaluate(element => element.scrollTop)).toBe(scrollBefore);
  await expect(page.locator('[data-slot="dialog-overlay"]')).toHaveCount(0);
  expect(await page.locator("body").evaluate(element => getComputedStyle(element).pointerEvents)).not.toBe("none");
  await page.screenshot({ path: testInfo.outputPath("public-link-popover.png") });

  // The first outside click must both close the popover and perform its action.
  const raw = page.getByRole("tab", { name: "Raw", exact: true });
  await raw.click();
  await expect(panel).toHaveCount(0);
  await expect(raw).toHaveAttribute("aria-selected", "true");
  await expect(raw).toBeFocused();
  await trigger.click();
  await trigger.click();
  await expect(panel).toHaveCount(0);
  await trigger.click();
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await trigger.click();
  await panel.getByRole("button", { name: "Close public link" }).click();
  await expect(trigger).toBeFocused();
});

for (const viewport of [{ width: 375, height: 812 }, { width: 844, height: 390 }]) test(`public-link popover fits ${viewport.width}x${viewport.height} without moving the article`, async ({ page }) => {
  await page.setViewportSize(viewport);
  await fixture(page, false, true, body, "guide-public");
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const trigger = page.getByRole("button", { name: "Public link", exact: true });
  await expect(trigger).toBeVisible();
  const canvas = page.locator("#document-reading-canvas");
  const before = await canvas.boundingBox();
  await trigger.click();
  const panel = page.getByRole("dialog", { name: "Public link", exact: true });
  await expect(panel).toBeVisible();
  const box = (await panel.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(7);
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width - 7);
  expect(box.y).toBeGreaterThanOrEqual(7);
  expect(box.y + box.height).toBeLessThanOrEqual(viewport.height - 7);
  expect(await canvas.boundingBox()).toEqual(before);
  await panel.getByRole("button", { name: "Unpublish", exact: true }).scrollIntoViewIfNeeded();
  await expect(panel.getByRole("button", { name: "Unpublish", exact: true })).toBeInViewport();
  expect(await canvas.boundingBox()).toEqual(before);
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
});

for (const previewMode of [false, true]) test(`public-link popover preserves confirmation and copy focus in ${previewMode ? "preview" : "full-page"} reading`, async ({ page, context }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await fixture(page, false, false, body, "guide-public");
  let mutations = 0;
  page.on("request", request => {
    if (new URL(request.url()).pathname.includes("/api/") && request.method() !== "GET") mutations += 1;
  });
  if (previewMode) {
    await page.goto("/search?q=reading&source=document");
    await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
  } else await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const trigger = page.getByRole("button", { name: "Public link", exact: true });
  await expect(trigger).toBeVisible();
  const readingUrl = page.url();
  await trigger.click();
  const panel = page.getByRole("dialog", { name: "Public link", exact: true });
  await panel.getByRole("button", { name: "Copy link", exact: true }).click();
  await expect(panel.getByRole("status")).toHaveText("Link copied.");
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(`${new URL(page.url()).origin}/p/guide-public`);
  const unpublish = panel.getByRole("button", { name: "Unpublish", exact: true });
  await unpublish.click();
  const confirm = page.getByRole("dialog", { name: "Unpublish this document?", exact: true });
  await expect(confirm).toBeVisible();
  await expect(panel).toBeHidden();
  await expect(confirm.getByRole("button", { name: "Cancel" })).toBeFocused();
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await expect(confirm).toHaveCount(0);
  await expect(panel).toBeVisible();
  await expect(unpublish).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(trigger).toBeFocused();
  expect(mutations).toBe(0);
  if (previewMode) {
    const preview = page.getByTestId("document-preview-dialog");
    await expect(preview).toBeVisible();
    await trigger.click();
    await preview.locator("#document-reading-canvas").click({ position: { x: 40, y: 80 } });
    await expect(panel).toHaveCount(0);
    await expect(preview).toBeVisible();
    await expect(page).toHaveURL(readingUrl);
  }
});

test("outside action closes floating context without swallowing the click", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const actions = page.getByRole("region", { name: "Document workspace" }).getByRole("button", { name: `Actions for ${title}` });
  const context = page.getByRole("group", { name: "Document context", exact: true });
  await context.getByRole("button", { name: "Document info", exact: true }).click();
  const close = page.getByRole("button", { name: "Close document panel" });
  await expect(close).toBeFocused();
  await context.getByRole("button", { name: "Table of contents", exact: true }).click();
  await expect(page.getByRole("complementary", { name: "On this page", exact: true })).toBeVisible();
  await expect(context.getByRole("button", { name: "Table of contents", exact: true })).toHaveAttribute("aria-expanded", "true");
  await actions.click();
  await expect(page.locator('[data-slot="document-context-panel"]')).toHaveCount(0);
  await expect(page.getByRole("menu")).toBeVisible();
  await page.getByRole("menuitemradio", { name: "Wide", exact: true }).click();
  await expect(actions).toBeFocused();
  await actions.click();
  await page.keyboard.press("Escape");
  await expect(actions).toBeFocused();
  await context.getByRole("button", { name: "Table of contents", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(context.getByRole("button", { name: "Table of contents", exact: true })).toBeFocused();
});

test("floating context preserves article geometry, scroll and selection while open", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page, false, false, body + "\n\n" + "A long article paragraph. ".repeat(2000));
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const canvas = page.locator("#document-reading-canvas");
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  await canvas.evaluate(element => { element.scrollTop = 240; });
  const before = await canvas.boundingBox();
  const paragraph = page.locator(".document-reading-flow .ProseMirror > p").first();
  const beforeParagraph = await paragraph.boundingBox();
  await page.getByRole("button", { name: "Document info", exact: true }).click();
  expect(await canvas.boundingBox()).toEqual(before);
  expect(await paragraph.boundingBox()).toEqual(beforeParagraph);
  expect(await canvas.evaluate(element => element.scrollTop)).toBe(240);
  const panel = page.getByRole("complementary", { name: "Document info", exact: true });
  await panel.getByText(title, { exact: true }).click({ clickCount: 3 });
  await expect(panel).toBeVisible();
  await canvas.hover({ position: { x: 40, y: 80 } });
  await page.mouse.wheel(0, 200);
  await expect.poll(() => canvas.evaluate(element => element.scrollTop)).toBeGreaterThan(240);
  await expect(panel).toBeVisible();
  await canvas.click({ position: { x: 40, y: 80 } });
  await expect(panel).toHaveCount(0);
});

test("floating properties and relation dialogs preserve their inputs and inspector", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const rail = page.locator('[data-slot="document-context-rail"]');
  await rail.getByRole("button", { name: "Document info", exact: true }).click();
  const panel = page.locator('[data-slot="document-context-panel"]');
  const edit = panel.getByRole("button", { name: "Edit properties", exact: true });
  await edit.click();
  const form = page.getByRole("dialog", { name: "Edit details", exact: true });
  const summary = form.getByRole("textbox", { name: "SUMMARY", exact: true });
  await summary.fill("An unsaved properties draft");
  await form.getByRole("button", { name: "Document type", exact: true }).click();
  await page.getByRole("menuitemradio", { name: "reference", exact: true }).click();
  await expect(summary).toHaveValue("An unsaved properties draft");
  await expect(panel).toHaveCount(1);
  await page.keyboard.press("Escape");
  const discard = page.getByRole("dialog", { name: /Discard/ });
  await expect(discard).toBeVisible();
  await discard.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(summary).toHaveValue("An unsaved properties draft");
  await form.getByRole("button", { name: "Cancel", exact: true }).click();
  await discard.getByRole("button", { name: /Discard/ }).click();
  await expect(form).toHaveCount(0);
  await expect(panel).toBeVisible();
  await expect(edit).toBeFocused();

  await rail.getByRole("button", { name: "Relations", exact: true }).click();
  await panel.getByRole("button", { name: "Add", exact: true }).click();
  const relation = page.getByRole("dialog", { name: "Add relation", exact: true });
  await relation.getByRole("button", { name: "Relation type", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(relation).toBeVisible();
  await expect(panel).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(relation).toHaveCount(0);
  await expect(panel).toBeVisible();
});

test("floating context dismisses inside a preview without closing the preview", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto("/search?q=reading&source=document");
  await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
  const preview = page.getByTestId("document-preview-dialog");
  const info = preview.getByRole("button", { name: "Document info", exact: true });
  await info.click();
  const panel = page.getByRole("complementary", { name: "Document info", exact: true });
  await expect(panel).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(preview).toBeVisible();
  await expect(info).toBeFocused();
  await info.click();
  await preview.locator("#document-reading-canvas").click({ position: { x: 40, y: 80 } });
  await expect(panel).toHaveCount(0);
  await expect(preview).toBeVisible();
  const previewUrl = page.url();
  const previewHistory = await page.evaluate(() => history.state);
  await preview.getByRole("button", { name: "Table of contents", exact: true }).click();
  await page.getByRole("complementary", { name: "On this page", exact: true }).getByRole("link", { name: "Authored heading", exact: true }).click();
  await expect(page.locator('[data-slot="document-context-panel"]')).toHaveCount(0);
  await expect(preview.getByRole("heading", { name: "Authored heading", exact: true })).toBeFocused();
  await expect(page).toHaveURL(previewUrl);
  expect(await page.evaluate(() => history.state)).toEqual(previewHistory);
});

test("outline navigation in a short document never scrolls the Vault shell under the header", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1918, height: 844 });
  const content = "# Operations Runbook\n\nUse this runbook when search is degraded or the indexing backlog grows.\n\n## Checklist\n\n1. Confirm API liveness and database readiness.\n2. Inspect the pending indexing count.\n3. Verify vector-store reachability.\n4. Restart only the affected local worker when required.\n5. Confirm the backlog drains before closing the incident.";
  await fixture(page, false, false, content);
  await page.route("**/api/v1/**", route => {
    if (new URL(route.request().url()).pathname.includes("/browse/")) return route.fulfill({ json: { items: [
      { type: "collection", path: "guides", name: "Guides" },
      { type: "collection", path: "guides/team", name: "Team practices" },
      { type: "collection", path: "guides/team/operations", name: "Operations" },
      { type: "document", path, name: title },
      ...Array.from({ length: 50 }, (_, index) => ({ type: "document", path: `guides/team/operations/test-${index}.md`, name: `Related document ${index}` })),
    ] } });
    return route.fallback();
  });
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const canvas = page.locator("#document-reading-canvas");
  const heading = canvas.getByRole("heading", { name: "Checklist", exact: true });
  await expect(heading).toBeVisible();
  const workspaceMetrics = () => canvas.evaluate(element => {
    const parents = [];
    for (let node = element.parentElement; node; node = node.parentElement) parents.push({ tag: node.tagName, className: node.className, top: node.scrollTop, left: node.scrollLeft });
    const rect = element.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, parents };
  });
  const before = await workspaceMetrics();
  await page.locator('[data-slot="document-context-rail"]').getByRole("button", { name: "Table of contents", exact: true }).click();
  await page.getByRole("navigation", { name: "Document outline" }).getByRole("link", { name: "Checklist", exact: true }).click();
  await expect(heading).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("short-outline-navigation.png") });
  expect(await workspaceMetrics()).toEqual(before);
  expect(await canvas.evaluate(element => element.scrollTop)).toBe(0);
});

for (const scenario of ["desktop", "mobile", "preview"] as const) {
  test(`outline navigation scrolls only the article, not the workspace in ${scenario}`, async ({ page }) => {
    await page.setViewportSize({ width: scenario === "mobile" ? 375 : 2560, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const content = Array.from({ length: 16 }, (_, index) => `## Section ${index + 1}\n\n${"Reading content with enough height to navigate. ".repeat(70)}`).join("\n\n") + "\n\n## Final section";
    await fixture(page, false, false, content);
    if (scenario === "preview") {
      await page.goto("/search?q=reading");
      await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
    } else await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
    const canvas = page.locator("#document-reading-canvas");
    await expect(canvas.getByRole("heading", { name: "Section 1", exact: true })).toBeVisible();
    const workspaceMetrics = () => canvas.evaluate(element => {
      const parents = [];
      for (let node = element.parentElement; node; node = node.parentElement) {
        parents.push({ tag: node.tagName, scrollTop: node.scrollTop, scrollLeft: node.scrollLeft });
      }
      const rect = element.getBoundingClientRect();
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, parents };
    });
    const before = await workspaceMetrics();
    const innerMetrics = () => canvas.getByRole("heading", { name: "Section 12", exact: true }).evaluate(element => {
      const parents = [];
      for (let node = element.parentElement; node && node.id !== "document-reading-canvas"; node = node.parentElement) {
        parents.push({ tag: node.tagName, className: node.className, scrollTop: node.scrollTop, scrollLeft: node.scrollLeft });
      }
      return parents;
    });
    const innerBefore = await innerMetrics();
    const url = page.url();
    const history = await page.evaluate(() => window.history.state);
    const rail = page.locator('[data-slot="document-context-rail"]');
    await rail.getByRole("button", { name: "Table of contents", exact: true }).click();
    const panel = page.locator('[data-slot="document-context-panel"]');
    await expect(panel.getByRole("navigation", { name: "Document outline" })).toBeVisible();
    expect(await workspaceMetrics()).toEqual(before);
    await panel.getByRole("link", { name: "Section 12", exact: true }).click();
    await expect(panel).toHaveCount(0);
    const heading = canvas.getByRole("heading", { name: "Section 12", exact: true });
    await expect(heading).toBeFocused();
    await expect(heading).toBeInViewport();
    expect(await canvas.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    expect(await workspaceMetrics()).toEqual(before);
    expect(await innerMetrics()).toEqual(innerBefore);
    expect(page.url()).toBe(url);
    expect(await page.evaluate(() => window.history.state)).toEqual(history);
    // A later return to the first heading must not scroll hidden layout shells.
    await rail.getByRole("button", { name: "Table of contents", exact: true }).click();
    await panel.getByRole("link", { name: "Section 1", exact: true }).click();
    await expect(canvas.getByRole("heading", { name: "Section 1", exact: true })).toBeFocused();
    expect(await workspaceMetrics()).toEqual(before);
    await rail.getByRole("button", { name: "Table of contents", exact: true }).click();
    await panel.getByRole("link", { name: "Final section", exact: true }).click();
    await expect(canvas.getByRole("heading", { name: "Final section", exact: true })).toBeFocused();
    expect(await workspaceMetrics()).toEqual(before);
  });
}

test("resizing defers context mode changes until nested editing closes", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const panel = page.locator('[data-slot="document-context-panel"]');
  const rail = page.locator('[data-slot="document-context-rail"]');
  await rail.getByRole("button", { name: "Relations", exact: true }).click();
  await panel.getByRole("button", { name: "Add", exact: true }).click();
  const relation = page.getByRole("dialog", { name: "Add relation", exact: true });
  await expect(relation).toBeVisible();
  await page.setViewportSize({ width: 375, height: 800 });
  await expect(relation).toBeVisible();
  await expect(panel).toHaveAttribute("data-mode", "floating");
  await relation.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(relation).toHaveCount(0);
  await expect(panel).toHaveAttribute("data-mode", "overlay");
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);

  await page.setViewportSize({ width: 2560, height: 1000 });
  await rail.getByRole("button", { name: "Document info", exact: true }).click();
  await panel.getByRole("button", { name: "Edit properties", exact: true }).click();
  const details = page.getByRole("dialog", { name: "Edit details", exact: true });
  await details.getByRole("textbox", { name: "SUMMARY", exact: true }).fill("Preserve on resize");
  await page.setViewportSize({ width: 375, height: 800 });
  await expect(details).toBeVisible();
  await expect(details.getByRole("textbox", { name: "SUMMARY", exact: true })).toHaveValue("Preserve on resize");
  await expect(page.getByRole("dialog", { name: "Discard changes?", exact: true })).toHaveCount(0);
  await expect(panel).toHaveAttribute("data-mode", "floating");
  await details.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("dialog", { name: "Discard changes?", exact: true }).getByRole("button", { name: "Discard", exact: true }).click();
  await expect(details).toHaveCount(0);
  await expect(panel).toHaveAttribute("data-mode", "overlay");

  await panel.getByRole("button", { name: "Relations", exact: true }).click();
  await panel.getByRole("button", { name: "Add", exact: true }).click();
  await expect(relation).toBeVisible();
  await page.setViewportSize({ width: 2560, height: 1000 });
  await expect(relation).toBeVisible();
  await expect(panel).toHaveAttribute("data-mode", "overlay");
  await relation.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(relation).toHaveCount(0);
  await expect(panel).toHaveAttribute("data-mode", "floating");
  await expect(panel.getByRole("button", { name: "Close document panel", exact: true })).toBeFocused();
});

test("preview keeps location and full-page promotion together without section navigation", async ({ page }, testInfo) => {
  await fixture(page);
  await page.goto("/search?q=reading&source=document");
  await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
  const reader = page.getByTestId("document-preview-dialog");
  const location = reader.getByRole("navigation", { name: "Resource location", exact: true });
  expect((await location.boundingBox())!.width).toBeGreaterThan(300);
  const promote = reader.getByRole("button", { name: "Open document in vault", exact: true });
  await expect(promote).toBeInViewport();
  const locationBox = (await location.boundingBox())!;
  const promoteBox = (await promote.boundingBox())!;
  expect(Math.abs(locationBox.y + locationBox.height / 2 - promoteBox.y - promoteBox.height / 2)).toBeLessThan(1);
  await expect(reader.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("preview-location.png") });
  await location.getByRole("link", { name: "fixture", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  await expect(reader).toHaveCount(0);
});

test("Escape closes only the nested context overlay inside a document preview", async ({ page }) => {
  // Search opens full-page documents on mobile. A short desktop preview keeps
  // this nested-modal coverage without depending on saved sidebar widths.
  await page.setViewportSize({ width: 1024, height: 390 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await fixture(page);
  await page.goto("/search?q=reading&source=document");
  await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
  const preview = page.getByTestId("document-preview-dialog");
  await expect(preview).toBeVisible();
  const info = preview.getByRole("button", { name: "Document info", exact: true });
  await info.click();
  const context = page.getByRole("dialog", { name: "Document info", exact: true });
  await expect(context).toHaveAttribute("data-mode", "overlay");
  await expect(context.getByRole("button", { name: "Close document panel", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(context).toHaveCount(0);
  await expect(preview).toBeVisible();
  await expect(info).toBeFocused();
  await expect(page).toHaveURL(new RegExp(`/doc/${encodeURIComponent(path)}$`));
  const previewUrl = page.url();
  const previewHistory = await page.evaluate(() => history.state);
  await preview.getByRole("button", { name: "Table of contents", exact: true }).click();
  await page.getByRole("dialog", { name: "On this page", exact: true }).getByRole("link", { name: "Authored heading", exact: true }).click();
  await expect(page.locator('[data-slot="document-context-panel"]')).toHaveCount(0);
  await expect(preview).toBeVisible();
  await expect(preview.getByRole("heading", { name: "Authored heading", exact: true })).toBeFocused();
  await expect(page).toHaveURL(previewUrl);
  expect(await page.evaluate(() => history.state)).toEqual(previewHistory);
});

test("foreground access verification preserves the live editor draft", async ({ page }) => {
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}?view=edit`);
  const editor = page.getByRole("textbox", { name: "Document body (markdown)" });
  await editor.fill("A local draft that must survive verification");
  await expect(page.getByText("Draft saved locally", { exact: true })).toBeVisible();
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/documents/fixture/**", async route => {
    await pending;
    await route.fallback();
  });
  const read = page.waitForRequest(request => new URL(request.url()).pathname.includes("/documents/fixture/"));
  await page.evaluate(() => window.dispatchEvent(new Event("akb:revalidate-access")));
  await read;
  await expect(editor).toHaveCount(0);
  release();
  await expect(editor).toContainText("A local draft that must survive verification");
  await editor.press("End");
  await editor.pressSequentially(" and continues safely");
  await expect(editor).toContainText("A local draft that must survive verification and continues safely");
  await page.route("**/vaults/fixture/info", route => route.fulfill({ json: { name: "fixture", role: "reader", is_archived: false, is_external_git: false } }));
  await page.evaluate(() => window.dispatchEvent(new Event("akb:revalidate-access")));
  await expect(page.getByText("Read-only · Draft preserved")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save changes", exact: true })).toBeDisabled();
  await expect(editor).toContainText("A local draft that must survive verification and continues safely");
});

test("short prose has no artificial body height", async ({ page }) => {
  await fixture(page, false, false, "One line of knowledge.");
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const body = page.locator(".document-reading-flow");
  await expect(body).toHaveText("One line of knowledge.");
  expect((await body.boundingBox())!.height).toBeLessThan(100);
});

test("wide tables stay locally scrollable at 200 percent CSS zoom", async ({ page }) => {
  const columns = Array.from({ length: 12 }, (_, index) => `Column${index}`);
  const table = `| ${columns.join(" | ")} |\n| ${columns.map(() => "---").join(" | ")} |\n| ${columns.map(() => "Unbroken_identifier_1234567890").join(" | ")} |`;
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page, false, true, table);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const region = page.getByRole("region", { name: "Scrollable table" });
  await expect(region).toBeVisible();
  await page.evaluate(() => { document.documentElement.style.zoom = "2"; });
  expect(await region.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(page.getByRole("navigation", { name: "Resource location", exact: true })).toBeInViewport();
  await expect(page.getByRole("button", { name: /^(Open vault navigation|Expand collections)$/ })).toBeInViewport();
  await region.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => region.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
});

for (const width of [375, 1440]) for (const kind of ["file", "table"] as const) {
  test(`${kind} shares the reading shell at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await fixture(page);
    await page.route("**/reading-file.txt", route => route.fulfill({ contentType: "text/plain", body: "A readable attachment." }));
    await page.route("**/api/v1/files/fixture/f-reading", route => route.fulfill({ json: {
      uri: "akb://fixture/coll/guides/team/operations/file/f-reading",
      name: "Reading notes.txt", collection: "guides/team/operations", mime_type: "text/plain", size_bytes: 22,
    } }));
    await page.route("**/api/v1/files/fixture/f-reading/download", route => route.fulfill({ json: {
      name: "Reading notes.txt", mime_type: "text/plain", download_url: "/reading-file.txt", size_bytes: 22,
    } }));
    await page.route("**/api/v1/tables/fixture", route => route.fulfill({ json: { items: [{
      name: "reading_catalog", collection: "guides/team/operations", description: "Reading inventory", row_count: 1,
      columns: [{ name: "title", type: "text", required: true }],
    }] } }));
    await page.route("**/api/v1/tables/fixture/reading_catalog/rows?*", route => route.fulfill({ json: {
      kind: "table_query", columns: ["id", "title"], total: 1,
      items: [{ id: "20e9d5ca-990c-4edc-9746-002c181abe12", title: "Operating guide" }],
    } }));
    await page.goto(`/vault/fixture/${kind}/${kind === "file" ? "f-reading" : "reading_catalog"}`);
    const commands = page.locator('[data-slot="resource-command-row"]');
    await expect(commands).toHaveCount(1);
    const trail = page.getByRole("navigation", { name: "Resource location", exact: true });
    await expect(trail).toContainText(kind === "file" ? "Reading notes.txt" : "reading_catalog");
    await expectQuietResourceTrail(trail, kind);
    await expect(commands.getByRole("navigation", { name: "Resource location", exact: true })).toHaveCount(0);
    if (width >= 1024) {
      await expect(page.locator(".app-header").getByRole("navigation", { name: "Resource location", exact: true })).toBeVisible();
      await expect(page.locator('[data-slot="vault-collections-sidebar"]').getByRole("tree", { name: "fixture explorer", exact: true })).toBeVisible();
    } else {
      await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toBeInViewport();
    }
    await expect(page.getByRole("button", { name: /^Explore vault:/ })).toHaveCount(0);
    const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
    await expectVaultDestinationsReachable(page);
    await expect(navigation.locator('[aria-current="page"]')).toHaveCount(0);
    if (kind === "file") {
      await expect(page.getByRole("link", { name: "Download file" })).toBeInViewport();
      await expect(page.getByText("A readable attachment.")).toBeVisible();
      await page.getByRole("button", { name: "Info", exact: true }).click();
      await expect(page.getByRole("dialog", { name: "File info" })).toBeVisible();
    } else {
      await expect(page.getByText("Operating guide", { exact: true })).toBeVisible();
      await expect(page.getByRole("button", { name: "Add row", exact: true })).toBeInViewport();
      await page.getByRole("button", { name: "Schema", exact: true }).click();
      await expect(page.getByRole("complementary", { name: "Table schema" })).toBeVisible();
    }
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: kind === "file" ? "Info" : "Schema", exact: true })).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`${kind}.png`) });
    await openVaultDestination(page, "Settings");
    await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
    await expect(navigation.getByRole("link", { name: "Settings", exact: true })).toHaveAttribute("aria-current", "page");
    await page.goBack();
    await expect(trail).toContainText(kind === "file" ? "Reading notes.txt" : "reading_catalog");
    await expect(navigation).toBeVisible();
    await expect(navigation.locator('[aria-current="page"]')).toHaveCount(0);
  });
}
