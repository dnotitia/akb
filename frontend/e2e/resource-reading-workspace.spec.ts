import { expect, test, type Locator, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");

// Browser-owned HTTP fixtures: no mutations or requests reach a user's Vault.
const title = "문서 읽기 작업공간 — Architecture and operating decisions for a shared knowledge workspace";
const path = "guides/team/operations/reading.md";
const lastEdited = "2026-09-14T09:42:30Z";
const createdAt = "2026-08-25T01:05:00Z";

for (const width of [1024, 1280, 1600]) for (const dark of [false, true]) {
  test(`Document byline insets and sidebar hairlines align at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, dark, false, body, undefined, { created_by_name: "김민정" });
    await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
    const tools = page.locator('[data-slot="resource-command-row"]');
    await expect(tools.getByRole("group", { name: "Document metadata", exact: true })).toContainText("김민정");
    const opener = page.getByRole("button", { name: "Open vault navigation", exact: true });
    if (await opener.isVisible()) await opener.click();
    const sidebar = page.locator("#vault-workspace-navigation");
    const management = sidebar.locator('[data-slot="collection-management-row"]');
    await expect(management).toBeVisible();
    const row = (await tools.boundingBox())!;
    const rail = (await management.boundingBox())!;
    expect(Math.abs(row.y - rail.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(row.y + row.height - rail.y - rail.height)).toBeLessThanOrEqual(1);
    expect(row.height).toBe(48);
    const metadata = (await tools.getByRole("group", { name: "Document metadata", exact: true }).boundingBox())!;
    expect(metadata.x - row.x).toBe(20);
    expect(metadata.y - row.y).toBeGreaterThanOrEqual(6);
    const identity = (await sidebar.locator('[data-slot="vault-identity-header"]').boundingBox())!;
    const header = (await page.locator(".app-header").boundingBox())!;
    expect(Math.abs(identity.y + identity.height - header.y - header.height)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath("document-sidebar-alignment.png") });
  });
}

test.describe("Document command alignment on touch", () => {
  test.use({ hasTouch: true });
  test("keeps touch targets and adjacent sidebar hairlines aligned", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await fixture(page, false, false, body, undefined, { created_by_name: "김민정" });
    await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
    const tools = page.locator('[data-slot="resource-command-row"]');
    await expect(tools.getByRole("group", { name: "Document metadata", exact: true })).toContainText("김민정");
    const row = (await tools.boundingBox())!;
    const management = (await page.locator('[data-slot="collection-management-row"]').boundingBox())!;
    expect(row.height).toBe(64);
    expect(Math.abs(row.y + row.height - management.y - management.height)).toBeLessThanOrEqual(1);
    const targets = await tools.locator("[data-reader-control]").evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().height));
    expect(targets.every(height => height >= 44)).toBe(true);
  });
});

for (const scenario of [
  { width: 375, height: 812, dark: false, preview: false },
  { width: 1440, height: 900, dark: false, preview: false },
  { width: 2560, height: 1440, dark: true, preview: false },
  { width: 1280, height: 800, dark: false, preview: true },
]) test(`document publishing opens from overflow at ${scenario.width}px ${scenario.preview ? "preview" : "full page"}`, async ({ page }, testInfo) => {
  await page.setViewportSize(scenario);
  await fixture(page, scenario.dark, false, body, "reading-public");
  if (scenario.preview) {
    await page.goto("/search?q=reading&source=document");
    await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
  } else await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const trigger = documentActions(page);
  await expect(trigger).toBeInViewport({ ratio: 1 });
  const tools = page.getByRole("group", { name: "Document reading tools", exact: true });
  await expect(tools.getByRole("button", { name: /^(Publish|Public link)$/ })).toHaveCount(0);
  const triggerBox = (await trigger.boundingBox())!;
  const toolsBox = (await tools.boundingBox())!;
  expect(triggerBox.y).toBeGreaterThanOrEqual(toolsBox.y);
  expect(triggerBox.y + triggerBox.height).toBeLessThanOrEqual(toolsBox.y + toolsBox.height);
  const canvas = page.locator("#document-reading-canvas");
  const before = await canvas.boundingBox();
  await openPublication(page);
  const panel = page.getByRole("dialog", { name: "Public link", exact: true });
  await expect(panel).toBeVisible();
  const panelBox = (await panel.boundingBox())!;
  expect(panelBox.y).toBeGreaterThanOrEqual(triggerBox.y + triggerBox.height);
  expect(await canvas.boundingBox()).toEqual(before);
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await openPublication(page);
  await page.getByRole("navigation", { name: "Resource location", exact: true }).locator('[aria-current="page"]').click();
  await expect(panel).toHaveCount(0);
  expect(await canvas.boundingBox()).toEqual(before);
  await page.screenshot({ path: testInfo.outputPath("publication-placement.png") });
  if (!scenario.preview) {
    await openVaultDestination(page, "Overview");
    await expect(documentActions(page)).toHaveCount(0);
  }
});

const body = [
  "# Authored heading",
  "읽기 흐름을 유지하면서 팀의 지식을 탐색합니다. ".repeat(12),
  "- First item\n- Second item",
  "| Name | Role |\n| --- | --- |\n| Reader | Read documents |",
  "```text\n" + "long_code_".repeat(100) + "\n```",
  "![Small diagram](/reading-fixture.svg)",
].join("\n\n");

async function fixture(page: Page, dark = false, folded = false, content = body, publicSlug?: string, metadata: { summary?: string; status?: string; title?: string; created_by_name?: string | null; created_by?: string | null } = {}) {
  await page.addInitScript(({ dark, folded }) => {
    localStorage.setItem("akb_token", "resource-reading-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    if (folded) {
      localStorage.setItem("akb.treeVisible", "0");
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
  const vault = trail.getByRole("link", { name: "fixture", exact: true });
  await expect(vault).toHaveAttribute("href", "/vault/fixture");
  await expect(trail.locator("a .lucide-box")).toBeVisible();
  await expect(trail.locator("a svg:not(.lucide-box)")).toHaveCount(0);
  const current = trail.locator('[aria-current="page"]');
  const glyph = trail.locator({ document: ".lucide-file-text", file: ".lucide-file", table: ".lucide-table-2" }[kind]);
  await expect(glyph).toBeInViewport({ ratio: 1 });
  const textBounds = (await current.boundingBox())!;
  const glyphBounds = (await glyph.boundingBox())!;
  const trailBounds = (await trail.boundingBox())!;
  if (trailBounds.width < 320) {
    const vaultBounds = (await vault.boundingBox())!;
    expect(vaultBounds.width).toBeGreaterThanOrEqual(36);
    expect(vaultBounds.height).toBeGreaterThanOrEqual(36);
  }
  // Kind belongs to the title, not before it or stranded at the tools edge.
  const gap = glyphBounds.x - textBounds.x - textBounds.width;
  expect(gap).toBeGreaterThanOrEqual(4);
  expect(gap).toBeLessThanOrEqual(8);
  expect(textBounds.width).toBeGreaterThan(30);
  expect(glyphBounds.x + glyphBounds.width).toBeLessThanOrEqual(trailBounds.x + trailBounds.width + 1);
  expect(Math.abs(glyphBounds.y + glyphBounds.height / 2 - textBounds.y - textBounds.height / 2)).toBeLessThanOrEqual(1);
}

async function revealVaultNavigation(page: Page) {
  const trigger = page.getByRole("button", { name: "Open vault navigation", exact: true });
  if (await trigger.isVisible()) await trigger.click();
  await expect(page.locator("#vault-workspace-navigation")).toBeVisible();
  return page.locator('[data-navigation-mode="overlay"]').isVisible();
}

async function expectQuietVaultTrail(page: Page, label: string) {
  const isRoot = label === "Overview";
  const isFocusedPage = ["Graph", "Public links", "Members", "Settings"].includes(label);
  const trail = page.getByRole("navigation", { name: "Current page", exact: true });
  await expect(trail.locator('[aria-current="page"]')).toHaveText(isRoot ? "fixture" : label);
  const vault = trail.getByRole("link", { name: isFocusedPage ? "Back to fixture" : "fixture", exact: true });
  await expect(vault).toHaveAttribute("href", "/vault/fixture");
  await expect(vault.locator(".lucide-box")).toBeVisible();
  if (isFocusedPage) await expect(page.getByRole("link", { name: "Back to vault fixture", exact: true })).toHaveAttribute("href", "/vault/fixture");
  await expect(trail.getByRole("button")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Navigate in fixture", exact: true })).toHaveCount(0);
  await expect(trail.locator("svg")).toHaveCount(1);
  const current = trail.locator('[aria-current="page"]');
  await expect(current).toBeInViewport({ ratio: 1 });
  const textBounds = (await current.boundingBox())!;
  const vaultBounds = (await vault.boundingBox())!;
  if (isRoot) {
    await expect(vault).toHaveAttribute("aria-current", "page");
    await expect(trail.getByText("/", { exact: true })).toHaveCount(0);
    return;
  }
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
    await expect(navigation).toHaveCount(0);
    await expect(page.getByRole("banner").getByRole("button", { name: "Search knowledge", exact: true })).toHaveCount(1);
    await expect(vaultBreadcrumb(page)).toBeInViewport({ ratio: 1 });
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
    await expect(page.getByRole("menuitemcheckbox", { name: /fixture/ })).toHaveAttribute("aria-checked", "true");
    await page.keyboard.press("Escape");
    await expect(panel).toBeVisible();
    await expect(scope).toBeFocused();
    await scope.click();
    const expandedRequest = page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/search") && !new URL(request.url()).searchParams.has("vault"));
    await page.getByRole("menuitem", { name: "All vaults", exact: true }).click();
    await expandedRequest;
    const allInput = panel.getByRole("combobox", { name: "Search all accessible vaults", exact: true });
    await expect(allInput).toHaveValue("reading");
    await expect(panel.getByRole("option").first()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("all-vault-search-results.png") });
    await panel.getByRole("button", { name: "Search scope: All vaults", exact: true }).click();
    const narrowedRequest = page.waitForRequest(request => {
      const url = new URL(request.url());
      return url.pathname.endsWith("/search") && url.searchParams.getAll("vault").join(",") === "fixture";
    });
    const fixtureOption = page.getByRole("menuitemcheckbox", { name: /fixture/ });
    await fixtureOption.click();
    await narrowedRequest;
    await expect(fixtureOption).toHaveAttribute("aria-checked", "true");
    const menu = page.getByRole("menu", { name: /^Search scope:/ });
    await expect(menu).toBeVisible();
    await menu.getByRole("menuitem", { name: "Done", exact: true }).click();
    await expect(menu).toHaveCount(0);
    await expect(scope).toBeFocused();
    await expect(input).toHaveValue("reading");
    await expect(panel.getByRole("option").first()).toBeVisible();
    await input.press("Enter");
    const preview = page.getByTestId("document-preview-dialog");
    await expect(preview).toBeVisible();
    await expect(preview.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(preview).toHaveCount(0);
    await expect(page).toHaveURL(/\/vault\/fixture\/members$/);
    await expect(panel).toBeVisible();
    await expect(input).toHaveValue("reading");
    await expect(input).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
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
]) test(`quick search adds an accessible Vault without dropping selected scopes at ${scenario.width}x${scenario.height} ${scenario.dark ? "dark" : "light"} from ${scenario.route}`, async ({ page }, testInfo) => {
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
  // Radix labels the menu through its trigger, including the current selection.
  const menu = page.getByRole("menu", { name: /^Search scope:/ });
  const filter = menu.getByRole("searchbox", { name: "Filter vaults" });
  await expect(filter).toBeFocused();
  await expect(menu).toBeInViewport({ ratio: 1 });
  await page.screenshot({ path: testInfo.outputPath("vault-scope-directory.png") });
  await filter.fill("missing-vault");
  await expect(menu.getByText("No vaults match this filter.")).toBeVisible();
  await filter.fill("beta");
  const betaOption = menu.getByRole("menuitemcheckbox", { name: "팀-beta", exact: true });
  await expect(betaOption).toBeVisible();
  await expect(betaOption).toHaveAttribute("aria-checked", "false");
  await filter.press("ArrowUp");
  await expect(betaOption).toBeFocused();
  const selectedVaults = scenario.route.startsWith("/vault/") ? ["fixture", "팀-beta"] : ["팀-beta"];
  const changed = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname.endsWith("/search") && url.searchParams.getAll("vault").join(",") === selectedVaults.join(",") && url.searchParams.get("source_type") === "document";
  });
  await page.keyboard.press("Enter");
  const requestUrl = new URL((await changed).url());
  expect(requestUrl.searchParams.get("q")).toBe("reading");
  await expect(menu).toBeVisible();
  await expect(betaOption).toHaveAttribute("aria-checked", "true");
  await expect(page).toHaveURL(scenario.route);
  await menu.getByRole("menuitem", { name: "Done", exact: true }).click();
  await expect(menu).toHaveCount(0);
  // The modal dropdown hides the underlying combobox from the accessibility
  // tree until Done/Escape restores the search dialog's focus scope.
  await expect(query).toHaveValue("reading");
  await expect(query).toHaveAccessibleName(`Search in ${selectedVaults.join(", ")}`);
  const selectedScope = panel.getByRole("button", { name: `Search scope: ${selectedVaults.join(", ")}`, exact: true });
  await expect(selectedScope).toBeFocused();
  await selectedScope.click();
  await expect(betaOption).toHaveAttribute("aria-checked", "true");
  if (selectedVaults.length === 2) await expect(menu.getByRole("menuitemcheckbox", { name: /fixture/ })).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(panel).toBeVisible();
  await expect(selectedScope).toBeFocused();
  await panel.getByRole("button", { name: "Continue in search page", exact: true }).click();
  await expect(page).toHaveURL(url =>
    decodeURIComponent(url.pathname) === (selectedVaults.length === 2 ? "/search" : "/vault/팀-beta/search") &&
    url.searchParams.get("q") === "reading" && url.searchParams.get("source") === "document" &&
    url.searchParams.get("v") === (selectedVaults.length === 2 ? "fixture,팀-beta" : null),
  );
  const scopeSummary = selectedVaults.length > 1 ? `${selectedVaults.length} vaults` : selectedVaults[0];
  const pageScope = page.getByTestId("search-query-entry").getByRole("button", { name: `Search scope: ${scopeSummary}`, exact: true });
  await expect(pageScope).toBeVisible();
  await pageScope.click();
  await expect(page.getByRole("menuitemcheckbox", { checked: true })).toHaveCount(selectedVaults.length);
  for (const vault of selectedVaults) {
    await expect(page.getByRole("menuitemcheckbox", { name: new RegExp(`^${vault}(?:\\s*Current vault)?$`) })).toHaveAttribute("aria-checked", "true");
  }
  await page.keyboard.press("Escape");
  await expect(pageScope).toBeFocused();
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
    // Header identity and the navigation rail should form one paper surface,
    // including while no Vault is selected.
    await expect(header).toHaveCSS("background-color", dark ? "rgb(18, 24, 33)" : "rgb(255, 255, 255)");
  }
});

for (const dark of [false, true]) for (const width of [375, 1440]) test(`Settings header uses the paper surface at ${width}px (${dark ? "dark" : "light"})`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 900 });
  await fixture(page, dark);
  for (const path of ["/settings", "/settings?tab=tokens", "/settings/?tab=preferences"]) {
    await page.goto(path);
    const header = page.locator(".app-header");
    await expect(header).toHaveCSS("background-color", dark ? "rgb(18, 24, 33)" : "rgb(255, 255, 255)");
    await expect(header).toHaveCSS("border-bottom-width", "1px");
    await expect(header).toHaveCSS("height", "56px");
    const workspace = page.getByTestId("account-settings-dialog");
    await expect(workspace).toHaveCSS("background-color", await header.evaluate(el => getComputedStyle(el).backgroundColor));
  }
  await page.screenshot({ path: testInfo.outputPath("settings-paper-header.png") });
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
  await expect(page.getByRole("menuitemcheckbox", { name: /Current vault/ })).toBeInViewport({ ratio: 1 });
  await expect(page.getByRole("menuitem", { name: "All vaults", exact: true })).toBeInViewport({ ratio: 1 });
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
    await page.getByRole("menuitem", { name: "All vaults", exact: true }).click();
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
    await page.getByRole("menuitem", { name: "All vaults", exact: true }).click();
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
    // Closing a quick-search preview resumes the same search session. The
    // background editor remains inert until that search dialog is dismissed.
    await expect(panel).toBeVisible();
    await expect(panel.getByRole("combobox")).toHaveValue("reading");
    await expect(panel.getByRole("button", { name: `Search scope: ${scope === "all" ? "All vaults" : "fixture"}`, exact: true })).toBeVisible();
    await panel.getByRole("button", { name: "Close search", exact: true }).click();
    await expect(panel).toHaveCount(0);
    await expect(editor).toContainText(draft);
  } else {
    await expect(page).toHaveURL(url => url.pathname === (scope === "all" ? "/search" : "/vault/fixture/search") && url.searchParams.get("q") === "reading");
  }
});

function vaultBreadcrumb(page: Page) {
  return page.getByRole("navigation", { name: /^(Current page|Resource location)$/ })
    .getByRole("link", { name: /^(Back to )?fixture$/ });
}

async function openVaultDestination(page: Page, name: string) {
  if (new URL(page.url()).pathname.replace(/\/$/, "") !== "/vault/fixture") {
    await vaultBreadcrumb(page).click();
    await expect(page).toHaveURL(url => url.pathname === "/vault/fixture");
  }
  if (name !== "Overview") await page.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link", { name, exact: true }).click();
}

async function expectVaultDestinationsReachable(page: Page, current?: string) {
  await expect(vaultBreadcrumb(page)).toHaveAttribute("href", "/vault/fixture");
  await expect(vaultBreadcrumb(page)).toBeInViewport({ ratio: 1 });
  await expect(page.getByRole("button", { name: "Navigate in fixture", exact: true })).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
  const navigation = page.getByRole("navigation", { name: "Vault pages", exact: true });
  if (current === "Overview") {
    await expect(navigation.getByRole("link")).toHaveCount(4);
    for (const [name, href] of vaultDestinations.filter(([name]) => name !== "Overview")) {
      await expect(navigation.getByRole("link", { name, exact: true })).toHaveAttribute("href", href);
    }
  } else await expect(navigation).toHaveCount(0);
}

function documentActions(page: Page) {
  return page.getByRole("group", { name: "Document reading tools", exact: true })
    .getByRole("button", { name: `Actions for ${title}`, exact: true });
}

async function openPublication(page: Page, name: "Publish" | "Public link" = "Public link") {
  await documentActions(page).click();
  await page.getByRole("menuitem", { name, exact: true }).click();
  await expect(page.getByRole("dialog", { name: name === "Publish" ? "Publish document" : "Public link", exact: true })).toBeVisible();
}

async function openDocumentDetails(page: Page, section?: "Properties" | "Version history" | "Related documents") {
  const panel = page.locator('[data-slot="document-context-panel"]');
  if (!await panel.count()) await page.getByRole("button", { name: "Document details", exact: true }).click();
  await expect(panel).toBeVisible();
  if (section) {
    const disclosure = panel.locator(`[data-context-section="${({ Properties: "info", "Version history": "history", "Related documents": "relations" })[section]}"]`);
    if (await disclosure.getAttribute("open") === null) await disclosure.locator("summary").click();
    await expect(disclosure).toHaveAttribute("open", "");
  }
  return panel;
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
    const account = header.getByRole("button", {
      name: "Account menu — Reading reviewer (@fixture), reader@example.invalid",
      exact: true,
    });
    for (const control of [search, notifications, account]) {
      await expect(control).toBeInViewport({ ratio: 1 });
      expect((await control.boundingBox())!.height).toBe(36);
    }
    const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
    await expect(navigation).toHaveCount(0);
    if (width >= 1440) {
      await expect(search).toHaveText("Search knowledge…");
      await expect(account.getByText("Reading reviewer", { exact: true })).toBeVisible();
      const headerColor = await header.evaluate(el => getComputedStyle(el).backgroundColor);
      expect(await page.getByRole("group", { name: "Document reading tools", exact: true }).evaluate(el => getComputedStyle(el).backgroundColor)).toBe(headerColor);
      expect(await page.locator('[aria-label="Document workspace"]').evaluate(el => getComputedStyle(el).backgroundColor)).toBe(headerColor);
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

test("document returns to the Overview before exposing Vault destinations", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  await expectVaultDestinationsReachable(page);
  await vaultBreadcrumb(page).click();
  await expectQuietVaultTrail(page, "Overview");
  await expectVaultDestinationsReachable(page, "Overview");
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  await expectVaultDestinationsReachable(page);
});

for (const width of [320, 1440]) test(`long summaries preserve draft state and overflow publishing at ${width}px`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const summary = "An extended description of this document. ".repeat(40);
  await fixture(page, false, false, "test", "existing-link", { summary, status: "draft" });
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const context = page.locator('[data-slot="resource-command-row"]');
  await expect(context.getByText("Draft", { exact: true })).toBeVisible();
  await expect(documentActions(page)).toBeInViewport({ ratio: 1 });
  const summaryRegion = page.getByRole("region", { name: "Document summary", exact: true });
  await expect(summaryRegion).toHaveText(summary.trim());
  await expect(summaryRegion).toBeVisible();
  await expect(page.getByRole("button", { name: "Read document summary" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /summary scrolling/ })).toBeHidden();
  const time = context.locator("time");
  await expect(time).toBeVisible();
  if (width === 1440) expect(await time.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  await openPublication(page);
  await expect(page.getByRole("dialog", { name: "Public link", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(documentActions(page)).toBeFocused();
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
    await testInfo.attach("resource-location-geometry", {
      body: JSON.stringify(await location.evaluate(element => ({
        trailWidth: element.getBoundingClientRect().width,
        titleWidth: element.querySelector('[aria-current="page"]')!.getBoundingClientRect().width,
        vaultLinkWidth: element.querySelector("a")!.getBoundingClientRect().width,
      }))),
      contentType: "application/json",
    });
    await expect(page.locator("h1#doc-title")).toHaveCount(1);
    await expect(page.locator('[data-slot="resource-command-row"]')).toHaveCount(1);
    const readingTools = page.getByRole("group", { name: "Document reading tools", exact: true });
    await expect(readingTools.getByRole("radio", { name: "Preview", exact: true })).toBeVisible();
    await expect(readingTools.getByRole("button", { name: "Copy markdown", exact: true })).toBeVisible();
    const contextBounds = (await page.locator('[data-slot="resource-command-row"]').boundingBox())!;
    const toolBounds = (await readingTools.boundingBox())!;
    expect(toolBounds.y).toBe(contextBounds.y);
    const workspaceBounds = (await page.getByRole("region", { name: "Document workspace" }).boundingBox())!;
    const frameBounds = (await page.locator('[data-slot="document-viewer-frame"]').boundingBox())!;
    expect(frameBounds.x).toBe(workspaceBounds.x);
    expect(frameBounds.width).toBe(workspaceBounds.width);
    expect(frameBounds.y).toBe(workspaceBounds.y);
    expect(frameBounds.height).toBe(workspaceBounds.height);
    const surface = await page.locator("#document-reading-canvas").evaluate(el => getComputedStyle(el).backgroundColor);
    await expect(page.locator(".app-header")).toHaveCSS("background-color", surface);
    await expect(readingTools).toHaveCSS("background-color", surface);
    await expect(readingTools.getByRole("button", { name: "Publish", exact: true })).toHaveCount(0);
    if (toolBounds.width >= 656) {
      expect(toolBounds.height).toBeLessThanOrEqual(49);
      const timeBounds = (await readingTools.locator("time").boundingBox())!;
      const tabBounds = (await readingTools.getByRole("radiogroup", { name: "Document view mode", exact: true }).boundingBox())!;
      expect(Math.abs(timeBounds.y + timeBounds.height / 2 - tabBounds.y - tabBounds.height / 2)).toBeLessThanOrEqual(1);
    }
    const modeBounds = (await readingTools.getByRole("radiogroup", { name: "Document view mode", exact: true }).boundingBox())!;
    const actionBounds = (await readingTools.getByRole("button", { name: "Copy markdown", exact: true }).boundingBox())!;
    expect(modeBounds.x + modeBounds.width).toBeLessThan(actionBounds.x);
    const commandMetrics = await page.locator('[data-slot="resource-command-row"] [data-reader-control]').evaluateAll(elements => elements.map(element => ({
      height: element.getBoundingClientRect().height,
      font: getComputedStyle(element).fontSize,
    })));
    expect(new Set(commandMetrics.map(item => item.height)).size).toBe(1);
    expect(commandMetrics.every(item => item.font === "14px")).toBe(true);
    const commandWidth = (await page.locator('[data-slot="resource-command-row"]').boundingBox())!.width;
    expect(commandMetrics.every(item => item.height === (commandWidth >= 656 ? 28 : 44))).toBe(true);
    const editTime = page.locator('[data-slot="resource-command-row"] time');
    await expect(editTime).toHaveAttribute("datetime", new Date(lastEdited).toISOString());
    const exactEditTime = await page.evaluate(value => new Date(value).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric", hour: "numeric",
      minute: "2-digit", second: "2-digit", timeZoneName: "short",
    }), lastEdited);
    await expect(editTime.locator(".sr-only")).toHaveText(`Last edited: ${exactEditTime}`);
    if (commandWidth >= 512) await expect(editTime).toBeVisible();
    await expect(readingTools.getByRole("button", { name: "Document details", exact: true })).toBeVisible();
    await expect(documentActions(page)).toBeVisible();
    await expect(page.getByRole("button", { name: "Open document info", exact: true })).toHaveCount(0);
    const collections = page.locator('[data-slot="vault-collections-sidebar"]');
    await expect(page.getByRole("button", { name: /^Explore vault:/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "More vault pages", exact: true })).toHaveCount(0);
    const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
    await expectVaultDestinationsReachable(page);
    await expect(navigation).toHaveCount(0);
    await expect(vaultBreadcrumb(page)).toBeInViewport({ ratio: 1 });
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
    }
    const drawer = await revealVaultNavigation(page);
    const contextPanel = page.locator("#vault-workspace-navigation");
    await expect(contextPanel).toHaveCount(1);
    await expect(collections.getByRole("navigation", { name: "fixture explorer", exact: true })).toBeVisible();
    await expect(collections.getByRole("treeitem")).toHaveCount(0);
    await expect(contextPanel.locator('[data-slot="vault-identity-header"]').getByRole("button", { name: "Switch vault: fixture", exact: true })).toBeVisible();
    await expect(contextPanel.getByRole("button", { name: "Add content", exact: true })).toHaveCount(1);
    await expect(page.getByRole("button", { name: "Close vault navigation", exact: true })).toBeInViewport();
    if (width >= 1024) {
      const resize = page.getByRole("separator", { name: "Resize vault navigation", exact: true });
      await expect(resize).toHaveAttribute("aria-valuenow", "256");
      await expect(resize).toHaveAttribute("aria-valuemin", "224");
      await expect(resize).toHaveAttribute("aria-valuemax", "360");
    }
    await expect(collections.getByRole("link", { name: /^(Search|Graph|Public links|Members|Settings)$/ })).toHaveCount(0);
    if (drawer) {
      // Leave the identity tooltip before testing the outer disclosure's
      // Escape behavior; nested layers consume Escape independently.
      await collections.getByRole("navigation", { name: "fixture explorer", exact: true }).getByRole("button").first().focus();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toBeFocused();
    }
    if (width === 2560) await page.getByRole("button", { name: "Close vault navigation", exact: true }).click();
    for (const name of ["Edit", "Copy markdown", `Actions for ${title}`]) {
      await expect(page.getByRole("region", { name: "Document workspace" }).getByRole("button", { name, exact: true })).toBeInViewport();
    }
    await expect(readingTools.getByRole("button", { name: "Publish", exact: true })).toHaveCount(0);
    await expect(page.getByRole("radio", { name: "Raw markdown", exact: true })).toBeInViewport();
    const flow = page.locator(".document-reading-flow .ProseMirror");
    const measures = await flow.evaluate(root => {
      const box = (selector: string) => {
        const el = root.querySelector(selector)!;
        const rect = el.getBoundingClientRect();
        return { x: rect.x, width: rect.width, top: rect.top, margin: getComputedStyle(el).marginTop };
      };
      return { heading: box("h2"), paragraph: box("p"), table: box(".akb-md-table"), list: box("ul"), code: box("pre") };
    });
    for (const box of [measures.paragraph, measures.table, measures.list, measures.code]) {
      expect(box.x).toBeCloseTo(measures.heading.x, 0);
      expect(box.width).toBeCloseTo(measures.heading.width, 0);
    }
    expect(measures.heading.margin).toBe("0px");
    expect(measures.paragraph.width).toBeLessThanOrEqual(832);
    const innerTable = await flow.locator("table").boundingBox();
    // The wrapper owns a 1px border on either side.
    expect(Math.abs(innerTable!.x - measures.table.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(innerTable!.width - measures.table.width)).toBeLessThanOrEqual(2);
    const code = page.getByRole("region", { name: /^Scrollable(?: [a-z0-9-]+)? code block$/i });
    expect(await code.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
    const image = await flow.getByRole("img", { name: "Small diagram" }).boundingBox();
    expect(image!.width).toBe(80);
    expect(image!.height).toBe(40);
    if (width === 375) {
      const commands = await page.locator('[data-slot="resource-command-row"]').boundingBox();
      // Metadata now belongs to this same toolbar; allow its compact first
      // line above the 44px touch controls and the 1px bottom divider,
      // but no redundant command band.
      expect(commands!.height).toBeLessThanOrEqual(91);
    }
    if (width >= 1440) expect(measures.heading.top).toBeLessThanOrEqual(220);
    await expect(page.getByRole("region", { name: "Scrollable table" })).toHaveAttribute("tabindex", "0");
    await expect(code).toHaveAttribute("tabindex", "0");
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
    const actions = documentActions(page);
    await actions.click();
    for (const name of ["Copy document link", "Copy markdown", "Publish", "Version history"]) {
      await expect(page.getByRole("menuitem", { name, exact: true })).toBeVisible();
    }
    await expect(page.getByRole("menuitemradio", { name: /^(Standard|Wide)$/ })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(actions).toBeFocused();
    await expect(page.locator('[data-slot="document-context-rail"]')).toHaveCount(0);
    const detailsTrigger = readingTools.getByRole("button", { name: "Document details", exact: true });
    await expect(detailsTrigger).toHaveAttribute("aria-expanded", "false");
    await expect(detailsTrigger.locator("svg")).toHaveAttribute("aria-hidden", "true");
    const canvas = page.locator("#document-reading-canvas");
    const beforeDetails = (await canvas.boundingBox())!;
    const docked = await canvas.evaluate(element => {
      const bounds = element.parentElement!.getBoundingClientRect();
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
      return bounds.width >= 48 * rem && bounds.height >= 20 * rem;
    });
    if (width === 375) expect(docked).toBe(false);
    if (width === 2560) expect(docked).toBe(true);
    const panel = await openDocumentDetails(page);
    await expect(panel).toHaveAttribute("data-mode", docked ? "docked" : "overlay");
    await expect(page.getByRole(docked ? "complementary" : "dialog", { name: "Document details", exact: true })).toBeVisible();
    await expect(panel.getByRole("heading", { name: "Document details", exact: true })).toBeVisible();
    const close = panel.getByRole("button", { name: "Close document panel", exact: true });
    await expect(close).toBeFocused();
    await expect(panel.getByRole("navigation", { name: "Document outline", exact: true })).toBeVisible();
    const afterDetails = (await canvas.boundingBox())!;
    if (docked) {
      const panelBox = (await panel.boundingBox())!;
      expect(panelBox.width).toBe(304);
      expect(panelBox.x).toBeCloseTo(afterDetails.x + afterDetails.width, 0);
      expect(afterDetails.width + panelBox.width).toBeCloseTo(beforeDetails.width, 0);
      expect(afterDetails.x).toBe(beforeDetails.x);
      expect(afterDetails.height).toBe(beforeDetails.height);
    } else {
      expect(afterDetails).toEqual(beforeDetails);
      await expect(panel).toHaveAttribute("aria-modal", "true");
      await page.keyboard.press("Shift+Tab");
      expect(await panel.evaluate(element => element.contains(document.activeElement))).toBe(true);
    }
    const headingLink = panel.getByRole("link", { name: "Authored heading", exact: true });
    const readingUrl = page.url();
    await headingLink.click();
    await expect(panel).toBeVisible();
    await expect(headingLink).toBeFocused();
    await headingLink.press("Enter");
    await expect(headingLink).toBeFocused();
    await expect(page).toHaveURL(readingUrl);
    await openDocumentDetails(page, "Properties");
    await expect(panel.getByText(title, { exact: true })).toBeVisible();
    await expect(panel.locator(`time[datetime="${new Date(lastEdited).toISOString()}"]`)).toBeVisible();
    await expect(panel.locator(`time[datetime="${new Date(createdAt).toISOString()}"]`)).toBeVisible();
    await openDocumentDetails(page, "Related documents");
    await expect(panel.getByText("No relations yet.", { exact: true })).toBeVisible();
    await expect(panel.locator('[data-context-section="info"]')).not.toHaveAttribute("open");
    await openDocumentDetails(page, "Version history");
    await expect(panel.locator('[data-context-section="history"]')).toHaveAttribute("open", "");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("document-details.png") });
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
    await expect(detailsTrigger).toBeFocused();
    expect(await canvas.boundingBox()).toEqual(beforeDetails);
    await detailsTrigger.click();
    await close.click();
    await expect(detailsTrigger).toBeFocused();
    // Overflow history enters the same pane and focuses its native disclosure.
    await actions.click();
    await page.getByRole("menuitem", { name: "Version history", exact: true }).click();
    await expect(panel.locator('[data-context-section="history"] > summary')).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(detailsTrigger).toBeFocused();
  });
}

test("document details remain reachable in a short landscape reader", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 280 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const trigger = page.getByRole("button", { name: "Document details", exact: true });
  await expect(trigger).toBeInViewport();
  await trigger.focus();
  await trigger.press("Enter");
  const panel = page.getByRole("dialog", { name: "Document details", exact: true });
  await expect(panel.getByRole("button", { name: "Close document panel" })).toBeInViewport();
  const history = panel.locator('[data-context-section="history"] > summary');
  await history.focus();
  await expect(history).toBeInViewport();
  await history.press("Enter");
  await expect(panel.locator('[data-context-section="history"]')).toHaveAttribute("open", "");
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
});

test("a deep document reader reaches every Vault page through Overview and Back restores its context", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  for (const [name, href] of vaultDestinations) {
    await openVaultDestination(page, name);
    await expect(page).toHaveURL(url => url.pathname === href);
    await expectQuietVaultTrail(page, name);
    await expectVaultDestinationsReachable(page, name);
    await page.goBack();
    if (name !== "Overview") await page.goBack();
    await expect(page).toHaveURL(new RegExp(`/doc/${encodeURIComponent(path)}$`));
    await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
    await expectVaultDestinationsReachable(page);
    await expect(page.locator(".app-header").getByRole("navigation", { name: "Resource location", exact: true })).toContainText(title);
  }
  await vaultBreadcrumb(page).focus();
  await page.keyboard.press("Enter");
  const settings = page.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link", { name: "Settings", exact: true });
  await settings.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
  await expect(page.locator("#main")).toBeFocused();
  await expectQuietVaultTrail(page, "Settings");
  await page.screenshot({ path: testInfo.outputPath("vault-settings-navigation.png") });
  await page.goBack();
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  await expect(page.locator('[data-slot="resource-command-row"]').getByRole("navigation", { name: "Resource location", exact: true })).toHaveCount(0);
  // Selecting the current Vault still leaves a resource for its Overview.
  const pickerTrigger = page.getByRole("button", { name: "Switch vault: fixture", exact: true });
  await pickerTrigger.click();
  const picker = page.getByRole("dialog", { name: "Vaults", exact: true });
  const vaultSearch = picker.getByRole("searchbox", { name: "Find a vault", exact: true });
  await expect(vaultSearch).toBeFocused();
  await vaultSearch.fill("missing");
  await expect(picker.getByText("No vaults match this name.", { exact: true })).toBeVisible();
  // Filtering the anchored chooser keeps the surrounding identity visible.
  await expect(pickerTrigger).toBeVisible();
  await expect(pickerTrigger).toHaveText("fixture");
  await vaultSearch.fill("fixture");
  const selectedVault = picker.getByRole("link", { name: "fixture, current vault", exact: true });
  await expect(selectedVault).toHaveAttribute("aria-current", "location");
  await selectedVault.click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  await expect(picker).toHaveCount(0);
  await expectQuietVaultTrail(page, "Overview");
  await page.goBack();
  await vaultBreadcrumb(page).click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
});

test("maximum saved navigation width uses a temporary disclosure without changing preferences", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1024, height: 1000 });
  await fixture(page);
  await page.addInitScript(() => {
    localStorage.setItem("akb.focusedNavigationWidth.v1", "360");
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
  await expect(collections.getByRole("navigation", { name: "fixture explorer", exact: true })).toBeVisible();
  const resize = page.getByRole("separator", { name: "Resize vault navigation", exact: true });
  await expect(resize).toHaveCount(1);
  await expect(resize).toHaveAttribute("aria-valuenow", "360");
  await expect(resize).toHaveAttribute("aria-valuemin", "224");
  await expect(resize).toHaveAttribute("aria-valuemax", "360");
  await expect(collections.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
  await collections.getByRole("navigation", { name: "fixture explorer", exact: true }).getByRole("button").first().focus();
  await page.keyboard.press("Escape");
  await expect(open).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("maximum-rails-compact.png") });
  // A Collection breadcrumb must reveal its destination even when the saved
  // context panel currently requires the temporary disclosure.
  const parentCollection = location.getByRole("link", { name: "Operations", exact: true });
  if (await parentCollection.isVisible()) await parentCollection.click();
  else {
    await location.getByRole("button", { name: "Show collection ancestry", exact: true }).click();
    await page.getByRole("menuitem", { name: "Operations", exact: true }).click();
  }
  await expect(page).toHaveURL(/\/vault\/fixture\?collection=guides%2Fteam%2Foperations$/);
  await expect(page.locator("#vault-workspace-navigation")).toBeInViewport();
  await collections.getByRole("navigation", { name: "fixture explorer", exact: true }).getByRole("button").first().focus();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toBeFocused();
  await page.setViewportSize({ width: 1920, height: 1000 });
  await expect(page.locator('[data-navigation-mode="inline"]')).toBeVisible();
  await expect(page.getByRole("button", { name: "Close vault navigation", exact: true })).toBeInViewport();
  await expect(resize).toHaveAttribute("aria-valuenow", "360");
  expect(await page.evaluate(() => ["akb.focusedNavigationWidth.v1", "akb.treeVisible"].map(key => localStorage.getItem(key)))).toEqual(["360", "1"]);
});

for (const dark of [false, true]) test(`Vault destinations preserve one context panel and its visibility in ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await fixture(page, dark);
  await page.goto("/vault/fixture");
  const initialTreePreference = await page.evaluate(() => localStorage.getItem("akb.treeVisible"));
  const collections = page.locator('[data-slot="vault-collections-sidebar"]');
  const tree = collections.getByRole("navigation", { name: "fixture explorer", exact: true });
  await expect(tree).toBeVisible();
  const breadcrumb = vaultBreadcrumb(page);
  const position = (await breadcrumb.boundingBox())!;
  const header = (await page.locator(".app-header").boundingBox())!;
  expect(header.height).toBe(56);
  const initialViewport = (await page.locator('[data-slot="vault-route-viewport"]').boundingBox())!;
  expect(initialViewport.y).toBe(header.y + header.height);
  for (const [name, href] of vaultDestinations) {
    await openVaultDestination(page, name);
    await expect(page).toHaveURL(url => url.pathname === href);
    await expectQuietVaultTrail(page, name);
    await expectVaultDestinationsReachable(page, name);
    if (name !== "Overview") {
      await expect(tree).toBeVisible();
      await expect(page.locator("#vault-workspace-navigation")).toHaveCount(1);
      expect(await page.evaluate(() => localStorage.getItem("akb.treeVisible"))).toBe(initialTreePreference);
      await page.goBack();
      await expect(tree).toBeVisible();
    }
    await expect(page.locator(".app-header").getByRole("navigation", { name: "Vault pages", exact: true })).toHaveCount(0);
    await expect(collections.getByRole("navigation", { name: "Vault pages", exact: true })).toHaveCount(0);
    expect(await breadcrumb.boundingBox()).toEqual(position);
  }
  const viewport = page.locator('[data-slot="vault-route-viewport"]');
  await viewport.evaluate(element => { element.scrollTop = element.scrollHeight; });
  expect(await breadcrumb.boundingBox()).toEqual(position);
  await page.screenshot({ path: testInfo.outputPath("working-page-navigation.png") });
  await page.getByRole("button", { name: "Close vault navigation", exact: true }).click();
  const expand = page.getByRole("button", { name: "Open vault navigation", exact: true });
  await expect(expand).toBeInViewport();
  await expect(collections).toBeHidden();
  await expectVaultDestinationsReachable(page, "Overview");
  expect(await page.evaluate(() => localStorage.getItem("akb.treeVisible"))).toBe("0");
  await openVaultDestination(page, "Members");
  await expect(collections).toBeHidden();
  expect(await page.evaluate(() => localStorage.getItem("akb.treeVisible"))).toBe("0");
  await page.goBack();
  await expect(expand).toBeInViewport();
  await expect(collections).toBeHidden();
  await expand.click();
  await expect(tree).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("akb.treeVisible"))).toBe("1");
  await page.goto("/vault/fixture/activity");
  await expectQuietVaultTrail(page, "Activity");
  await expectVaultDestinationsReachable(page);
  await expect(tree).toBeVisible();
});

test("Vault breadcrumb remains stable when only working content width changes", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture");
  const trigger = vaultBreadcrumb(page);
  const content = page.locator('[data-slot="vault-route-viewport"]');
  await expect(page.locator('[data-navigation-mode="inline"]')).toBeVisible();
  const original = (await trigger.boundingBox())!;
  await content.evaluate(element => { element.style.maxWidth = "340px"; });
  const compact = (await content.boundingBox())!;
  await expectVaultDestinationsReachable(page, "Overview");
  expect(await trigger.boundingBox()).toEqual(original);
  await page.locator(".app-header").screenshot({ path: testInfo.outputPath("content-width-compact.png") });
  await content.evaluate(element => { element.style.maxWidth = "850px"; });
  const wide = (await content.boundingBox())!;
  expect(wide.width).toBeGreaterThan(compact.width);
  expect(await trigger.boundingBox()).toEqual(original);
  await expectVaultDestinationsReachable(page, "Overview");
  expect(page.viewportSize()).toEqual({ width: 1920, height: 1000 });
  await content.evaluate(element => { element.style.maxWidth = "340px"; });
  expect(await content.boundingBox()).toEqual(compact);
  expect(await trigger.boundingBox()).toEqual(original);
  await expect(page.getByRole("button", { name: "More vault pages", exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const width of [1440, 768]) {
  test(`short-height context navigation remains reachable without an empty outer gutter at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await fixture(page);
    await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
    await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
    await revealVaultNavigation(page);
    const collections = page.locator('[data-slot="vault-collections-sidebar"]');
    const collapse = page.getByRole("button", { name: "Close vault navigation", exact: true });
    const tree = collections.getByRole("navigation", { name: "fixture explorer", exact: true });
    await expect(collapse).toBeInViewport();
    await expect(collections.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
    const emptyGutter = () => collections.evaluate(element => ({
      style: getComputedStyle(element).scrollbarGutter,
      overflow: element.scrollHeight > element.clientHeight,
      reservedWidth: element.getBoundingClientRect().width - element.clientWidth,
    }));
    await expect.poll(emptyGutter).toEqual({ style: "auto", overflow: false, reservedWidth: 0 });

    await collapse.focus();
    // The compact identity and icon controls leave more reading room; use
    // a shorter viewport so this fixture still exercises actual overflow.
    const shortHeight = width >= 1024 ? 180 : 280;
    await page.setViewportSize({ width, height: shortHeight });
    await expect.poll(async () => {
      const box = (await collections.boundingBox())!;
      return box.y + box.height;
    }).toBe(shortHeight);
    // The stable header toggle remains reachable while native link focus
    // reveals a document inside the independently scrolling explorer.
    const treeDocument = tree.getByRole("link", { name: `Document: ${title}`, exact: true });
    await treeDocument.focus();
    await expect(treeDocument).toBeInViewport();
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

test("Overview page links preserve keyboard focus while the mobile workspace resizes", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await fixture(page);
  await page.goto("/vault/fixture");
  const navigation = page.getByRole("navigation", { name: "Vault pages", exact: true });
  const settings = navigation.getByRole("link", { name: "Settings", exact: true });
  await settings.focus();
  for (const width of [768, 375]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(settings).toBeFocused();
    await expect(settings).toBeInViewport({ ratio: 1 });
    await expectVaultDestinationsReachable(page, "Overview");
  }
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
  await expect(page.locator("#main")).toBeFocused();
});

test("Vault discovery adapts to enlarged text and a short landscape viewport", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await fixture(page);
  await page.goto("/vault/fixture");
  await page.evaluate(() => { document.documentElement.style.fontSize = "200%"; });
  await expectVaultDestinationsReachable(page, "Overview");
  const links = page.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link");
  for (const link of await links.all()) {
    await link.focus();
    await expect(link).toBeInViewport({ ratio: 1 });
    expect(await link.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
  await page.setViewportSize({ width: 844, height: 390 });
  await expectVaultDestinationsReachable(page, "Overview");
  for (const link of await links.all()) {
    await link.focus();
    await expect(link).toBeInViewport({ ratio: 1 });
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const width of [375, 768]) {
  test(`Overview links stay separate from the breadcrumb at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await fixture(page);
    await page.goto("/vault/fixture");
    const navigation = page.getByRole("navigation", { name: "Vault pages", exact: true });
    await expectVaultDestinationsReachable(page, "Overview");
    await expectQuietVaultTrail(page, "Overview");
    await expect(page.locator(".app-header").getByRole("navigation", { name: "Vault pages", exact: true })).toHaveCount(0);
    for (const link of await navigation.getByRole("link").all()) {
      const box = (await link.boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(44);
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
    }
    await page.screenshot({ path: testInfo.outputPath("vault-pages-mobile.png") });
    const settings = navigation.getByRole("link", { name: "Settings", exact: true });
    await settings.focus();
    await settings.press("Enter");
    await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
    await expect(page.locator("#main")).toBeFocused();
    await expectQuietVaultTrail(page, "Settings");
    await expectVaultDestinationsReachable(page, "Settings");
    await page.goBack();
    await expectQuietVaultTrail(page, "Overview");
    await expectVaultDestinationsReachable(page, "Overview");
    await openVaultDestination(page, "Members");
    await expect(page).toHaveURL(/\/vault\/fixture\/members$/);
    await expect(vaultBreadcrumb(page)).toBeInViewport();
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
    const logo = header.getByRole("link", { name: /^AKB home\b/ });
    const search = header.getByRole("button", { name: "Search knowledge", exact: true });
    const account = header.getByRole("button", {
      name: `Account menu — ${displayName} (@fixture), reader@example.invalid`,
      exact: true,
    });
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
  await reader.getByRole("radio", { name: "Raw markdown", exact: true }).click();
  await expect(reader.getByTestId("doc-raw")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(reader).toHaveCount(0);
  await expect(page).toHaveURL(/\/search\?q=reading&source=document/);
  await expect(result).toBeFocused();
  await result.click();
  await reader.getByRole("radio", { name: "Raw markdown", exact: true }).click();
  await expect(reader.getByRole("radio", { name: "Raw markdown", exact: true })).toHaveAttribute("aria-checked", "true");
  await expect(reader.getByTestId("doc-raw")).toBeVisible();
  await reader.getByRole("button", { name: "Open document in vault" }).click();
  await expect(reader).toHaveCount(0);
  await expect(page).toHaveURL(/view=raw/);
  await expect(page.getByTestId("doc-raw")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Resource location", exact: true })).toContainText(title);
  await expectVaultDestinationsReachable(page);
  await expect(page.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
});

test("Vault Search preview keeps local location and restores its background breadcrumb", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture/search?q=reading&source=document");
  const navigation = page.getByRole("navigation", { name: "Current page", exact: true });
  await expectVaultDestinationsReachable(page);
  const initialPosition = await navigation.boundingBox();
  const result = page.getByRole("link", { name: /문서 읽기 작업공간/ }).first();
  await result.click();
  const preview = page.getByTestId("document-preview-dialog");
  await expect(preview).toBeVisible();
  await expect(preview.getByRole("navigation", { name: "Vault pages", exact: true })).toHaveCount(0);
  await expect(preview.getByRole("navigation", { name: "Resource location", exact: true })).toContainText(title);
  await preview.getByRole("radio", { name: "Raw markdown", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(preview).toHaveCount(0);
  await expect(page).toHaveURL(/\/vault\/fixture\/search\?q=reading&source=document$/);
  await expect(result).toBeFocused();
  await expectVaultDestinationsReachable(page);
  await expect(page.getByRole("button", { name: "Search knowledge", exact: true })).toBeVisible();
  expect(await navigation.boundingBox()).toEqual(initialPosition);
});

for (const width of [375, 1440]) test(`read-mode radios select immediately with keyboard navigation at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const rendered = page.getByRole("radio", { name: "Preview", exact: true });
  const raw = page.getByRole("radio", { name: "Raw markdown", exact: true });
  await expect(rendered).toHaveAttribute("aria-checked", "true");
  await rendered.focus();
  await page.keyboard.press("ArrowRight");
  await expect(raw).toBeFocused();
  await expect(raw).toHaveAttribute("aria-checked", "true");
  await expect(page.getByTestId("doc-raw")).toBeVisible();
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await page.keyboard.press("Enter");
  await expect(raw).toHaveAttribute("aria-checked", "true");
  await expect(page.getByTestId("doc-raw")).toBeVisible();
  await page.keyboard.press("Home");
  await expect(rendered).toBeFocused();
  await expect(rendered).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Space");
  await expect(rendered).toHaveAttribute("aria-checked", "true");
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  for (const tab of [rendered, raw]) {
    expect(await tab.evaluate(element => getComputedStyle(element, "::after").content)).toBe("none");
    expect(await tab.evaluate(element => parseFloat(getComputedStyle(element).borderRadius))).toBeGreaterThan(0);
  }
  await expect(rendered).toHaveAttribute("tabindex", "0");
  await expect(raw).toHaveAttribute("tabindex", "-1");
  expect(await rendered.evaluate(element => getComputedStyle(element).color)).not.toBe(await raw.evaluate(element => getComputedStyle(element).color));
});

for (const width of [375, 1440]) test(`modified keyboard Vault chooser links keep a dirty editor in its original tab at ${width}px`, async ({ page, context }) => {
  // Popups must also remain isolated from the real local backend. Page-owned
  // fixtures take precedence in the original tab; the new tab only tests linking.
  await context.route("**/api/v1/**", route => route.fulfill({ json: {} }));
  await page.setViewportSize({ width, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}?view=edit`);
  const editor = page.getByRole("textbox", { name: "Document body (markdown)" });
  await editor.fill("Keep editing here while checking Vault settings in another tab.");
  await expect(page.getByText("Draft saved locally", { exact: true })).toBeVisible();
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  const switcher = page.locator("#vault-workspace-navigation").getByRole("button", { name: "Switch vault: fixture", exact: true });
  await switcher.click();
  const picker = page.getByRole("dialog", { name: "Vaults", exact: true });
  const destination = picker.getByRole("link", { name: "fixture, current vault", exact: true });
  await destination.focus();
  // Browser-native new-tab gestures do not necessarily retain a JS opener.
  const popupPromise = context.waitForEvent("page");
  await page.keyboard.press("ControlOrMeta+Enter");
  const popup = await popupPromise;
  await expect(popup).toHaveURL(/\/vault\/fixture$/);
  await popup.close();
  await expect(page).toHaveURL(/\?view=edit$/);
  await expect(page.getByRole("dialog", { name: "Leave this document?", exact: true })).toHaveCount(0);
  await expect(picker).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(switcher).toBeFocused();
  await expect(editor).toContainText("Keep editing here");
});

for (const width of [375, 1440]) test(`Header Vault chooser protects an unsaved editor draft at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}?view=edit`);
  const editor = page.getByRole("textbox", { name: "Document body (markdown)" });
  const draft = "An unsaved draft kept while reviewing another Vault page.";
  await editor.fill(draft);
  await expect(page.getByText("Draft saved locally", { exact: true })).toBeVisible();
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  const switcher = page.locator("#vault-workspace-navigation").getByRole("button", { name: "Switch vault: fixture", exact: true });
  const switchVault = async () => {
    const picker = page.getByRole("dialog", { name: "Vaults", exact: true });
    if (!await picker.isVisible()) {
      if (!await switcher.isVisible()) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
      await switcher.click();
      const search = picker.getByRole("searchbox", { name: "Find a vault", exact: true });
      await expect(search).toBeFocused();
      await search.fill("fixture");
      await picker.getByRole("button", { name: /^Filter by role(?:, 1 active)?$/ }).click();
      await picker.getByRole("button", { name: "Can edit", exact: true }).click();
    }
    await picker.getByRole("link", { name: "fixture, current vault", exact: true }).click();
  };
  await switchVault();
  const confirmation = page.getByRole("dialog", { name: "Leave this document?", exact: true });
  await expect(confirmation).toBeVisible();
  await expect(page.locator("#vault-switcher-panel")).toHaveAttribute("inert", "");
  await expect(page.locator("#vault-switcher-panel")).toHaveAttribute("aria-hidden", "true");
  await expect(page).toHaveURL(/\?view=edit$/);
  await expect(page.getByRole("menu")).toHaveCount(0);
  await confirmation.getByRole("button", { name: "Keep editing", exact: true }).click();
  await expect(confirmation).toHaveCount(0);
  const remainingPicker = page.getByRole("dialog", { name: "Vaults", exact: true });
  await expect(remainingPicker).toBeVisible();
  await expect(remainingPicker).not.toHaveAttribute("inert", "");
  await expect(remainingPicker.getByRole("searchbox", { name: "Find a vault", exact: true })).toHaveValue("fixture");
  await expect(remainingPicker.getByRole("button", { name: "Can edit", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(remainingPicker.getByRole("searchbox", { name: "Find a vault", exact: true })).toBeFocused();
  await expect(page.locator("#vault-workspace-navigation")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(switcher).toBeFocused();
  await expect(editor).toContainText(draft);
  // Global Vaults remains a native directory link and consults the same guard.
  const globalVaults = width < 1024
    ? page.getByRole("navigation", { name: "Primary mobile navigation", exact: true }).getByRole("link", { name: "Vaults", exact: true })
    : page.getByTestId("app-sidebar").getByRole("link", { name: "Vaults", exact: true });
  await expect(globalVaults).toHaveAttribute("href", "/vault");
  await globalVaults.click();
  await expect(confirmation).toBeVisible();
  await expect(page).toHaveURL(/\?view=edit$/);
  await confirmation.getByRole("button", { name: "Keep editing", exact: true }).click();
  await expect(confirmation).toHaveCount(0);
  await expect(editor).toContainText(draft);
  // A global departure returns focus to the editor and closes the mobile
  // disclosure. Wait for that state before opening it again.
  await expect(page.getByRole("textbox", { name: "Document title", exact: true })).toBeFocused();
  if (width < 1024) await expect(page.locator("#vault-workspace-navigation")).toBeHidden();
  await switchVault();
  await confirmation.getByRole("button", { name: "Leave document", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  await expectQuietVaultTrail(page, "Overview");
  await expectVaultDestinationsReachable(page, "Overview");
});

test("toolbar publishing opens a review, never publishes on disclosure, and restores focus", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  let mutations = 0;
  page.on("request", request => {
    if (new URL(request.url()).pathname.includes("/publications") && request.method() !== "GET") mutations += 1;
  });
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const publish = documentActions(page);
  await publish.click();
  await expect(page.getByRole("menuitem", { name: "Publish", exact: true })).not.toHaveAttribute("aria-disabled", "true");
  await page.getByRole("menuitem", { name: "Publish", exact: true }).click();
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
  const trigger = documentActions(page);
  await expect(trigger).toBeVisible();
  const canvas = page.locator("#document-reading-canvas");
  const before = await canvas.boundingBox();
  const overlayCount = await page.locator('[data-slot="dialog-overlay"]').count();
  await openPublication(page, "Publish");
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
  await expect(trigger).toBeFocused();
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
  const trigger = documentActions(page);
  await expect(trigger).toBeVisible();
  const canvas = page.locator("#document-reading-canvas");
  const before = await canvas.boundingBox();
  const scrollBefore = await canvas.evaluate(element => element.scrollTop);
  await openPublication(page);
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

  // A reader action dismisses the popover on its first click.
  const details = page.getByRole("button", { name: "Document details", exact: true });
  await details.click();
  await expect(panel).toHaveCount(0);
  const detailsPanel = page.locator('[data-slot="document-context-panel"]');
  await expect(detailsPanel).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(detailsPanel).toHaveCount(0);
  await expect(details).toBeFocused();
  await openPublication(page);
  await trigger.click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");
  await openPublication(page);
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await openPublication(page);
  await panel.getByRole("button", { name: "Close public link" }).click();
  await expect(trigger).toBeFocused();
});

for (const viewport of [{ width: 375, height: 812 }, { width: 844, height: 390 }]) test(`public-link popover fits ${viewport.width}x${viewport.height} without moving the article`, async ({ page }) => {
  await page.setViewportSize(viewport);
  await fixture(page, false, true, body, "guide-public");
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const trigger = documentActions(page);
  await expect(trigger).toBeVisible();
  const canvas = page.locator("#document-reading-canvas");
  const before = await canvas.boundingBox();
  await openPublication(page);
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
  const trigger = documentActions(page);
  await expect(trigger).toBeVisible();
  const readingUrl = page.url();
  await openPublication(page);
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
    await openPublication(page);
    await preview.locator("#document-reading-canvas").click({ position: { x: 40, y: 80 } });
    await expect(panel).toHaveCount(0);
    await expect(preview).toBeVisible();
    await expect(page).toHaveURL(readingUrl);
  }
});

test("docked details remain open while overflow actions are usable", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const panel = await openDocumentDetails(page);
  await expect(panel).toHaveAttribute("data-mode", "docked");
  await documentActions(page).click();
  await expect(page.getByRole("menu")).toBeVisible();
  await expect(panel).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(documentActions(page)).toBeFocused();
  await expect(panel).toBeVisible();
  await panel.getByRole("button", { name: "Close document panel", exact: true }).click();
  await expect(page.getByRole("button", { name: "Document details", exact: true })).toBeFocused();
});

test("docked details preserve article scroll and remain open during reading", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page, false, false, body + "\n\n" + "A long article paragraph. ".repeat(2000));
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const canvas = page.locator("#document-reading-canvas");
  await expect(page.getByRole("heading", { name: "Authored heading", exact: true })).toBeVisible();
  await canvas.evaluate(element => { element.scrollTop = 240; });
  const before = (await canvas.boundingBox())!;
  const panel = await openDocumentDetails(page, "Properties");
  await expect(panel).toHaveAttribute("data-mode", "docked");
  expect(await canvas.evaluate(element => element.scrollTop)).toBe(240);
  const after = (await canvas.boundingBox())!;
  expect(after.width + (await panel.boundingBox())!.width).toBeCloseTo(before.width, 0);
  await panel.getByText(title, { exact: true }).click({ clickCount: 3 });
  await expect(panel).toBeVisible();
  await canvas.hover({ position: { x: 40, y: 80 } });
  await page.mouse.wheel(0, 200);
  await expect.poll(() => canvas.evaluate(element => element.scrollTop)).toBeGreaterThan(240);
  await canvas.click({ position: { x: 40, y: 80 } });
  await expect(panel).toBeVisible();
  await panel.getByRole("button", { name: "Close document panel", exact: true }).click();
  expect(await canvas.boundingBox()).toEqual(before);
});

test("properties enter unified authoring while relation dialogs retain their inspector", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  await openDocumentDetails(page, "Properties");
  const panel = page.locator('[data-slot="document-context-panel"]');
  const edit = panel.getByRole("button", { name: "Edit properties", exact: true });
  await edit.click();
  const form = page.getByRole("complementary", { name: "Document details", exact: true });
  const summary = form.getByRole("textbox", { name: "Summary", exact: true });
  await expect(summary).toBeFocused();
  await summary.fill("An unsaved properties draft");
  await form.getByRole("button", { name: "Document type", exact: true }).click();
  await page.getByRole("menuitemradio", { name: "reference", exact: true }).click();
  await expect(summary).toHaveValue("An unsaved properties draft");
  await expect(panel).toHaveCount(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  const discard = page.getByRole("dialog", { name: /Discard/ });
  await expect(discard).toBeVisible();
  await discard.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(summary).toHaveValue("An unsaved properties draft");
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await discard.getByRole("button", { name: /Discard/ }).click();
  await expect(form).toHaveCount(0);
  await expect(panel).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toBeFocused();

  await openDocumentDetails(page, "Related documents");
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

test("docked details close inside a preview and outline navigation preserves its route", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto("/search?q=reading&source=document");
  await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
  const preview = page.getByTestId("document-preview-dialog");
  const trigger = preview.getByRole("button", { name: "Document details", exact: true });
  const panel = await openDocumentDetails(page);
  await expect(panel).toHaveAttribute("data-mode", "docked");
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(preview).toBeVisible();
  await expect(trigger).toBeFocused();
  await openDocumentDetails(page);
  await preview.locator("#document-reading-canvas").click({ position: { x: 40, y: 80 } });
  await expect(panel).toBeVisible();
  const previewUrl = page.url();
  const previewHistory = await page.evaluate(() => history.state);
  const headingLink = panel.getByRole("link", { name: "Authored heading", exact: true });
  await headingLink.click();
  await expect(panel).toBeVisible();
  await expect(headingLink).toBeFocused();
  await expect(page).toHaveURL(previewUrl);
  expect(await page.evaluate(() => history.state)).toEqual(previewHistory);
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(preview).toBeVisible();
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
  await openDocumentDetails(page);
  const before = await workspaceMetrics();
  const outlineLink = page.getByRole("navigation", { name: "Document outline" }).getByRole("link", { name: "Checklist", exact: true });
  await outlineLink.click();
  await expect(outlineLink).toBeFocused();
  await expect(page.locator('[data-slot="document-context-panel"]')).toBeVisible();
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
    const innerMetrics = () => canvas.getByRole("heading", { name: "Section 12", exact: true, includeHidden: true }).evaluate(element => {
      const parents = [];
      for (let node = element.parentElement; node && node.id !== "document-reading-canvas"; node = node.parentElement) {
        parents.push({ tag: node.tagName, className: node.className, scrollTop: node.scrollTop, scrollLeft: node.scrollLeft });
      }
      return parents;
    });
    const innerBefore = await innerMetrics();
    const url = page.url();
    const history = await page.evaluate(() => window.history.state);
    const rail = page.getByRole("group", { name: "Document reading tools", exact: true });
    await openDocumentDetails(page);
    const panel = page.locator('[data-slot="document-context-panel"]');
    await expect(panel.getByRole("navigation", { name: "Document outline" })).toBeVisible();
    const before = await workspaceMetrics();
    const sectionLink = panel.getByRole("link", { name: "Section 12", exact: true });
    await sectionLink.click();
    await expect(panel).toBeVisible();
    await expect(sectionLink).toBeFocused();
    await expect(sectionLink).toHaveAttribute("aria-current", "location");
    const heading = canvas.getByRole("heading", { name: "Section 12", exact: true, includeHidden: true });
    await expect(heading).toBeInViewport();
    expect(await canvas.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    expect(await workspaceMetrics()).toEqual(before);
    expect(await innerMetrics()).toEqual(innerBefore);
    expect(page.url()).toBe(url);
    expect(await page.evaluate(() => window.history.state)).toEqual(history);
    // Continue navigating without reopening; keyboard activation preserves the
    // outline's focus while scrolling only the document canvas.
    const firstLink = panel.getByRole("link", { name: "Section 1", exact: true });
    await firstLink.focus();
    await firstLink.press("Enter");
    await expect(firstLink).toBeFocused();
    await expect(firstLink).toHaveAttribute("aria-current", "location");
    await expect(canvas.getByRole("heading", { name: "Section 1", exact: true, includeHidden: true })).toBeInViewport();
    expect(await workspaceMetrics()).toEqual(before);
    const finalLink = panel.getByRole("link", { name: "Final section", exact: true });
    await finalLink.click();
    await expect(panel).toBeVisible();
    await expect(finalLink).toBeFocused();
    await expect(canvas.getByRole("heading", { name: "Final section", exact: true, includeHidden: true })).toBeInViewport();
    expect(await workspaceMetrics()).toEqual(before);
    await panel.getByRole("button", { name: "Close document panel", exact: true }).click();
    await expect(panel).toHaveCount(0);
    await expect(rail.getByRole("button", { name: "Document details", exact: true })).toBeFocused();
  });
}

test("resizing defers context mode changes until nested editing closes", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const panel = page.locator('[data-slot="document-context-panel"]');
  await openDocumentDetails(page, "Related documents");
  await panel.getByRole("button", { name: "Add", exact: true }).click();
  const relation = page.getByRole("dialog", { name: "Add relation", exact: true });
  await expect(relation).toBeVisible();
  await page.setViewportSize({ width: 375, height: 800 });
  await expect(relation).toBeVisible();
  await expect(panel).toHaveAttribute("data-mode", "docked");
  await relation.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(relation).toHaveCount(0);
  await expect(panel).toHaveAttribute("data-mode", "overlay");
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);

  await page.setViewportSize({ width: 2560, height: 1000 });
  await openDocumentDetails(page, "Properties");
  await panel.getByRole("button", { name: "Edit properties", exact: true }).click();
  const details = page.getByRole("complementary", { name: "Document details", exact: true });
  await details.getByRole("textbox", { name: "Summary", exact: true }).fill("Preserve on resize");
  await page.setViewportSize({ width: 375, height: 800 });
  await expect(details).toBeVisible();
  await expect(details.getByRole("textbox", { name: "Summary", exact: true })).toHaveValue("Preserve on resize");
  await expect(page.getByRole("dialog", { name: "Discard unsaved changes?", exact: true })).toHaveCount(0);
  await expect(panel).toHaveCount(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("dialog", { name: "Discard unsaved changes?", exact: true }).getByRole("button", { name: "Discard changes", exact: true }).click();
  await expect(details).toHaveCount(0);
  await openDocumentDetails(page, "Properties");
  await expect(panel).toHaveAttribute("data-mode", "overlay");

  await openDocumentDetails(page, "Related documents");
  await panel.getByRole("button", { name: "Add", exact: true }).click();
  await expect(relation).toBeVisible();
  await page.setViewportSize({ width: 2560, height: 1000 });
  await expect(relation).toBeVisible();
  await expect(panel).toHaveAttribute("data-mode", "overlay");
  await relation.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(relation).toHaveCount(0);
  await expect(panel).toHaveAttribute("data-mode", "docked");
  await expect(panel.getByRole("button", { name: "Add", exact: true })).toBeFocused();
});

test("preview geometry is independent of its search entry and background sidebars", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await fixture(page);
  let expectedBox: Awaited<ReturnType<Locator["boundingBox"]>> = null;
  for (const folded of [false, true]) {
    for (const entry of [
      { route: `/vault/fixture/doc/${encodeURIComponent(path)}`, global: true },
      { route: "/search?q=reading&source=document", global: true },
      { route: "/search?q=reading&source=document", global: false },
      { route: "/vault/fixture/search?q=reading&source=document", global: false },
    ]) {
      await page.goto("/search");
      await page.evaluate(folded => {
        localStorage.setItem("akb.treeVisible", folded ? "0" : "1");
      }, folded);
      await page.goto(entry.route);
      if (entry.global) {
        await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
        const search = page.getByTestId("global-search-dialog");
        await search.getByRole("combobox").fill("reading");
        await search.getByRole("option", { name: /문서 읽기 작업공간/ }).click();
      } else await page.getByRole("region", { name: "Search results", exact: true }).getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
      const preview = page.getByTestId("document-preview-dialog");
      await expect(preview.locator(".document-reading-flow")).toBeVisible();
      const box = (await preview.boundingBox())!;
      expect(box.x).toBeCloseTo((1920 - box.width) / 2, 0);
      expect(box.y).toBeCloseTo((1080 - box.height) / 2, 0);
      expect(box.x).toBeGreaterThanOrEqual(16);
      expect(box.x).toBeLessThanOrEqual(48);
      if (expectedBox) expect(box).toEqual(expectedBox);
      expectedBox = box;
      await page.mouse.click(box.x / 2, 540);
      await expect(preview).toHaveCount(0);
      expect(new URL(page.url()).pathname + new URL(page.url()).search).toBe(entry.route);
    }
  }
});

test("reading gutters adapt to the reader container and align Preview with Raw", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1440 });
  await fixture(page, false, true);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const content = page.getByRole("region", { name: "Document content", exact: true });
  await expect(content.locator(".document-reading-flow")).not.toHaveClass(/document-reading-wide/);
  const paragraph = content.locator(".ProseMirror > p").first();
  // Simulate the same component embedded in a narrow or wide reading canvas,
  // without changing the viewport (sidebar/panel changes must reflow it too).
  for (const { width, gutter } of [{ width: 600, gutter: 16 }, { width: 1200, gutter: 24 }, { width: 1800, gutter: 32 }]) {
    await content.evaluate((el, width) => { el.style.width = `${width}px`; }, width);
    const outer = (await content.boundingBox())!;
    const rendered = (await paragraph.boundingBox())!;
    const firstBlock = (await content.locator(".ProseMirror > :first-child").boundingBox())!;
    const measure = Math.min(832, width - gutter * 2);
    expect(rendered.x - outer.x).toBeCloseTo((width - measure) / 2, 0);
    expect(rendered.width).toBeCloseTo(measure, 0);
    await page.getByRole("radio", { name: "Raw markdown", exact: true }).click();
    const raw = (await page.getByTestId("doc-raw").boundingBox())!;
    expect(raw.x).toBeCloseTo(rendered.x, 0);
    expect(raw.width).toBeCloseTo(rendered.width, 0);
    expect(raw.y).toBeCloseTo(firstBlock.y, 0);
    await page.getByRole("radio", { name: "Preview", exact: true }).click();
    await expect(content.locator(".document-reading-flow")).not.toHaveClass(/document-reading-wide/);
  }
});

for (const scenario of [
  { width: 2560, height: 1440, dark: false, entry: "modal" },
  { width: 2560, height: 1440, dark: true, entry: "modal" },
  { width: 1440, height: 900, dark: false, entry: "modal" },
  { width: 375, height: 812, dark: false, entry: "modal" },
  { width: 1920, height: 1080, dark: false, entry: "page" },
]) test(`search preview uses fluid content gutters at ${scenario.width}px ${scenario.dark ? "dark" : "light"} from ${scenario.entry}`, async ({ page }, testInfo) => {
  await page.setViewportSize(scenario);
  await fixture(page, scenario.dark);
  await page.goto("/search?q=reading&source=document");
  const searchTrigger = page.getByRole("button", { name: "Search knowledge", exact: true });
  if (scenario.entry === "modal") {
    await searchTrigger.click();
    const search = page.getByTestId("global-search-dialog");
    await search.getByRole("combobox").fill("reading");
    await search.getByRole("option", { name: /문서 읽기 작업공간/ }).click();
  } else {
    await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
  }
  const preview = page.getByTestId("document-preview-dialog");
  const canvas = preview.locator("#document-reading-canvas");
  const flow = preview.locator(".document-reading-flow .ProseMirror");
  const paragraph = flow.locator(":scope > p").first();
  await expect(paragraph).toBeVisible();
  const canvasBox = (await canvas.boundingBox())!;
  const canvasWidth = await canvas.evaluate(el => el.clientWidth);
  const gutter = await preview.locator(".document-reading-inset").evaluate(element => parseFloat(getComputedStyle(element).paddingLeft));
  expect(gutter).toBeGreaterThanOrEqual(16);
  expect(gutter).toBeLessThanOrEqual(32);
  const measure = Math.min(832, canvasWidth - gutter * 2);
  const inset = (canvasWidth - measure) / 2;
  for (const block of await flow.locator(":scope > h2, :scope > p, :scope > ul, :scope > pre, :scope > .akb-md-table, :scope > .tableWrapper").all()) {
    const box = (await block.boundingBox())!;
    expect(box.x - canvasBox.x).toBeCloseTo(inset, 0);
    expect(box.width).toBeCloseTo(measure, 0);
  }
  // A long code line scrolls locally, never widening the overlay or its canvas.
  expect(await flow.locator(":scope > pre").evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
  expect(await canvas.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("fluid-preview.png") });
  if (scenario.width === 2560 && !scenario.dark) {
    // Resize the already-open dialog across breakpoints; no remount may be
    // needed to replace a stale sidebar measurement or a viewport-based gutter.
    for (const width of [1440, 768, 375]) {
      await page.setViewportSize({ width, height: 900 });
      const box = (await preview.boundingBox())!;
      expect(box.x).toBeCloseTo((width - box.width) / 2, 0);
      expect(box.y).toBeCloseTo((900 - box.height) / 2, 0);
      if (width < 640) {
        expect(box.x).toBe(0);
        expect(box.height).toBe(900);
      } else {
        expect(box.x).toBeGreaterThanOrEqual(16);
        expect(box.x).toBeLessThanOrEqual(48);
      }
      const canvasBox = (await canvas.boundingBox())!;
      const paragraphBox = (await paragraph.boundingBox())!;
      const gutter = await preview.locator(".document-reading-inset").evaluate(element => parseFloat(getComputedStyle(element).paddingLeft));
      const canvasWidth = await canvas.evaluate(element => element.clientWidth);
      const measure = Math.min(832, canvasWidth - gutter * 2);
      expect(paragraphBox.width).toBeCloseTo(measure, 0);
      expect(paragraphBox.x - canvasBox.x).toBeCloseTo((canvasWidth - measure) / 2, 0);
      expect(await canvas.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    }
  }
  if (scenario.entry === "modal") {
    await page.keyboard.press("Escape");
    await expect(preview).toHaveCount(0);
    const resumedSearch = page.getByTestId("global-search-dialog");
    await expect(resumedSearch.getByRole("combobox")).toHaveValue("reading");
    await expect(resumedSearch.getByRole("combobox")).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(resumedSearch).toHaveCount(0);
    await expect(searchTrigger).toBeFocused();
  } else {
    await preview.getByRole("button", { name: "Open document in vault", exact: true }).click();
    await expect(preview).toHaveCount(0);
    // Full-page and preview share the automatic reading measure.
    await expect(page.locator(".document-reading-flow")).not.toHaveClass(/document-reading-wide/);
  }
});

for (const entry of ["page", "modal"] as const) test(`dirty preview promotion resumes once after confirmation from ${entry} search`, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  let writes = 0;
  page.on("request", request => {
    if (new URL(request.url()).pathname.includes("/api/") && request.method() !== "GET") writes += 1;
  });
  await page.goto("/search?q=reading&source=document");
  if (entry === "modal") {
    await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
    const search = page.getByTestId("global-search-dialog");
    await search.getByRole("combobox").fill("reading");
    await search.getByRole("option", { name: /문서 읽기 작업공간/ }).click();
  } else {
    await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
  }
  const preview = page.getByTestId("document-preview-dialog");
  await preview.getByRole("button", { name: "Edit", exact: true }).click();
  await preview.getByRole("textbox", { name: "Document title", exact: true }).fill("Unsaved preview title");
  await preview.getByRole("button", { name: "Open document in vault", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "Leave this document?", exact: true });
  await confirmation.getByRole("button", { name: "Keep editing", exact: true }).click();
  await expect(preview.getByRole("textbox", { name: "Document title", exact: true })).toHaveValue("Unsaved preview title");
  await preview.getByRole("button", { name: "Open document in vault", exact: true }).click();
  await confirmation.getByRole("button", { name: "Leave document", exact: true }).click();
  await expect(preview).toHaveCount(0);
  await expect(confirmation).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Document workspace", exact: true })).toHaveAttribute("data-presentation", "page");
  await expect(page).toHaveURL(url => url.pathname === `/vault/fixture/doc/${encodeURIComponent(path)}` && url.searchParams.get("view") === "edit");
  await expect(page.getByRole("textbox", { name: "Document title", exact: true })).toHaveValue("Unsaved preview title");
  expect(await page.evaluate(() => history.state.usr)).toBeNull();
  expect(writes).toBe(0);
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
  const info = preview.getByRole("button", { name: "Document details", exact: true });
  await info.click();
  const context = page.getByRole("dialog", { name: "Document details", exact: true });
  await expect(context).toHaveAttribute("data-mode", "overlay");
  await expect(context.getByRole("button", { name: "Close document panel", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(context).toHaveCount(0);
  await expect(preview).toBeVisible();
  await expect(info).toBeFocused();
  await expect(page).toHaveURL(new RegExp(`/doc/${encodeURIComponent(path)}$`));
  const previewUrl = page.url();
  const previewHistory = await page.evaluate(() => history.state);
  await preview.getByRole("button", { name: "Document details", exact: true }).click();
  const outline = page.getByRole("dialog", { name: "Document details", exact: true });
  const headingLink = outline.getByRole("link", { name: "Authored heading", exact: true });
  await headingLink.click();
  await expect(outline).toBeVisible();
  await expect(headingLink).toBeFocused();
  await expect(preview).toBeVisible();
  await expect(page).toHaveURL(previewUrl);
  expect(await page.evaluate(() => history.state)).toEqual(previewHistory);
  await page.keyboard.press("Escape");
  await expect(outline).toHaveCount(0);
  await expect(preview).toBeVisible();
  await expect(preview.getByRole("button", { name: "Document details", exact: true })).toBeFocused();
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
  // macOS uses Command+Right for the line-end caret action; End only scrolls.
  await editor.press(process.platform === "darwin" ? "Meta+ArrowRight" : "End");
  await editor.pressSequentially(" and continues safely");
  await expect(editor).toContainText("A local draft that must survive verification and continues safely");
  await page.route("**/vaults/fixture/info", route => route.fulfill({ json: { name: "fixture", role: "reader", is_archived: false, is_external_git: false } }));
  await page.evaluate(() => window.dispatchEvent(new Event("akb:revalidate-access")));
  await expect(page.getByText("Read-only · Draft preserved")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save changes", exact: true })).toBeDisabled();
  await expect(editor).toContainText("A local draft that must survive verification and continues safely");
});

for (const dark of [false, true]) test(`code blocks follow the automatic reading measure (${dark ? "dark" : "light"})`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 2560, height: 1000 });
  await fixture(page, dark, true, [
    "# Code alignment", "The code blocks should share this paragraph’s edges.",
    '```js\nconsole.log("short");\n```',
    "```text\n" + "long_code_".repeat(250) + "\n```",
  ].join("\n\n"));
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const flow = page.locator(".document-reading-flow .ProseMirror");
  const paragraph = flow.locator(":scope > p").first();
  const blocks = flow.locator(":scope > pre");
  await expect(blocks).toHaveCount(2);
  const checkEdges = async () => {
    const text = (await paragraph.boundingBox())!;
    for (const block of await blocks.all()) {
      const box = (await block.boundingBox())!;
      expect(box.x).toBeCloseTo(text.x, 0);
      expect(box.width).toBeCloseTo(text.width, 0);
    }
    expect(await blocks.first().evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    expect(await blocks.last().evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    return text.width;
  };
  expect(await checkEdges()).toBe(832);
  const readingBox = (await paragraph.boundingBox())!;
  await page.getByRole("radio", { name: "Raw markdown", exact: true }).click();
  const rawBox = (await page.getByTestId("doc-raw").boundingBox())!;
  expect(rawBox.x).toBeCloseTo(readingBox.x, 0);
  expect(rawBox.width).toBe(832);
  await page.getByRole("radio", { name: "Preview", exact: true }).click();
  await page.screenshot({ path: testInfo.outputPath("automatic-code-width.png") });
  await documentActions(page).click();
  await expect(page.getByRole("menuitemradio", { name: /^(Standard|Wide)$/ })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 1440, height: 1000 });
  expect(await checkEdges()).toBeLessThanOrEqual(832);
  await page.setViewportSize({ width: 375, height: 812 });
  expect(await checkEdges()).toBeLessThan(375);
  const longCode = blocks.last();
  await expect(longCode).toHaveAttribute("tabindex", "0");
  await longCode.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => longCode.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
  await page.screenshot({ path: testInfo.outputPath("mobile-code-width.png") });
});

for (const dark of [false, true]) for (const entry of ["document", "search", "quick-search"] as const) {
  test(`images share the reading measure in ${entry} (${dark ? "dark" : "light"})`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 2560, height: 1440 });
    await fixture(page, dark, true, [
      "# Image alignment",
      "Images and their surrounding text share one reading boundary.",
      "![Small diagram](/reading-fixture.svg)",
      "![Wide diagram](/reading-large.svg)",
      "![Tall diagram](/reading-tall.svg)",
      "> ![Quoted diagram](/reading-fixture.svg)",
      "- List image ![Listed diagram](/reading-fixture.svg)",
      "Before ![Inline diagram](/reading-fixture.svg) after.",
    ].join("\n\n"));
    for (const image of [
      { path: "reading-large.svg", width: 2400, height: 1200 },
      { path: "reading-tall.svg", width: 240, height: 960 },
    ]) {
      await page.route(`**/${image.path}`, route => route.fulfill({
        contentType: "image/svg+xml",
        body: `<svg xmlns="http://www.w3.org/2000/svg" width="${image.width}" height="${image.height}"><rect width="100%" height="100%" fill="teal"/></svg>`,
      }));
    }
    if (entry === "document") await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
    else {
      await page.goto("/search?q=reading&source=document");
      if (entry === "quick-search") {
        await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
        const search = page.getByTestId("global-search-dialog");
        await search.getByRole("combobox").fill("reading");
        await search.getByRole("option", { name: /문서 읽기 작업공간/ }).click();
      } else await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
      await expect(page.getByTestId("document-preview-dialog")).toBeVisible();
    }
    const flow = page.locator(".document-reading-flow .ProseMirror");
    const images = flow.locator("img[data-markdown-image]");
    await expect(images).toHaveCount(6);
    await expect.poll(() => images.evaluateAll(elements => elements.every(element => {
      const image = element as HTMLImageElement;
      return image.complete && image.naturalWidth > 0;
    }))).toBe(true);

    const checkAlignment = async () => {
      const heading = (await flow.locator(":scope > h2").boundingBox())!;
      for (const block of await flow.locator(":scope > p, :scope > blockquote, :scope > ul").all()) {
        const box = (await block.boundingBox())!;
        expect(box.x).toBeCloseTo(heading.x, 0);
        expect(box.width).toBeCloseTo(heading.width, 0);
      }
      // Nested images respect their parent indentation; natural-size images
      // never stretch, and large images shrink without changing aspect ratio.
      for (const image of await images.all()) {
        const geometry = await image.evaluate(element => {
          const imageElement = element as HTMLImageElement;
          const box = element.getBoundingClientRect();
          // The shared renderer can flatten a single image-only quote.
          const parent = element.closest<HTMLElement>("p, blockquote")!;
          const parentBox = parent.getBoundingClientRect();
          const style = getComputedStyle(parent);
          const paddingLeft = parseFloat(style.paddingLeft);
          const paddingRight = parseFloat(style.paddingRight);
          return {
            x: box.x, width: box.width, height: box.height,
            parentX: parentBox.x + parent.clientLeft + paddingLeft,
            parentWidth: parent.clientWidth - paddingLeft - paddingRight,
            naturalWidth: imageElement.naturalWidth, naturalHeight: imageElement.naturalHeight,
          };
        });
        expect(geometry.x).toBeCloseTo(geometry.parentX, 0);
        expect(geometry.width).toBeCloseTo(Math.min(geometry.naturalWidth, geometry.parentWidth), 0);
        expect(geometry.width / geometry.height).toBeCloseTo(geometry.naturalWidth / geometry.naturalHeight, 2);
        expect(geometry.x + geometry.width).toBeLessThanOrEqual(heading.x + heading.width + 1);
      }
      await expect(flow.locator(":scope > p").last()).toHaveText("Before  after.");
      expect(await page.locator("#document-reading-canvas").evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      return heading.width;
    };
    expect(await checkAlignment()).toBe(832);
    await documentActions(page).click();
    await expect(page.getByRole("menuitemradio", { name: /^(Standard|Wide)$/ })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.screenshot({ path: testInfo.outputPath("automatic-image-alignment.png") });
    for (const width of [1440, 375]) {
      await page.setViewportSize({ width, height: 900 });
      await checkAlignment();
    }
    await page.screenshot({ path: testInfo.outputPath("mobile-image-alignment.png") });
  });
}

test("short prose has no artificial body height", async ({ page }) => {
  await fixture(page, false, false, "One line of knowledge.", undefined, { summary: "" });
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const body = page.locator(".document-reading-flow");
  await expect(body).toHaveText("One line of knowledge.");
  expect((await body.boundingBox())!.height).toBeLessThan(100);
});

test("wide tables stay locally scrollable at 200 percent CSS zoom", async ({ page }, testInfo) => {
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
  const location = page.getByRole("navigation", { name: "Resource location", exact: true });
  await expect(location).toBeInViewport();
  const indexing = page.getByTestId("header-indexing-status");
  await expect(indexing).toBeEmpty();
  await expect.poll(() => indexing.evaluate(element => element.clientWidth)).toBe(0);
  const currentTitle = location.locator('[aria-current="page"]');
  try {
    await expect.poll(() => currentTitle.evaluate(element => element.getBoundingClientRect().width / 2), {
      message: "Current resource title keeps its 30px minimum before CSS zoom",
    }).toBeGreaterThan(30);
  } catch (error) {
    await testInfo.attach("zoomed-header-geometry", {
      body: JSON.stringify(await location.evaluate(element => {
        const box = (node: Element | null) => {
          if (!node) return null;
          const style = getComputedStyle(node);
          return { clientWidth: node.clientWidth, visualWidth: node.getBoundingClientRect().width, flex: style.flex, display: style.display, width: style.width, minWidth: style.minWidth, maxWidth: style.maxWidth };
        };
        const header = document.querySelector(".app-header")!;
        const wrapper = element.parentElement!;
        const indexing = header.querySelector('[data-testid="header-indexing-status"]');
        return {
          cssZoom: 2,
          header: box(header),
          locationWrapper: box(wrapper),
          breadcrumb: box(element),
          list: box(element.querySelector("ol")),
          items: Array.from(element.querySelectorAll("ol > li"), box),
          currentTitle: box(element.querySelector('[aria-current="page"]')),
          searchControls: box(wrapper.nextElementSibling),
          searchTrigger: box(header.querySelector("#global-search-trigger")),
          accountControls: box(header.querySelector('[aria-label="Primary mobile navigation"]')?.nextElementSibling ?? null),
          indexing: { ...box(indexing), childCount: indexing?.childElementCount, text: indexing?.textContent },
        };
      })),
      contentType: "application/json",
    });
    throw error;
  }
  const titleWidth = await currentTitle.evaluate(element => element.getBoundingClientRect().width / 2);
  await testInfo.attach("zoomed-resource-location-geometry", {
    body: JSON.stringify({ titleWidth, cssZoom: 2 }),
    contentType: "application/json",
  });
  await expect(currentTitle).toBeInViewport({ ratio: 1 });
  await expect(vaultBreadcrumb(page)).toBeInViewport({ ratio: 1 });
  await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toBeInViewport();
  await region.focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => region.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
  await page.screenshot({ path: testInfo.outputPath("zoomed-reader.png") });
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
    await page.route(/\/api\/v1\/tables\/fixture(?:\?.*)?$/, route => route.fulfill({ json: { items: [{
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
    }
    const drawer = await revealVaultNavigation(page);
    await expect(page.locator('[data-slot="vault-collections-sidebar"]').getByRole("navigation", { name: "fixture explorer", exact: true })).toBeVisible();
    if (drawer) {
      await page.getByRole("navigation", { name: "fixture explorer", exact: true }).getByRole("button").first().focus();
      await page.keyboard.press("Escape");
      await expect(page.locator("#vault-workspace-navigation")).toBeHidden();
    }
    await expect(page.getByRole("button", { name: /^Explore vault:/ })).toHaveCount(0);
    const navigation = page.getByRole("navigation", { name: "Vault sections", exact: true });
    await expectVaultDestinationsReachable(page);
    await expect(navigation).toHaveCount(0);
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
    await expectQuietVaultTrail(page, "Settings");
    await page.goBack();
    await page.goBack();
    await expect(trail).toContainText(kind === "file" ? "Reading notes.txt" : "reading_catalog");
    await expect(navigation).toHaveCount(0);
    await expectVaultDestinationsReachable(page);
  });
}

for (const scenario of [
  { width: 375, dark: false, preview: false },
  { width: 375, dark: true, preview: true },
  { width: 1440, dark: false, preview: true },
  { width: 1440, dark: true, preview: false },
]) test(`document metadata byline wraps authors at ${scenario.width}px ${scenario.dark ? "dark" : "light"} ${scenario.preview ? "preview" : "route"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: scenario.width, height: 900 });
  const author = "김민정 플랫폼 지식 운영팀 — " + "한글작성자이름".repeat(12);
  await fixture(page, scenario.dark, false, body, undefined, {
    created_by_name: author, created_by: "0c37e906-6db0-48c2-ac5d-576d0797b3f7",
  });
  if (scenario.preview) {
    await page.goto("/search?q=reading&source=document");
    await page.getByRole("link", { name: /문서 읽기 작업공간/ }).first().click();
  } else await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const tools = page.getByRole("group", { name: "Document reading tools", exact: true });
  const byline = tools.getByRole("group", { name: "Document metadata", exact: true });
  const name = byline.getByText(author, { exact: true });
  await expect(name).toBeVisible();
  await expect(byline.getByText("Author", { exact: true })).toBeVisible();
  await expect(byline).not.toContainText("Last edited by");
  await expect(byline).not.toContainText("0c37e906");
  await expect(byline.locator("time")).toHaveCount(1);
  const timestamp = byline.locator("time");
  await expect(timestamp).toHaveAttribute("datetime", new Date(lastEdited).toISOString());
  await expect(tools.getByRole("button", { name: "Edit", exact: true })).toBeInViewport({ ratio: 1 });
  await expect(tools.getByRole("button", { name: "Document details", exact: true })).toBeInViewport({ ratio: 1 });
  await expect(page.getByRole("region", { name: "Document preview", exact: true })).toContainText("Authored heading");
  expect(await byline.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  expect(await name.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  const contrast = await byline.locator(':scope > span > span, time > [aria-hidden="true"]').evaluateAll(elements => {
    const channels = (color: string) => (color.match(/[\d.]+/g) || []).map(Number);
    const luminance = (rgb: number[]) => rgb.slice(0, 3).map(v => {
      const c = v / 255;
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
    return elements.map(element => {
      let parent: Element | null = element;
      let background = [255, 255, 255];
      while (parent) {
        const color = channels(getComputedStyle(parent).backgroundColor);
        if (color.length === 3 || color[3] === 1) { background = color; break; }
        parent = parent.parentElement;
      }
      const foreground = luminance(channels(getComputedStyle(element).color));
      const bg = luminance(background);
      return { text: element.textContent, ratio: (Math.max(foreground, bg) + 0.05) / (Math.min(foreground, bg) + 0.05) };
    });
  });
  expect(contrast).toHaveLength(3);
  for (const sample of contrast) expect(sample.ratio, sample.text || "Document metadata contrast").toBeGreaterThanOrEqual(4.5);
  await timestamp.focus();
  await expect(timestamp).toBeFocused();
  await expect(page.getByRole("tooltip")).toContainText("Last edited:");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await expect(timestamp).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("document-byline.png"), animations: "disabled" });

  const panel = await openDocumentDetails(page, "Properties");
  const propertyAuthor = panel.getByText(author, { exact: true });
  await expect(propertyAuthor).toBeVisible();
  expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  expect(await propertyAuthor.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
});
