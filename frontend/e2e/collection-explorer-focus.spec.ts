import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures; no user data.");

const segments = ["팀 자료 & 운영", "platform", "architecture", "decisions", "release #1", "implementation"];
const paths = segments.map((_segment, index) => segments.slice(0, index + 1).join("/"));
const root = paths[4];
const deepest = paths[5];
const title = "00 A long document title preserving context across six nested collections";
const fileTitle = "A long deployment checklist filename retaining all its original context.txt";
const documentPath = `${deepest}/current.md`;
const documentUrl = `/vault/fixture/doc/${encodeURIComponent(documentPath)}`;
const siblingPath = `${paths[3]}/other-branch`;
const outsidePath = "outside/elsewhere.md";
const fileId = "7ead8d7b-2861-4d64-8190-4cab55372b91";

async function fixture(page: Page, { dark = false, reader = false } = {}) {
  let removed = false;
  let denied = false;
  let reads = 0;
  let nextRead: Promise<void> | undefined;
  const role = reader ? "reader" : "owner";
  await page.addInitScript(dark => {
    localStorage.setItem("akb_token", "collection-focus-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    localStorage.setItem("akb.treeVisible", "1");
    localStorage.setItem("akb.focusedNavigationWidth.v1", "224");
  }, dark);
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let json: object = { items: [], history: [], changes: [], relations: [], tokens: [], templates: [], subscribed: false, unread_count: 0 };
    if (path.endsWith("/auth/config")) json = { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
    else if (path.endsWith("/auth/me")) json = { user_id: "focus-user", username: "fixture", display_name: "Focus reviewer", email: "focus@example.invalid", auth_method: "local", key_class: null, is_admin: false };
    else if (path.endsWith("/vaults")) json = { vaults: [{ id: "fixture", name: "fixture", role }] };
    else if (path.endsWith("/info")) json = { name: "fixture", role, description: "Focused exploration fixture", public_access: "none", document_count: 77, collection_count: 8, file_count: 1, table_count: 1, is_archived: false, is_external_git: false };
    else if (path.includes("/browse/")) {
      reads++;
      const pending = nextRead; nextRead = undefined;
      if (pending) await pending;
      if (denied) return route.fulfill({ status: 403, json: { detail: "Forbidden" } });
      const nameQuery = url.searchParams.get("name_query") || "";
      const items = [
        ...paths.filter(candidate => !removed || !candidate.startsWith(root)).map(candidate => ({ type: "collection", path: candidate, name: candidate.split("/").at(-1) })),
        { type: "collection", path: siblingPath, name: "other-branch" },
        { type: "collection", path: "outside", name: "outside" },
        { type: "document", path: outsidePath, name: "Outside document" },
        { type: "document", path: `${siblingPath}/sibling.md`, name: "Sibling document" },
        ...(!removed ? [
          { type: "document", path: documentPath, name: title },
          ...Array.from({ length: 74 }, (_, index) => ({ type: "document", path: `${deepest}/note-${index}.md`, name: `Note ${String(index).padStart(2, "0")}` })),
          { type: "file", path: fileId, collection: deepest, name: fileTitle, uri: `akb://fixture/file/${fileId}` },
          { type: "table", path: "services", collection: deepest, name: "services" },
        ] : []),
      ];
      json = { archive_scope: "unarchived", complete: true, next_cursor: null, name_query: nameQuery || null,
        items: items.filter(item => !nameQuery || `${item.name} ${item.path}`.toLowerCase().includes(nameQuery.toLowerCase())) };
    } else if (decodeURIComponent(path).endsWith("/documents/fixture/overview/vault-skill.md")) return route.fulfill({ status: 404, json: { detail: "No guide" } });
    else if (path.includes("/documents/fixture/") && !path.endsWith("/history")) {
      const selected = decodeURIComponent(path).split("/documents/fixture/")[1];
      json = { uri: `akb://fixture/coll/${deepest}/doc/current.md`, path: selected, title: selected === documentPath ? title : "Another document", summary: "Original summary", content: `# Reading context\n\n[Sibling document](/vault/fixture/doc/${encodeURIComponent(`${siblingPath}/sibling.md`)})\n\n[Outside document](/vault/fixture/doc/${encodeURIComponent(outsidePath)})`, status: "active", current_commit: "aaaaaaaaaaaaa", updated_at: "2026-09-01T00:00:00Z" };
    }
    return route.fulfill({ json });
  });
  return {
    get reads() { return reads; },
    removeRoot() { removed = true; },
    deny() { denied = true; },
    holdNextRead() { let release!: () => void; nextRead = new Promise<void>(resolve => { release = resolve; }); return release; },
  };
}

async function reveal(page: Page) {
  const opener = page.getByRole("button", { name: "Open vault navigation", exact: true });
  if (await opener.isVisible()) await opener.click();
  await expect(page.getByRole("navigation", { name: "fixture explorer", exact: true })).toBeVisible();
}

async function explore(page: Page, path: string) {
  const trigger = page.getByRole("button", { name: `Collection actions for ${path}`, exact: true });
  await trigger.locator("..").hover();
  await trigger.click();
  await page.getByRole("menuitem", { name: "Explore from here", exact: true }).click();
}

for (const width of [375, 1440]) for (const dark of [false, true]) {
  test(`Collection exploration keeps the draft and frees indentation at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await fixture(page, { dark });
    await page.goto(documentUrl);
    const close = page.getByRole("button", { name: "Close vault navigation", exact: true });
    if (width < 1024 && await close.isVisible()) await close.click();
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await page.getByRole("textbox", { name: "Summary", exact: true }).fill("Keep this unsaved summary while exploring");
    const editingUrl = page.url();
    await reveal(page);
    const explorer = page.getByRole("navigation", { name: "fixture explorer", exact: true });
    const child = explorer.locator(`[data-sig="collection:${deepest}"]`);
    const before = await child.evaluate(node => parseFloat(getComputedStyle(node).paddingLeft));
    await explore(page, root);
    const navigation = page.getByRole("navigation", { name: "Collection exploration", exact: true });
    const parent = navigation.getByRole("button", { name: `Back to collection ${paths[3]}`, exact: true });
    await expect(parent).toBeFocused();
    await expect(page).toHaveURL(editingUrl);
    await expect(page.getByRole("dialog", { name: /Leave document/ })).toHaveCount(0);
    await expect(explorer.locator(`[data-sig="collection:${root}"]`)).toHaveCount(0);
    const after = await child.evaluate(node => parseFloat(getComputedStyle(node).paddingLeft));
    expect(after).toBeLessThanOrEqual(12);
    expect(before - after).toBeGreaterThanOrEqual(48);
    await expect(explorer.getByRole("link", { name: `Document: ${title}`, exact: true })).toBeVisible();
    const sidebar = page.locator("#vault-workspace-navigation");
    if (width === 1440) expect((await sidebar.boundingBox())!.width).toBe(224);
    expect(await sidebar.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("focused-collection-draft.png"), animations: "disabled" });
    await parent.press("Enter");
    await expect(explorer.locator(`[data-sig="collection:${root}"]`)).toBeVisible();
    const path = navigation.getByRole("button", { name: "Collection exploration path", exact: true });
    await path.focus();
    await page.keyboard.press("Enter");
    const all = page.getByRole("menuitem", { name: "All collections", exact: true });
    await all.focus();
    await page.keyboard.press("Enter");
    await expect(explorer.locator('[data-sig="collection:outside"]')).toBeVisible();
    await expect(page).toHaveURL(editingUrl);
    if (width < 1024) await close.click();
    await expect(page.getByRole("textbox", { name: "Summary", exact: true })).toHaveValue("Keep this unsaved summary while exploring");
  });
}

test("Readers compose exploration with filters and automatically paged resources", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page, { reader: true });
  await page.goto(documentUrl);
  await reveal(page);
  await explore(page, deepest);
  const explorer = page.getByRole("navigation", { name: "fixture explorer", exact: true });
  const navigation = page.getByRole("navigation", { name: "Collection exploration", exact: true });
  await expect(navigation.getByRole("button", { name: `Back to collection ${root}`, exact: true })).toBeFocused();
  await expect(explorer.getByRole("link", { name: /^Document:/ })).toHaveCount(75);
  await expect(explorer.getByRole("button", { name: /^Show 50 more documents/ })).toHaveCount(0);
  const files = explorer.getByRole("button", { name: "Files, 1 item", exact: true });
  if (await files.getAttribute("aria-expanded") === "false") await files.click();
  await expect(explorer.getByRole("link", { name: `File: ${fileTitle}`, exact: true })).toBeVisible();
  const filter = page.getByRole("searchbox", { name: "Filter resources", exact: true });
  await filter.fill("checklist");
  await expect(explorer.getByRole("link", { name: /^Document:/ })).toHaveCount(0);
  await expect(explorer.getByRole("link", { name: `File: ${fileTitle}`, exact: true })).toBeVisible();
  await page.getByRole("button", { name: /^Filter content/ }).click();
  await page.getByRole("button", { name: "Reset filters", exact: true }).click();
  await expect(filter).toHaveValue("");
  await page.getByRole("button", { name: "Collection search scope", exact: true }).click();
  const picker = page.getByRole("combobox", { name: "Find a collection", exact: true });
  await picker.fill("outside");
  await expect(page.getByRole("option", { name: "outside", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(navigation.getByRole("button", { name: "Collection exploration path", exact: true })).toContainText(segments[5]);
  await expect(explorer.locator('[data-sig="collection:outside"]')).toHaveCount(0);
  await expect(page).toHaveURL(documentUrl);
});

for (const failure of ["removed", "denied"] as const) test(`Refreshing a ${failure} exploration root never broadens the sidebar`, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const control = await fixture(page);
  await page.goto(documentUrl);
  await reveal(page);
  await explore(page, root);
  const explorer = page.getByRole("navigation", { name: "fixture explorer", exact: true });
  const search = page.getByRole("searchbox", { name: "Filter resources", exact: true });
  const refresh = page.getByRole("button", { name: "Refresh content", exact: true });
  const initial = control.reads;
  const release = control.holdNextRead();
  await refresh.click();
  await search.focus();
  release();
  await expect.poll(() => control.reads).toBeGreaterThan(initial);
  await expect(refresh).toBeEnabled();
  await expect(search).toBeFocused();
  if (failure === "removed") control.removeRoot(); else control.deny();
  await refresh.click();
  await expect(refresh).toBeEnabled();
  await expect(explorer.locator('[data-sig="collection:outside"]')).toHaveCount(0);
  await expect(explorer.getByRole("link", { name: `Document: ${title}`, exact: true })).toHaveCount(0);
  await expect(page).toHaveURL(documentUrl);
  await expect(page.getByRole("navigation", { name: "Collection exploration", exact: true })).toBeVisible();
});


test("Resource navigation retains the focus inside it and lifts only for a new outside destination", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page, { reader: true });
  await page.addInitScript(({ siblingPath, outsidePath }) => {
    localStorage.setItem("akb.workspaceShortcuts.v1:focus-user", JSON.stringify([
      { kind: "document", vault: "fixture", path: `${siblingPath}/sibling.md`, title: "Sibling document" },
      { kind: "document", vault: "fixture", path: outsidePath, title: "Outside document" },
    ]));
  }, { siblingPath, outsidePath });
  await page.goto(documentUrl);
  await reveal(page);
  await explore(page, deepest);
  const explorer = page.getByRole("navigation", { name: "fixture explorer", exact: true });
  const navigation = page.getByRole("navigation", { name: "Collection exploration", exact: true });
  await explorer.getByRole("link", { name: "Document: Note 00", exact: true }).click();
  await expect(page).toHaveURL(`/vault/fixture/doc/${encodeURIComponent(`${deepest}/note-0.md`)}`);
  await expect(navigation.getByRole("button", { name: "Collection exploration path", exact: true })).toContainText(segments[5]);
  const sidebar = page.locator("#vault-workspace-navigation");
  await sidebar.locator("summary").filter({ hasText: "Pinned" }).click();
  const pinned = sidebar.getByRole("navigation", { name: "Pinned", exact: true });
  await pinned.getByRole("link", { name: "Sibling document", exact: true }).click();
  await expect(page).toHaveURL(`/vault/fixture/doc/${encodeURIComponent(`${siblingPath}/sibling.md`)}`);
  await expect(navigation.getByRole("button", { name: "Collection exploration path", exact: true })).toContainText(segments[3]);
  await pinned.getByRole("link", { name: "Outside document", exact: true }).click();
  await expect(page).toHaveURL(`/vault/fixture/doc/${encodeURIComponent(outsidePath)}`);
  await expect(explorer.locator('[data-sig="collection:outside"]')).toBeVisible();
  await expect(navigation.getByRole("button", { name: /^Back to collection / })).toHaveCount(0);
});

test("The sidebar create control uses its exploration root as the new collection parent", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto(documentUrl);
  await reveal(page);
  await explore(page, root);
  await page.locator('[data-slot="collection-management-row"]').getByRole("button", { name: "Add content", exact: true }).click();
  await page.getByRole("menuitem", { name: "New collection", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New collection", exact: true });
  await expect(dialog.getByRole("textbox", { name: "Path", exact: true })).toHaveValue(`${root}/`);
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page).toHaveURL(documentUrl);
});
