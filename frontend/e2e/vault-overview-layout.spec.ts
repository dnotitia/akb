import { expect, test, type Page } from "@playwright/test";
import axe from "axe-core";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP fixtures.");

// Every API request is fulfilled in the browser; no user Vault is read or changed.
async function fixture(page: Page, { dark = false, folded = false, empty = false, legacy = false, long = false, description = "Architecture, operating guides, and shared knowledge for the platform team." } = {}) {
  await page.addInitScript(({ dark, folded }) => {
    localStorage.setItem("akb_token", "overview-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    if (folded) {
      localStorage.setItem("akb.treeVisible", "0");
      localStorage.setItem("akb.vaultRailCollapsed", "1");
    }
  }, { dark, folded });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  let browses = 0;
  const createdCollections: string[] = [];
  await page.route("**/api/v1/**", route => {
    const p = new URL(route.request().url()).pathname;
    let json: object = { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 };
    if (p.endsWith("/collections/fixture") && route.request().method() === "POST") {
      const { path } = route.request().postDataJSON() as { path: string };
      createdCollections.push(path);
      return route.fulfill({ json: { ok: true, created: true, collection: { path, name: path.split("/").at(-1), summary: null, doc_count: 0 } } });
    }
    if (p.endsWith("/auth/config")) json = { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
    else if (p.endsWith("/auth/me")) json = { user_id: "overview-user", username: "fixture", display_name: "Review user", email: "review@example.invalid", auth_method: "local", is_admin: false };
    else if (p.endsWith("/vaults")) json = { vaults: [{ id: "overview-vault", name: "fixture", role: "owner" }] };
    else if (p.endsWith("/info")) json = legacy ? { name: "fixture", role: "reader", file_count: 0 } : {
      name: "fixture", role: "owner", public_access: "none", owner_display_name: long ? "A very long owner name that must remain readable without clipping" : "Review owner",
      description,
      document_count: empty ? 1 : 36, collection_count: (empty ? 1 : 8) + createdCollections.length, table_count: empty ? 0 : 2, file_count: empty ? 0 : 7, member_count: 5,
      is_archived: false, is_external_git: false, tables: empty ? [] : [{ name: "service_catalog", row_count: 18 }, { name: "incidents" }],
    };
    else if (p.endsWith("/recent")) json = { changes: empty ? [] : ["Operations Runbook", "System Architecture", "API Gateway", "Indexing Pipeline", "Incident Response", "Vector Store"].map((title, i) => ({
      doc_id: `d-${i}`, title: long && i === 0 ? "An unusually long document title for shared operational ownership and incident response across multiple teams" : title,
      path: `guides/team/resource-${i}.md`, type: "note", changed_at: "2026-09-20T00:00:00Z", commit: "abc1234567", // pragma: allowlist secret — synthetic Git commit
    })) };
    else if (p.includes("/activity/")) json = { total: 1, activity: [{ hash: "abc1234567", subject: "Update operating guides", date: "2026-09-20T00:00:00Z", author_name: "Review owner" }] }; // pragma: allowlist secret — synthetic Git commit
    else if (p.includes("/browse/")) {
      browses++;
      const params = new URL(route.request().url()).searchParams;
      const collection = params.get("collection") || "";
      const depth = Number(params.get("depth") ?? -1);
      const kind = params.get("content_type") || "all";
      const query = params.get("name_query") || "";
      const allItems = [
        { type: "collection", path: "guides", name: "Guides", summary: "Runbooks and team practices" },
        { type: "collection", path: "platform", name: "Platform" },
        { type: "collection", path: "guides/operations", name: "Operations" },
        ...createdCollections.map(path => ({ type: "collection", path, name: path.split("/").at(-1) })),
        { type: "document", path: "guides/runbook.md", name: "Runbook" },
        { type: "table", path: "services", name: "services", collection: "guides" },
        { type: "file", path: "legacy-file-id", name: "diagram.svg", uri: "akb://fixture/file/7ead8d7b-2861-4d64-8190-4cab55372b91", collection: "guides" },
      ];
      const prefix = collection ? collection + "/" : "";
      const items = allItems.filter(item => {
        const path = item.type === "table" || item.type === "file" ? `${item.collection}/${item.name}` : item.path;
        return path.startsWith(prefix) && (depth < 0 || !path.slice(prefix.length).includes("/"))
          && (kind !== "collections" || item.type === "collection") && (kind !== "resources" || item.type !== "collection")
          && (!["documents", "tables", "files"].includes(kind) || `${item.type}s` === kind)
          && (!query || `${item.name} ${path}`.toLowerCase().includes(query.toLowerCase()));
      });
      json = { path: collection, complete: true, next_cursor: null, name_query: query || undefined, items,
        context: collection ? { type: "collection", path: collection, name: collection.split("/").at(-1) } : undefined };
    }
    else if (p.endsWith("/help/skill-template")) return route.fulfill({ contentType: "text/plain", body: "# {vault} Guide\n\nStarter template." });
    else if (p.includes("/documents/fixture/")) json = { title: "Fixture guide", path: "overview/vault-skill.md", content: empty ? "# fixture Guide\n\nStarter template." : "# Fixture guide\n\nUse this vault for architecture decisions, operational procedures, and service ownership.", created_at: "2026-08-20T00:00:00Z", updated_at: "2026-09-20T00:00:00Z" };
    return route.fulfill({ json });
  });
  return { browses: () => browses, createdCollections: () => [...createdCollections] };
}

async function expectNoOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const overflowing = await page.locator('.vault-overview, .vault-overview-summary, .vault-overview-actions, .vault-overview-links, .vault-content-tabs, .vault-content-tabs [role="tab"], .vault-overview-main, .vault-overview-context').evaluateAll(nodes => nodes.filter(node => node.scrollWidth > node.clientWidth + 1).map(node => node.className));
  expect(overflowing).toEqual([]);
}

for (const dark of [false, true]) for (const width of [375, 874, 1280, 1440, 1920, 2560]) {
  test(`Overview prioritizes content at ${width}px in ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, { dark });
    await page.goto('/vault/fixture');
    const overview = page.getByRole('region', { name: 'fixture Vault overview', exact: true });
    const summary = page.getByRole('region', { name: 'Vault summary', exact: true });
    const directory = page.getByRole('region', { name: 'Collection workspace', exact: true });
    const aside = page.getByRole('complementary', { name: 'Vault overview details' });
    await expect(directory.getByRole('link', { name: 'Open collection guides', exact: true })).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const viewport = page.locator('[data-slot="vault-route-viewport"]');
    const box = (await overview.boundingBox())!;
    const viewportBox = (await viewport.boundingBox())!;
    const viewportWidth = await viewport.evaluate(node => node.clientWidth);
    const gutter = width >= 1280 ? 32 : width >= 640 ? 24 : 16;
    const expectedWidth = Math.min(1280, viewportWidth - gutter * 2);
    expect(Math.abs(box.width - expectedWidth)).toBeLessThanOrEqual(1);
    expect(Math.abs(box.x - viewportBox.x - (viewportWidth - expectedWidth) / 2)).toBeLessThanOrEqual(1);
    expect(Math.abs(box.y - viewportBox.y - 20)).toBeLessThanOrEqual(1);
    await expect(summary.getByRole('button', { name: 'Copy akb://fixture' })).toHaveCount(0);
    await expect(summary.getByText('owner', { exact: true })).toHaveCount(0);
    await expect(summary.getByText('Review owner', { exact: true })).toHaveCount(1);
    await expect(directory.getByRole('link', { name: 'Members', exact: true })).toHaveText('Members5');
    await expect(summary.getByRole('button', { name: 'New content', exact: true })).toHaveText('New');
    const tabs = directory.getByRole('tablist', { name: 'Resource type' });
    await expect(tabs.getByRole('tab')).toHaveCount(4);
    await expect(tabs.getByRole('tab', { name: /^Documents/ })).toContainText('36');
    expect((await directory.getByRole('region', { name: 'Contents', exact: true }).boundingBox())!.width).toBeGreaterThanOrEqual(box.width - 2);
    await expect(aside.getByRole('region', { name: 'Recently updated', exact: true })).toBeVisible();
    await expect(aside.getByRole('region', { name: 'Version history', exact: true })).toBeVisible();
    const history = aside.getByRole('region', { name: 'Version history', exact: true });
    await expect(history.getByRole('link', { name: 'Full commit log', exact: true })).toHaveCount(0);
    await history.getByRole('button', { name: 'Show version history', exact: true }).click();
    await expect(history.getByRole('link', { name: 'Full commit log', exact: true })).toBeVisible();
    await expectNoOverflow(page);
    await history.getByRole('button', { name: 'Hide version history', exact: true }).click();
    await viewport.evaluate(node => { node.scrollTop = 0; });
    const newButton = (await summary.getByRole('button', { name: 'New content', exact: true }).boundingBox())!;
    const title = (await summary.getByRole('heading', { level: 1 }).boundingBox())!;
    expect(newButton.y).toBeLessThan(title.y + title.height);
    expect(newButton.x).toBeGreaterThan(title.x);
    const bar = directory.getByRole('region', { name: 'Contents', exact: true });
    const pages = bar.getByRole('navigation', { name: 'Vault pages', exact: true });
    const navigationBox = (await pages.boundingBox())!;
    const barBox = (await bar.boundingBox())!;
    const tabBox = (await tabs.boundingBox())!;
    expect(navigationBox.y).toBeGreaterThanOrEqual(barBox.y);
    expect(navigationBox.y + navigationBox.height).toBeLessThanOrEqual(barBox.y + barBox.height);
    if (box.width >= 780) {
      expect(navigationBox.x).toBeGreaterThanOrEqual(tabBox.x + tabBox.width + 24);
      expect(Math.abs(navigationBox.y + navigationBox.height / 2 - tabBox.y - tabBox.height / 2)).toBeLessThanOrEqual(2);
    } else if (box.width <= 640) {
      expect(navigationBox.y).toBeGreaterThanOrEqual(tabBox.y + tabBox.height);
    }
    await expect(pages.getByRole('group', { name: 'Knowledge views', exact: true })).toBeVisible();
    await expect(pages.getByRole('group', { name: 'Vault administration', exact: true })).toBeVisible();
    await expect(summary.getByRole('navigation', { name: 'Vault pages', exact: true })).toHaveCount(0);
    const ownerGuide = summary.locator('.vault-owner-guide');
    await expect(ownerGuide).toContainText('Review owner');
    await expect(ownerGuide.getByRole('link', { name: 'Open vault guide', exact: true })).toBeVisible();

    const mainBox = (await page.locator('.vault-overview-main').boundingBox())!;
    const asideBox = (await aside.boundingBox())!;
    if (box.width >= 1040) {
      expect(asideBox.x).toBeGreaterThanOrEqual(mainBox.x + mainBox.width + 20);
      expect(Math.abs(asideBox.y - mainBox.y)).toBeLessThanOrEqual(1);
    } else expect(asideBox.y).toBeGreaterThanOrEqual(mainBox.y + mainBox.height);
    const searchBox = (await directory.getByRole('textbox', { name: 'Search collections and content' }).boundingBox())!;
    expect(searchBox.width).toBeGreaterThanOrEqual(mainBox.width * .65);
    await expectNoOverflow(page);
    await page.screenshot({ path: testInfo.outputPath('vault-overview.png'), animations: 'disabled' });
    await expect(summary.getByRole('button', { name: 'Manage', exact: true })).toHaveCount(0);
    for (const [label, path] of [['Graph','graph'],['Publications','publications'],['Members','members'],['Settings','settings']]) {
      const link = pages.getByRole('link', { name: label, exact: true });
      await expect(link).toBeVisible();
      await expect(link).toHaveAttribute('href', `/vault/fixture/${path}`);
      await link.focus();
      await expect(link).toBeFocused();
    }
  });
}

for (const width of [874, 1280]) test(`Overview description uses available header width at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  await fixture(page, { description: 'A shared knowledge space for architecture, engineering decisions, operating procedures, and the context that helps our team work together.' });
  await page.goto('/vault/fixture');
  const description = page.locator('.vault-overview-description');
  await expect(description).toBeVisible();
  const summary = (await page.locator('.vault-overview-summary').boundingBox())!;
  const paragraph = (await description.boundingBox())!;
  expect(paragraph.width).toBeGreaterThan(summary.width * .7);
  await expectNoOverflow(page);
});

test('Overview redistributes columns across viewport changes and a folded navigator', async ({ page }) => {
  await fixture(page);
  await page.goto('/vault/fixture');
  for (const [width,height] of [[1920,1000],[874,1000],[1100,500],[2560,1000]]) {
    await page.setViewportSize({ width, height });
    await expectNoOverflow(page);
  }
  await page.evaluate(() => localStorage.setItem('akb.treeVisible', '0'));
  await page.reload();
  await expect(page.getByRole('textbox', { name: 'Search collections and content' })).toBeVisible();
  await expectNoOverflow(page);
});

test('Overview creation keeps collection defaults and updates the authoritative tab count', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const scenario = await fixture(page);
  await page.goto('/vault/fixture?collection=guides');
  await page.getByRole('button', { name: 'New content', exact: true }).click();
  await page.getByRole('menuitem', { name: 'New collection', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New collection', exact: true });
  await expect(dialog.getByLabel('Path', { exact: true })).toHaveValue('guides/');
  await dialog.getByLabel('Path', { exact: true }).fill('guides/new-section');
  await dialog.getByRole('button', { name: 'Create', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(scenario.createdCollections()).toEqual(['guides/new-section']);
  await expect(page.getByRole('tab', { name: /^Collections/ })).toContainText('9');
});

test('Empty Vault retains all setup entry points while unavailable totals remain distinct from zero', async ({ page }) => {
  await fixture(page, { empty: true });
  await page.goto('/vault/fixture');
  await expect(page.getByRole('region', { name: 'Set up this Vault', exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: /^Documents/ })).toContainText('1');
  await expect(page.getByRole('tab', { name: /^Files/ })).toContainText('0');
  await expect(page.getByRole('complementary', { name: 'Vault overview details' })).toHaveCount(0);
  await expectNoOverflow(page);
});

test('Legacy counts do not invent empty onboarding or zero totals', async ({ page }) => {
  await fixture(page, { legacy: true });
  await page.goto('/vault/fixture');
  await expect(page.getByRole('tab', { name: /^Documents/ })).toContainText('—');
  await expect(page.getByRole('tab', { name: /^Files/ })).toContainText('0');
  await expect(page.getByRole('region', { name: 'Set up this Vault', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'New content', exact: true })).toHaveCount(0);
});

test('Long ownership and enlarged text remain readable with reduced motion', async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 375, height: 900 });
  await fixture(page, { long: true });
  await page.goto('/vault/fixture');
  const owner = page.getByText('A very long owner name that must remain readable without clipping', { exact: true });
  await expect(owner).toBeVisible();
  expect(await owner.evaluate(node => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
  await page.addStyleTag({ content: 'html { font-size: 20px; }' });
  await expectNoOverflow(page);
  await page.screenshot({ path: testInfo.outputPath('vault-long.png'), animations: 'disabled' });
});

for (const dark of [false,true]) test(`Metadata and keyboard navigation stay accessible in ${dark ? 'dark' : 'light'}`, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page, { dark });
  await page.goto('/vault/fixture');
  const access = page.getByRole('button', { name: 'Vault access: Private', exact: true });
  await access.focus(); await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Vault access', exact: true });
  await expect(dialog).toContainText('Only people with access can open this vault.');
  await page.keyboard.press('Escape'); await expect(access).toBeFocused();
  const collections = page.getByRole('tab', { name: /^Collections/ });
  await collections.focus(); await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: /^Documents/ })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/type=documents$/);
  await page.evaluate(axe.source);
  const results = await page.evaluate(async () => (await (window as any).axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a','wcag2aa','wcag21aa','wcag22aa'] } })).violations);
  expect(results.map((v: { id: string }) => v.id)).toEqual([]);
});

test('Guide opens in the shared modal and returns to the selected Collection', async ({ page }) => {
  await fixture(page); await page.goto('/vault/fixture?collection=guides');
  const guide = page.getByRole('link', { name: 'Open vault guide', exact: true });
  await guide.click();
  const preview = page.getByRole('dialog', { name: 'Document preview', exact: true });
  await expect(preview).toContainText('Use this vault for architecture decisions');
  await preview.getByRole('button', { name: 'Close dialog', exact: true }).click();
  await expect(page).toHaveURL(/collection=guides$/); await expect(guide).toBeFocused();
});

for (const width of [375,1440]) test(`Collection navigation and resource tabs remain server scoped at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 }); await fixture(page);
  const requests: URL[] = []; page.on('request', request => { if(request.url().includes('/browse/')) requests.push(new URL(request.url())); });
  await page.goto('/vault/fixture');
  const workspace = page.getByRole('region', { name: 'Collection workspace', exact: true });
  await workspace.getByRole('link', { name: 'Open collection guides', exact: true }).click();
  await expect(workspace.getByRole('link', { name: 'Runbook document', exact: true })).toHaveAttribute('href','/vault/fixture/doc/guides%2Frunbook.md');
  await workspace.getByRole('textbox', { name: 'Search collections and content' }).fill('Platform');
  await expect(workspace.getByRole('link', { name: 'Open collection platform', exact: true })).toBeVisible();
  expect(requests.some(url=>url.searchParams.get('name_query')==='Platform'&&!url.searchParams.has('collection')&&url.searchParams.get('depth')==='-1')).toBe(true);
  await workspace.getByRole('button', { name: 'Clear search', exact: true }).click();
  await workspace.getByRole('link', { name: 'All collections', exact: true }).click();
  await page.getByRole('tab', { name: /^Tables/ }).click();
  await expect(page).toHaveURL(/type=tables$/);
  await expect(workspace.getByRole('link', { name: 'services table', exact: true })).toBeVisible();
  await expect(workspace.getByRole('link', { name: /Runbook document/ })).toHaveCount(0);
  expect(requests.some(url=>url.searchParams.get('content_type')==='tables'&&url.searchParams.get('page_size')==='100'&&url.searchParams.get('depth')==='-1')).toBe(true);
  await page.goBack();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  await page.getByRole('button', { name: 'List view', exact: true }).click();
  await expect(workspace.locator('.vault-collection-list')).toBeVisible();
  await page.reload();
  await expect(workspace.locator('.vault-collection-list')).toBeVisible();
});
