import { test, expect, type Page } from "@playwright/test";

// These scenarios intercept HTTP; the mock app is instead owned by MSW.
test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");

async function fixture(page: Page, options: { infoGate?: Promise<void>; templatesGate?: Promise<void>; activityError?: boolean } = {}) {
  const state = { summaryError: false };
  await page.addInitScript(() => localStorage.setItem('akb_token', 'isolated-loading-audit'));
  await page.route('**/health/**', r => r.fulfill({ json: {} }));
  await page.route('**/api/v1/**', async r => {
    const p = new URL(r.request().url()).pathname;
    let json: unknown = { items: [], history: [], relations: [], vaults: [], changes: [], subscribed: false, unread_count: 0 };
    if (p.endsWith('/auth/config')) json = { schema_version: 2, auth_mode: 'local', local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
    else if (p.endsWith('/auth/me')) json = { user_id: 'loading-audit', username: 'fixture', display_name: 'Audit fixture', email: 'audit@example.invalid', auth_method: 'local', is_admin: false };
    else if (p.endsWith('/my/workspace-summary')) {
      if (state.summaryError) return r.fulfill({ status: 503, json: { detail: 'Fixture outage' } });
      json = { version: 1, scope: 'accessible', observed_at: new Date().toISOString(), vault_count: 1, document_count: 36, table_count: 2, file_count: 7 };
    } else if (p.endsWith('/vaults/templates')) {
      if (options.templatesGate) await options.templatesGate;
      json = [{ name: 'team', description: 'Team starter' }];
    } else if (p.endsWith('/vaults')) json = { vaults: [{ id: 'fixture-vault', name: 'fixture', role: 'owner' }] };
    else if (p.endsWith('/info')) {
      if (options.infoGate) await options.infoGate;
      json = { name: 'fixture', role: 'owner', public_access: 'none', description: 'Fixture vault', document_count: 36, table_count: 2, file_count: 7, member_count: 1, collection_count: 1, is_archived: false, is_external_git: false };
    } else if (p.includes('/activity/')) {
      if (options.activityError) return r.fulfill({ status: 503, json: { detail: 'Fixture outage' } });
      json = { total: 0, activity: [] };
    } else if (p.includes('/documents/fixture/')) json = { title: 'Guide', path: 'overview/vault-skill.md', content: '# Guide\n\nFixture content.' };
    else if (p.includes('/browse/')) json = { items: [] };
    else if (p.endsWith('/help/skill-template')) return r.fulfill({ contentType: 'text/plain', body: '# {vault} Guide' });
    return r.fulfill({ json });
  });
  return state;
}

for (const width of [375, 1440]) for (const dark of [false, true]) {
  test(`loading feedback preserves content at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(dark => { localStorage.setItem("akb_theme", dark ? "dark" : "light"); }, dark);
    let releaseInfo!: () => void;
    await fixture(page, { infoGate: new Promise<void>(resolve => { releaseInfo = resolve; }) });
    await page.goto("/vault/fixture");
    const loading = page.getByRole("status", { name: "Loading vault overview…", exact: true });
    await expect(loading).toBeVisible();
    expect((await loading.boundingBox())!.height).toBeGreaterThan(200);
    await page.screenshot({ path: testInfo.outputPath("overview-pending.png") });
    releaseInfo();
    await expect(page.getByRole("region", { name: "fixture Vault overview", exact: true })).toBeVisible();
    await page.unrouteAll({ behavior: "wait" });

    const state = await fixture(page);
    await page.goto("/");
    const summary = page.getByRole("region", { name: "Workspace summary" });
    await expect(summary).toContainText("36");
    state.summaryError = true;
    await page.evaluate(() => window.dispatchEvent(new CustomEvent("akb:inventory-changed", { detail: { vault: "fixture" } })));
    await expect(summary).toContainText("previous totals shown");
    await expect(summary).toContainText("36");
    await expect(summary).toHaveAttribute("aria-busy", "false");
    await page.screenshot({ path: testInfo.outputPath("home-refresh-error.png") });
    await page.unrouteAll({ behavior: "wait" });

    await fixture(page, { activityError: true });
    await page.goto("/vault/fixture/activity");
    await expect(page.getByRole("heading", { name: "Failed to load" })).toBeVisible();
    await expect(page.locator('#main [aria-busy="true"]')).toHaveCount(0);
    await page.unrouteAll({ behavior: "wait" });

    let releaseTemplates!: () => void;
    await fixture(page, { templatesGate: new Promise<void>(resolve => { releaseTemplates = resolve; }) });
    await page.goto("/vault/new");
    await expect(page.getByText("Loading optional templates; you can still create an empty vault")).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("templates-pending.png") });
    releaseTemplates();
    await expect(page.getByText("Loading optional templates; you can still create an empty vault")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}


test("graph counts stay unknown until a successful response", async ({ page }) => {
  await fixture(page);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/v1/graph/overview?**", async route => {
    await pending;
    await route.fulfill({ status: 503, json: { detail: "Fixture outage" } });
  });
  await page.goto("/vault/fixture/graph");
  await expect(page.getByText("Checking graph counts…", { exact: true })).toBeVisible();
  await expect(page.getByText("0 resources · 0 relationships", { exact: true })).toHaveCount(0);
  release();
  await expect(page.getByText("Graph counts unavailable", { exact: true })).toBeVisible();
  await expect(page.getByText("0 resources · 0 relationships", { exact: true })).toHaveCount(0);
});


for (const slow of [false, true]) test(`document entry avoids a second loading phase (${slow ? "slow" : "fast"})`, async ({ page }) => {
  await fixture(page);
  let reads = 0;
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route(url => url.pathname === "/api/v1/documents/fixture/guide.md", async route => {
    reads++;
    if (slow) await pending;
    await route.fulfill({ json: { path: "guide.md", title: "Loading feedback guide", content: "# Reader ready\n\nStable document body.", current_commit: "fixture-revision" } });
  });
  await page.goto("/vault/fixture/doc/guide.md");
  if (slow) {
    await expect(page.getByRole("status", { name: "Loading document", exact: true })).toBeVisible();
    release();
  }
  await expect(page.getByText("Stable document body.", { exact: true })).toBeVisible();
  await page.waitForLoadState("networkidle");
  // Vite dev runs StrictMode's initial unsubscribe/remount. Consuming the
  // query's abort signal cancels that first query, so dev makes two initial
  // HTTP attempts; a production build makes one. Neither may refetch again
  // when the document body mounts after the page has received its response.
  const development = await page.locator('script[src$="/@vite/client"]').count() > 0;
  expect(reads).toBe(development ? 2 : 1);
  await expect(page.getByText("Updating document", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("status", { name: "Loading document", exact: true })).toHaveCount(0);
});
