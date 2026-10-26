import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");

async function fixture(page: Page, dark: boolean) {
  let browseReads = 0;
  let failNextRead = false;
  const createdCollections: string[] = [];
  await page.addInitScript(dark => {
    localStorage.setItem("akb_token", "vault-context-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    localStorage.setItem("akb-vault-role-scope", "all");
    localStorage.setItem("akb-vault-favorites:v2:context-user", JSON.stringify(["research"]));
  }, dark);
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "context-user", username: "owner", email: "owner@example.invalid", display_name: "Workspace owner", auth_method: "local", key_class: null, is_admin: false } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [
      { id: "engineering", name: "engineering", role: "owner" },
      { id: "research", name: "research", role: "reader" },
      { id: "long", name: "팀 / #? a complete human vault name", role: "writer" },
    ] } });
    if (path.endsWith("/collections/engineering") && route.request().method() === "POST") {
      const { path: collection } = route.request().postDataJSON() as { path: string };
      createdCollections.push(collection);
      return route.fulfill({ json: { created: true, collection: { path: collection, name: collection, doc_count: 0, file_count: 0 } } });
    }
    if (path.endsWith("/info")) return route.fulfill({ json: { name: "engineering", role: "owner", document_count: 1, collection_count: 1, file_count: 0, table_count: 0, public_access: "none" } });
    if (path.includes("/browse/")) {
      browseReads += 1;
      if (failNextRead) { failNextRead = false; return route.fulfill({ status: 500, json: { detail: "Fixture refresh failed" } }); }
      const nameQuery = url.searchParams.get("name_query") || "";
      return route.fulfill({ json: { vault: "engineering", name_query: nameQuery || null, complete: true, next_cursor: null, items: [
      { type: "collection", path: "guides", name: "guides", doc_count: 1 },
      { type: "document", path: "guides/shared-decisions.md", name: "Shared decisions and the full context behind them" },
      ...createdCollections.map(name => ({ type: "collection", path: name, name, doc_count: 0 })),
    ].filter(item => !nameQuery || `${item.name} ${item.path}`.toLowerCase().includes(nameQuery.toLowerCase())), archive_scope: "unarchived" } });
    }
    if (path.includes("/documents/")) return route.fulfill({ status: 404, json: { detail: "No guide" } });
    if (path.endsWith("/help/skill-template")) return route.fulfill({ contentType: "text/plain", body: "# {vault} Guide\n\nStarter template." });
    return route.fulfill({ json: { items: [], changes: [], activity: [], tokens: [], templates: [], unread_count: 0 } });
  });
  return { get browseReads() { return browseReads; }, failNextBrowse() { failNextRead = true; } };
}

for (const width of [375, 1440]) for (const dark of [false, true]) test(`Vault directory and collections share one panel at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 900 });
  await fixture(page, dark);
  await page.goto("/vault");
  await expect(page.getByRole("link", { name: "Back to all vaults", exact: true })).toHaveCount(0);
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  const sidebar = page.getByRole("navigation", { name: "Vaults", exact: true });
  await expect(sidebar).toBeVisible();
  await expect(sidebar.getByRole("link")).toHaveText(["research", "engineering", "팀 / #? a complete human vault name"]);
  await expect(page.locator("#vault-workspace-navigation")).toHaveCount(1);
  const search = page.getByRole("searchbox", { name: "Find a vault in sidebar", exact: true });
  const identity = page.locator('[data-slot="vault-identity-header"]');
  await expect(identity.getByRole("heading", { name: "Knowledge", exact: true })).toBeVisible();
  await expect(identity.getByText("Vaults", { exact: true })).toHaveCount(0);
  const directoryIdentity = (await identity.boundingBox())!;
  const directoryCollapse = (await identity.getByRole("button", { name: "Close vault navigation", exact: true }).boundingBox())!;
  expect(directoryCollapse.x + directoryCollapse.width).toBeGreaterThan(directoryIdentity.x + directoryIdentity.width - 16);
  const directorySearch = (await search.boundingBox())!;
  const directoryManagement = page.locator('[data-slot="vault-management-row"]');
  await expect(directoryManagement.getByText("Vaults", { exact: true })).toBeVisible();
  await expect(directoryManagement.getByRole("button", { name: "Filter sidebar vaults by role", exact: true })).toBeVisible();
  const directoryManagementBounds = (await directoryManagement.boundingBox())!;
  const directoryField = page.locator('[data-slot="sidebar-search-input"]');
  const directoryFieldBounds = (await directoryField.boundingBox())!;
  const panelBounds = (await page.locator("#vault-workspace-navigation").boundingBox())!;
  expect(Math.abs(directoryFieldBounds.width - (panelBounds.width - 16))).toBeLessThanOrEqual(1);
  expect(directoryManagementBounds.y).toBe(directoryIdentity.y + directoryIdentity.height);
  expect(directoryManagementBounds.height).toBe(width < 1024 ? 48 : 40);
  expect(directoryFieldBounds.y).toBe(directoryManagementBounds.y + directoryManagementBounds.height + 8);
  await expect(page.locator('[data-slot="sidebar-search-toolbar"]').getByRole("button")).toHaveCount(0);
  const directoryCreate = (await page.getByRole("button", { name: "Create vault", exact: true }).boundingBox())!;
  const directoryRow = (await sidebar.getByRole("link", { name: "engineering", exact: true }).boundingBox())!;
  const directoryList = (await sidebar.boundingBox())!;
  expect(directoryCreate.y).toBeGreaterThanOrEqual(directoryManagementBounds.y);
  expect(directoryCreate.y + directoryCreate.height).toBeLessThanOrEqual(directoryManagementBounds.y + directoryManagementBounds.height);
  expect(directoryIdentity.height).toBe(56);
  expect(directorySearch.height).toBe(width < 1024 ? 44 : 36);
  expect(directoryCreate.height).toBe(directorySearch.height);
  expect(directoryRow.height).toBe(directorySearch.height);
  await search.fill("engineering");
  await expect(sidebar.getByRole("link")).toHaveCount(1);
  await sidebar.getByRole("link", { name: "engineering", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/engineering$/);
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  await expect(page.getByRole("button", { name: "Switch vault: engineering", exact: true })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "engineering explorer", exact: true })).toBeVisible();
  await expect(sidebar).toHaveCount(0);
  await expect(page.locator("#vault-workspace-navigation")).toHaveCount(1);
  const contentSearch = page.getByRole("searchbox", { name: "Filter resources", exact: true });
  await expect(contentSearch).toBeVisible();
  const namedSearch = (await contentSearch.boundingBox())!;
  const collectionManagement = page.locator('[data-slot="collection-management-row"]');
  await expect(collectionManagement.getByText("Collections", { exact: true })).toBeVisible();
  await expect(collectionManagement.getByRole("button", { name: "Filter content", exact: true })).toBeVisible();
  const collectionManagementBounds = (await collectionManagement.boundingBox())!;
  const collectionFieldBounds = (await page.locator('[data-slot="sidebar-search-input"]').boundingBox())!;
  expect(collectionManagementBounds.y).toBe(directoryManagementBounds.y);
  expect(collectionManagementBounds.height).toBe(directoryManagementBounds.height);
  expect(collectionFieldBounds.y).toBe(collectionManagementBounds.y + collectionManagementBounds.height + 8);
  expect(collectionFieldBounds.width).toBe(directoryFieldBounds.width);
  await expect(page.locator('[data-slot="sidebar-search-toolbar"]').getByRole("button")).toHaveCount(0);
  const collectionList = (await page.getByRole("navigation", { name: "engineering explorer", exact: true }).boundingBox())!;
  expect(collectionList.y).toBe(directoryList.y);
  expect((await identity.boundingBox())!.height).toBe(56);
  expect(namedSearch.y).toBe(directorySearch.y);
  expect(namedSearch.height).toBe(directorySearch.height);
  expect(namedSearch.x).toBe(directorySearch.x);
  expect(namedSearch.width).toBe(directorySearch.width);
  const refresh = page.getByRole("button", { name: "Refresh content", exact: true });
  await expect(refresh).toHaveText("Refresh content");
  await expect(page.locator('[data-slot="sidebar-search-toolbar"]').getByRole("button", { name: "Refresh content", exact: true })).toHaveCount(0);
  const contentCreate = (await page.getByRole("button", { name: "Add content", exact: true }).boundingBox())!;
  expect(contentCreate.x + contentCreate.width).toBe(directoryCreate.x + directoryCreate.width);
  expect(contentCreate.y).toBe(directoryCreate.y);
  await expect(page.getByRole("button", { name: "Vault actions for engineering", exact: true })).toHaveCount(0);
  const collection = page.getByRole("navigation", { name: "engineering explorer", exact: true }).getByRole("button", { name: "guides", exact: true });
  expect((await collection.boundingBox())!.height).toBe(directoryRow.height);
  await collection.click();
  await expect(page.getByRole("link", { name: "Document: Shared decisions and the full context behind them", exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("vault-collections-panel.png") });
  const panel = page.locator("#vault-workspace-navigation");
  const close = panel.getByRole("button", { name: "Close vault navigation", exact: true });
  await expect(close).toBeVisible();
  await expect(close).toHaveAttribute("id", "vault-navigation-collapse");
  const collapseBounds = (await close.boundingBox())!;
  const switcherBounds = (await panel.getByRole("button", { name: "Switch vault: engineering", exact: true }).boundingBox())!;
  expect(collapseBounds.x).toBeGreaterThanOrEqual(switcherBounds.x + switcherBounds.width);
  expect(collapseBounds.x).toBe(directoryCollapse.x);
  const back = page.getByRole("link", { name: "Back to all vaults", exact: true });
  await expect(back).toHaveAttribute("href", "/vault");
  await expect(panel.getByRole("link", { name: "Back to all vaults", exact: true })).toHaveCount(0);
  expect(await back.textContent()).toBe("");
  await close.click();
  await expect(page).toHaveURL(/\/vault\/engineering$/);
  await expect(panel).toBeHidden();
  const reopen = page.getByRole("button", { name: "Open vault navigation", exact: true });
  await expect(reopen).toBeVisible();
  await expect(reopen).toBeFocused();
  const stub = page.locator('[data-slot="vault-navigation-collapsed"]');
  const stubBounds = (await stub.boundingBox())!;
  const viewport = (await page.locator('[data-slot="vault-route-viewport"]').boundingBox())!;
  expect(viewport.x).toBeGreaterThanOrEqual(stubBounds.x + stubBounds.width);
  await reopen.click();
  await expect(panel).toBeVisible();
  await expect(page.getByRole("navigation", { name: "engineering explorer", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Document: Shared decisions and the full context behind them", exact: true })).toBeVisible();
  if (width < 1024) await close.click();
  await back.click();
  await expect(page).toHaveURL(/\/vault$/);
  await expect(page.getByRole("link", { name: "Back to all vaults", exact: true })).toHaveCount(0);
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  await expect(sidebar).toBeVisible();
  await expect(page.getByRole("button", { name: "Switch vault: engineering", exact: true })).toHaveCount(0);
  await expect(page.locator("#vault-workspace-navigation")).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("vault-directory-panel.png") });
});

for (const width of [375, 1440]) test(`Collections leading return reveals the Vault directory at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await fixture(page, false);
  await page.addInitScript(() => localStorage.setItem("akb.focusedNavigationWidth.v1", "224"));
  await page.goto("/vault/engineering");
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  const row = page.locator('[data-slot="collection-management-row"]');
  const back = row.getByRole("link", { name: "Show vault list", exact: true });
  await expect(back).toHaveAttribute("href", "/vault");
  const bounds = (await back.boundingBox())!;
  const label = (await row.getByText("Collections", { exact: true }).boundingBox())!;
  const filter = (await row.getByRole("button", { name: "Filter content", exact: true }).boundingBox())!;
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(label.x);
  expect(label.x + label.width).toBeLessThanOrEqual(filter.x);
  await back.focus();
  await back.press("Enter");
  await expect(page).toHaveURL(/\/vault$/);
  const sidebar = page.getByRole("navigation", { name: "Vaults", exact: true });
  await expect(sidebar).toBeVisible();
  await expect(page.getByRole("searchbox", { name: "Find a vault in sidebar", exact: true })).toBeFocused();
  await sidebar.getByRole("link", { name: "engineering", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/engineering$/);
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  await expect(back).toBeVisible();
});

test("mobile Escape dismisses a tooltip with the drawer and preserves nested picker and menu priority", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await fixture(page, false);
  await page.goto("/vault/engineering");
  const panel = page.locator("#vault-workspace-navigation");
  const open = page.getByRole("button", { name: "Open vault navigation", exact: true });
  const switcher = page.getByRole("button", { name: "Switch vault: engineering", exact: true });
  const close = panel.getByRole("button", { name: "Close vault navigation", exact: true });
  await open.click();
  await expect(close).toBeFocused();
  await expect(page.getByRole("tooltip", { name: "Close vault navigation", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await expect(page.getByRole("button", { name: "Dismiss vault navigation", exact: true })).toHaveCount(0);
  await expect(open).toBeFocused();

  await open.click();
  await switcher.click();
  const picker = page.getByRole("dialog", { name: "Vaults", exact: true });
  await expect(picker).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(picker).toHaveCount(0);
  await expect(panel).toBeVisible();
  await expect(switcher).toBeFocused();

  const actions = page.getByRole("button", { name: "Add content", exact: true });
  await actions.click();
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(panel).toBeVisible();
  await expect(actions).toBeFocused();

  await page.getByRole("button", { name: "Filter content", exact: true }).click();
  await page.getByRole("button", { name: "Collection search scope", exact: true }).click();
  const collectionPicker = page.getByRole("dialog", { name: "Choose a collection", exact: true });
  await expect(collectionPicker).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(collectionPicker).toHaveCount(0);
  await expect(panel).toBeVisible();
  await expect(page.getByRole("button", { name: "Collection search scope", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await expect(open).toBeFocused();
});

test("a minimum-width sidebar keeps full Vault identity and a usable persistent search", async ({ page }) => {
  const name = "팀 / #? a complete human vault name";
  await page.setViewportSize({ width: 1440, height: 900 });
  await fixture(page, false);
  await page.addInitScript(() => localStorage.setItem("akb.focusedNavigationWidth.v1", "224"));
  await page.goto(`/vault/${encodeURIComponent(name)}`);
  const panel = page.locator("#vault-workspace-navigation");
  const identity = panel.locator('[data-slot="vault-identity-header"]');
  const switcher = identity.getByRole("button", { name: `Switch vault: ${name}`, exact: true });
  await expect(switcher).toHaveText(name);
  await expect(identity.getByRole("button", { name: "Close vault navigation", exact: true })).toBeVisible();
  await expect(panel.getByRole("link", { name: "Back to all vaults", exact: true })).toHaveCount(0);
  expect((await panel.boundingBox())!.width).toBe(224);
  const search = panel.getByRole("searchbox", { name: "Filter resources", exact: true });
  await expect(search).toBeVisible();
  const inputBox = panel.locator('[data-slot="sidebar-search-input"]');
  expect((await inputBox.boundingBox())!.width).toBeGreaterThanOrEqual(207);
  await search.fill("content");
  expect(await search.evaluate(element => {
    const style = getComputedStyle(element);
    return element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  })).toBeGreaterThanOrEqual(120);
  const clear = page.getByRole("button", { name: "Clear filter resources", exact: true });
  const filter = page.getByRole("button", { name: "Filter content, 1 active", exact: true });
  const clearBounds = (await clear.boundingBox())!;
  expect((await search.boundingBox())!.x + (await search.boundingBox())!.width).toBeLessThanOrEqual(clearBounds.x + 1);
  expect((await filter.boundingBox())!.y + (await filter.boundingBox())!.height).toBeLessThanOrEqual((await inputBox.boundingBox())!.y);
  await page.getByRole("button", { name: "Clear filter resources", exact: true }).click();
  await expect(search).toHaveValue("");
  await expect(search).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const touch of [false, true]) test.describe(`management and search rows with ${touch ? "coarse" : "fine"} pointer`, () => {
  test.use({ hasTouch: touch });
  for (const panelWidth of [224, 256]) test(`keeps both sidebars readable and controls independent at ${panelWidth}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await fixture(page, false);
    await page.addInitScript(width => localStorage.setItem("akb.focusedNavigationWidth.v1", String(width)), panelWidth);
    await page.goto("/vault");
    const panel = page.locator("#vault-workspace-navigation");
    for (const kind of ["directory", "collection"] as const) {
      const search = panel.getByRole("searchbox", { name: kind === "directory" ? "Find a vault in sidebar" : "Filter resources", exact: true });
      const add = panel.getByRole("button", { name: kind === "directory" ? "Create vault" : "Add content", exact: true });
      const filterLabel = kind === "directory" ? "Filter sidebar vaults by role" : "Filter content";
      const management = panel.locator(`[data-slot="${kind === "directory" ? "vault" : "collection"}-management-row"]`);
      await expect(add).toBeVisible();
      await expect(management.getByText(kind === "directory" ? "Vaults" : "Collections", { exact: true })).toBeVisible();
      const inputBox = panel.locator('[data-slot="sidebar-search-input"]');
      await expect(inputBox.locator(".lucide-search")).toBeVisible();
      const fieldBounds = (await inputBox.boundingBox())!;
      const panelBounds = (await panel.boundingBox())!;
      expect(Math.abs(fieldBounds.width - (panelBounds.width - 16))).toBeLessThanOrEqual(1);
      const managementBounds = (await management.boundingBox())!;
      expect(managementBounds.height).toBe(touch ? 48 : 40);
      expect(fieldBounds.y).toBe(managementBounds.y + managementBounds.height + 8);
      const placeholderFits = await search.evaluate(element => {
        const style = getComputedStyle(element);
        const textWidth = element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
        const context = document.createElement("canvas").getContext("2d")!;
        context.font = style.font;
        return context.measureText((element as HTMLInputElement).placeholder).width <= textWidth;
      });
      expect(placeholderFits).toBe(true);
      await search.fill("architecture");
      expect(await search.evaluate(element => {
        const style = getComputedStyle(element);
        return element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      })).toBeGreaterThanOrEqual(touch ? 100 : 120);
      const clear = inputBox.getByRole("button", { name: kind === "directory" ? "Clear find a vault in sidebar" : "Clear filter resources", exact: true });
      const filter = management.getByRole("button", { name: kind === "directory" ? filterLabel : `${filterLabel}, 1 active`, exact: true });
      const clearBounds = (await clear.boundingBox())!;
      const searchBounds = (await search.boundingBox())!;
      const filterBounds = (await filter.boundingBox())!;
      const addBounds = (await add.boundingBox())!;
      expect(clearBounds.height).toBe(touch ? 44 : 36);
      expect(filterBounds.width).toBe(touch ? 44 : 36);
      expect(searchBounds.x + searchBounds.width).toBeLessThanOrEqual(clearBounds.x + 1);
      expect(clearBounds.x + clearBounds.width).toBeLessThanOrEqual(fieldBounds.x + fieldBounds.width + 1);
      expect(addBounds.y).toBe(filterBounds.y);
      expect(filterBounds.x + filterBounds.width).toBeLessThanOrEqual(addBounds.x);
      expect(addBounds.y + addBounds.height).toBeLessThanOrEqual(fieldBounds.y);
      await search.focus();
      const focusedBorder = await inputBox.evaluate(element => getComputedStyle(element).boxShadow);
      if (!touch) {
        const restingButton = await filter.evaluate(element => getComputedStyle(element).backgroundColor);
        await filter.hover();
        await expect.poll(() => filter.evaluate(element => getComputedStyle(element).backgroundColor)).not.toBe(restingButton);
        expect(await inputBox.evaluate(element => getComputedStyle(element).boxShadow)).toBe(focusedBorder);
      }
      await filter.focus();
      await expect.poll(() => inputBox.evaluate(element => getComputedStyle(element).boxShadow)).not.toBe(focusedBorder);
      await search.focus();
      await page.keyboard.press("Tab");
      await expect(clear).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(search).toHaveValue("");
      await expect(search).toBeFocused();
      expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${kind}-management-search.png`) });
      if (kind === "directory") await panel.getByRole("navigation", { name: "Vaults", exact: true }).getByRole("link", { name: "engineering", exact: true }).click();
    }
  });
});

test("quiet footer refresh retries failures and collection creation refreshes the list automatically", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const data = await fixture(page, false);
  await page.goto("/vault/engineering");
  const panel = page.locator("#vault-workspace-navigation");
  const explorer = panel.getByRole("navigation", { name: "engineering explorer", exact: true });
  await expect(explorer.getByRole("button", { name: "guides", exact: true })).toBeVisible();
  const refresh = panel.getByRole("button", { name: "Refresh content", exact: true });
  await expect(refresh.locator("svg")).toHaveCount(0);
  const initialReads = data.browseReads;
  await refresh.click();
  await expect.poll(() => data.browseReads).toBeGreaterThan(initialReads);
  await expect(refresh).toBeEnabled();
  data.failNextBrowse();
  await refresh.click();
  await expect(panel.getByRole("alert")).toHaveText("Fixture refresh failed");
  await expect(explorer.getByRole("button", { name: "guides", exact: true })).toBeVisible();
  await refresh.click();
  await expect(panel.getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: "Add content", exact: true }).click();
  await page.getByRole("menuitem", { name: "New collection", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "New collection", exact: true });
  await dialog.getByRole("textbox", { name: "Path", exact: true }).fill("reference");
  const beforeCreate = data.browseReads;
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(explorer.getByRole("button", { name: "reference", exact: true })).toBeVisible();
  expect(data.browseReads).toBeGreaterThan(beforeCreate);
});
