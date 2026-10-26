import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses; never writes user data.");

async function fixture(page: Page, dark: boolean) {
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "bounded-navigation-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    localStorage.setItem("akb.treeVisible", "1");
  }, { dark });
  const requests: URL[] = [];
  let attempts = 0;
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "paging-reader", username: "reader", display_name: "Reader", email: "reader@example.invalid", auth_method: "local", is_admin: false } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "alpha", name: "alpha", role: "reader" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: { name: "alpha", role: "reader", document_count: 101, collection_count: 101, is_archived: false, is_external_git: false } });
    if (path.includes("/browse/")) {
      requests.push(url);
      const kind = url.searchParams.get("content_type") || "all";
      const query = url.searchParams.get("name_query");
      const cursor = url.searchParams.get("cursor");
      const base = { vault: "alpha", path: "", archive_scope: "unarchived", name_query: query, scanned: 100 };
      if (query) {
        if (cursor) {
          await new Promise(resolve => setTimeout(resolve, 250));
          return route.fulfill({ json: { ...base, complete: true, next_cursor: null, items: [{ type: "document", name: "Unloaded needle title", path: "unrelated-filename.md" }] } });
        }
        return route.fulfill({ json: { ...base, complete: false, next_cursor: "search-next", items: [] } });
      }
      if (cursor) {
        if (kind === "all" && ++attempts === 1) return route.fulfill({ status: 503, json: { error: "Read temporarily unavailable", code: "service_unavailable" } });
        return route.fulfill({ json: { ...base, complete: true, next_cursor: null, items: [kind === "collections" ? { type: "collection", name: "Last collection", path: "z-last" } : { type: "document", name: "Last document", path: "z-last.md" }] } });
      }
      return route.fulfill({ json: { ...base, complete: false, next_cursor: `${kind}-next`, items: Array.from({ length: 100 }, (_, index) => kind === "collections" ? { type: "collection", name: `Collection ${index}`, path: `collection-${String(index).padStart(3, "0")}` } : { type: "document", name: `Document ${String(index).padStart(3, "0")}`, path: `doc-${String(index).padStart(3, "0")}.md` }) } });
    }
    if (path.includes("/documents/") && !path.endsWith("/history")) return route.fulfill({ json: { title: "Document 000", path: "doc-000.md", content: "# Document", status: "active" } });
    return route.fulfill({ json: { items: [], history: [], relations: [], subscribed: false, unread_count: 0 } });
  });
  return { requests, get attempts() { return attempts; } };
}

for (const width of [375, 1440]) for (const dark of [false, true]) {
  test(`scroll paging preserves rows and recovers at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const state = await fixture(page, dark);
    await page.goto("/vault/alpha/doc/doc-000.md");
    if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
    const explorer = page.getByRole("navigation", { name: "alpha explorer", exact: true });
    const rows = explorer.getByRole("link", { name: /^Document:/ });
    await expect(rows).toHaveCount(100);
    expect(state.requests.some(url => url.searchParams.has("cursor"))).toBe(false);
    await rows.last().scrollIntoViewIfNeeded();
    const retry = explorer.getByRole("button", { name: "Retry: load more content", exact: true });
    await expect(retry).toBeVisible();
    await expect(rows).toHaveCount(100);
    await page.waitForTimeout(300);
    expect(state.attempts).toBe(1);
    await retry.focus();
    await page.keyboard.press("Enter");
    await expect(explorer.getByRole("link", { name: "Document: Last document", exact: true })).toBeVisible();
    await expect(rows).toHaveCount(101);
    await expect(page.getByText(/Only part of this scope is loaded/)).toHaveCount(0);
    expect(state.requests.every(url => url.searchParams.get("page_size") === "100")).toBe(true);
    await page.getByRole("searchbox", { name: "Filter resources" }).fill("needle");
    await expect.poll(() => state.requests.some(url => url.searchParams.get("cursor") === "search-next")).toBe(true);
    await expect(explorer.getByText("No resources match these filters.")).toHaveCount(0);
    await expect(explorer.getByRole("link", { name: "Document: Unloaded needle title", exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("automatic-paging-search.png") });
  });
}

test("main Collections and content advance independently on scroll", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixture(page, false);
  await page.goto("/vault/alpha");
  const workspace = page.getByRole("region", { name: "Collection workspace", exact: true });
  const collections = workspace.getByRole("list", { name: "Collections", exact: true });
  for (let step = 0; step < 6; step++) {
    await collections.getByRole("link").last().scrollIntoViewIfNeeded();
    if (state.requests.some(url => url.searchParams.get("cursor") === "collections-next")) break;
    await page.waitForTimeout(100);
  }
  await expect(collections.getByRole("link", { name: "Open collection z-last", exact: true })).toBeAttached();
  const resources = workspace.getByRole("region", { name: "Collection resources", exact: true });
  for (let step = 0; step < 6; step++) {
    await resources.getByRole("link").last().scrollIntoViewIfNeeded();
    if (state.requests.some(url => url.searchParams.get("cursor") === "resources-next")) break;
    await page.waitForTimeout(100);
  }
  await expect(resources.getByRole("link", { name: /Last document/ })).toBeAttached();
  expect(state.requests.some(url => url.searchParams.get("cursor") === "collections-next")).toBe(true);
  expect(state.requests.some(url => url.searchParams.get("cursor") === "resources-next")).toBe(true);
});

test("a README beside closed Collections does not drain hidden document pages", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await fixture(page, false);
  const cursors: (string | null)[] = [];
  await page.route("**/api/v1/browse/alpha?**", route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("content_type") !== "all" || url.searchParams.get("name_query")) return route.fallback();
    const cursor = url.searchParams.get("cursor");
    cursors.push(cursor);
    const collection = (index: number) => ({ type: "collection", name: `Collection ${index}`, path: `collection-${String(index).padStart(3, "0")}` });
    const response = { vault: "alpha", path: "", archive_scope: "unarchived", complete: false };
    if (!cursor) return route.fulfill({ json: { ...response, next_cursor: "catalog-tail", items: Array.from({ length: 100 }, (_, index) => collection(index)) } });
    if (cursor === "catalog-tail") return route.fulfill({ json: { ...response, next_cursor: "documents-first", items: Array.from({ length: 5 }, (_, index) => collection(index + 100)) } });
    if (cursor === "documents-first") return route.fulfill({ json: { ...response, next_cursor: "documents-middle", items: [
      { type: "document", name: "README", path: "README.md" },
      ...Array.from({ length: 5 }, (_, index) => ({ type: "document", name: `Hidden document ${index}`, path: `collection-000/doc-${String(index).padStart(3, "0")}.md` })),
    ] } });
    if (cursor === "documents-middle") return route.fulfill({ json: { ...response, next_cursor: "documents-tail", items: [{ type: "document", name: "Hidden document 5", path: "collection-000/doc-005.md" }] } });
    return route.fulfill({ json: { ...response, complete: true, next_cursor: null, items: [{ type: "document", name: "Last hidden document", path: "collection-000/z-last.md" }] } });
  });
  await page.goto("/vault/alpha");
  const explorer = page.getByRole("navigation", { name: "alpha explorer", exact: true });
  await explorer.getByRole("button", { name: "collection-099", exact: true }).scrollIntoViewIfNeeded();
  await expect(explorer.getByRole("button", { name: "collection-104", exact: true })).toBeAttached();
  await explorer.getByRole("button", { name: "collection-104", exact: true }).scrollIntoViewIfNeeded();
  await expect.poll(() => cursors.filter(Boolean)).toEqual(["catalog-tail", "documents-first", "documents-middle"]);
  await page.waitForTimeout(400);
  expect(cursors.filter(Boolean)).toEqual(["catalog-tail", "documents-first", "documents-middle"]);
  await expect(explorer.getByRole("link", { name: "Document: README", exact: true })).toBeAttached();
  await expect(explorer.getByRole("button", { name: "Load more content", exact: true })).toBeEnabled();
  await explorer.getByRole("button", { name: "collection-000", exact: true }).click();
  await expect(explorer.getByRole("link", { name: "Document: Hidden document 5", exact: true })).toBeAttached();
  await explorer.getByRole("button", { name: "collection-104", exact: true }).scrollIntoViewIfNeeded();
  await expect(explorer.getByRole("link", { name: "Document: Last hidden document", exact: true })).toBeAttached();
  expect(cursors.filter(Boolean)).toEqual(["catalog-tail", "documents-first", "documents-middle", "documents-tail"]);
});
