import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");

// Browser-owned HTTP fixtures: no mutations or requests reach a user's Vault.
const title = "문서 읽기 작업공간 — Architecture and operating decisions for a shared knowledge workspace";
const path = "guides/team/operations/reading.md";
const body = [
  "# Authored heading",
  "읽기 흐름을 유지하면서 팀의 지식을 탐색합니다. ".repeat(12),
  "- First item\n- Second item",
  "| Name | Role |\n| --- | --- |\n| Reader | Read documents |",
  "```text\n" + "long_code_".repeat(100) + "\n```",
  "![Small diagram](/reading-fixture.svg)",
].join("\n\n");

async function fixture(page: Page, dark = false, folded = false, content = body) {
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
    if (pathname.includes("/documents/fixture/") && !pathname.endsWith("/history")) return route.fulfill({ json: { path, title, content, status: "active", current_commit: "aaaaaaaaaaaaa", summary: "An orientation to the resource reading workspace.", tags: ["guide"] } });
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
}

const vaultDestinations = [
  ["Overview", "/vault/fixture"],
  ["Search this vault", "/vault/fixture/search"],
  ["Graph", "/vault/fixture/graph"],
  ["Public links", "/vault/fixture/publications"],
  ["Members", "/vault/fixture/members"],
  ["Settings", "/vault/fixture/settings"],
] as const;

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
  if (await more.isVisible()) {
    await expect(more).toHaveText("More");
    await more.click();
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
}

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
    await expect(page.locator("h1#doc-title")).toHaveCount(1);
    await expect(page.locator('[data-slot="resource-command-row"]')).toHaveCount(1);
    const commandMetrics = await page.locator('[data-slot="resource-command-row"] [data-reader-control]').evaluateAll(elements => elements.map(element => ({
      height: element.getBoundingClientRect().height,
      font: getComputedStyle(element).fontSize,
    })));
    expect(new Set(commandMetrics.map(item => item.height)).size).toBe(1);
    expect(commandMetrics.every(item => item.font === "14px")).toBe(true);
    const commandWidth = (await page.locator('[data-slot="resource-command-row"]').boundingBox())!.width;
    expect(commandMetrics.every(item => item.height === (commandWidth >= 768 ? 32 : 44))).toBe(true);
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
    await expect(collections.getByRole("link", { name: /^(Search this vault|Graph|Public links|Members|Settings)$/ })).toHaveCount(0);
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
    const code = page.getByRole("region", { name: /^Scrollable(?: [a-z0-9-]+)? code block$/i });
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
    if (width === 2560) {
      await page.getByRole("region", { name: "Document workspace" }).getByRole("button", { name: `Actions for ${title}` }).click();
      await page.getByRole("menuitemradio", { name: "Wide", exact: true }).click();
      await expect(page.locator(".document-reading-wide")).toBeVisible();
      const wide = await flow.locator("p").first().boundingBox();
      expect(wide!.width).toBeGreaterThan(measures.paragraph.width);
    }
    const beforeInfo = (await flow.boundingBox())!.width;
    await page.getByRole("region", { name: "Document workspace" }).getByRole("button", { name: `Actions for ${title}` }).click();
    await page.getByRole("menuitem", { name: "Document info", exact: true }).click();
    await expect(page.locator(".block.break-words").filter({ hasText: title })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Outline", exact: false })).toBeVisible();
    expect((await flow.boundingBox())!.width).toBe(beforeInfo);
    await page.getByRole("button", { name: "Close document panel" }).click();
    await expect(page.getByRole("region", { name: "Document workspace" }).getByRole("button", { name: `Actions for ${title}` })).toBeFocused();
  });
}

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

test("working-page navigation stays outside content scrolling and independent from collection collapse", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await fixture(page);
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
  expect(settingsPosition.x + settingsPosition.width).toBe(position.x + position.width - rightInset);
  expect(membersPosition.x - publicPosition.x - publicPosition.width).toBeGreaterThan(100);
  const managementRow = (await collections.locator('[data-slot="collection-management-row"]').boundingBox())!;
  expect(position.y + position.height).toBe(managementRow.y + managementRow.height);
  for (const [name, href] of vaultDestinations) {
    await openVaultDestination(page, name);
    await expect(page).toHaveURL(new RegExp(`${href}$`));
    await expect(navigation.getByRole("link", { name, exact: true })).toHaveAttribute("aria-current", "page");
    await expect(tree).toBeVisible();
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
  await expect(navigation.getByRole("link")).toHaveCount(6);
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
  await expect(navigation.getByRole("link")).toHaveCount(6);
  const wide = (await navigation.boundingBox())!;
  expect(wide.width).toBeGreaterThan(compact.width);
  expect(wide.height).toBe(compact.height);
  expect(page.viewportSize()).toEqual({ width: 1440, height: 1000 });
  const settings = navigation.getByRole("link", { name: "Settings", exact: true });
  const wideSettings = (await settings.boundingBox())!;
  const rightInset = await navigation.evaluate(element => parseFloat(getComputedStyle(element).paddingRight));
  expect(wideSettings.x + wideSettings.width).toBe(wide.x + wide.width - rightInset);
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
  await expect(navigation.getByRole("link", { name: "Search this vault", exact: true })).toHaveAttribute("aria-current", "page");
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
  await expect(navigation.getByRole("link", { name: "Search this vault", exact: true })).toHaveAttribute("aria-current", "page");
  expect(await navigation.boundingBox()).toEqual(initialPosition);
});

for (const width of [375, 1440]) test(`read-mode segments retain manual keyboard activation at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const rendered = page.getByRole("tab", { name: "Rendered", exact: true });
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
  await expect(review.getByText(/without signing in/)).toBeVisible();
  await expect(review.getByRole("button", { name: "Publish", exact: true })).toBeVisible();
  expect(mutations).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("publish-review.png") });
  await review.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(publish).toBeFocused();
  expect(mutations).toBe(0);
});

test("an open inspector retains a focus destination when its overflow menu is reused", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const actions = page.getByRole("region", { name: "Document workspace" }).getByRole("button", { name: `Actions for ${title}` });
  await actions.click();
  await page.getByRole("menuitem", { name: "Document info", exact: true }).click();
  const close = page.getByRole("button", { name: "Close document panel" });
  await expect(close).toBeFocused();
  await actions.click();
  await page.getByRole("menuitem", { name: "Table of contents", exact: true }).click();
  await expect(close).toBeFocused();
  await expect(page.getByRole("tab", { name: /^Outline/ })).toHaveAttribute("aria-selected", "true");
  await actions.click();
  await page.getByRole("menuitemradio", { name: "Wide", exact: true }).click();
  await expect(actions).toBeFocused();
  await actions.click();
  await page.keyboard.press("Escape");
  await expect(actions).toBeFocused();
  await expect(close).toBeVisible();
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
