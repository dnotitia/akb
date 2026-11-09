import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP fixtures; no backend writes.");

async function fixture(page: Page, documentStatus = 200) {
  await page.addInitScript(() => {
    localStorage.setItem("akb_token", "overlay-fixture");
    localStorage.setItem("akb_theme", "light");
  });
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "00000000-0000-4000-8000-000000000100", username: "fixture", display_name: "Fixture Author", auth_method: "local", is_admin: false } });
    if (path.endsWith("/my/vaults")) return route.fulfill({ json: { vaults: [{ id: "fixture-vault", name: "fixture", role: "owner" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: { name: "fixture", role: "owner", role_source: "member", public_access: "none", is_archived: false, is_external_git: false, document_count: 1 } });
    if (path.endsWith("/members")) return route.fulfill({ json: { members: [{ id: "00000000-0000-4000-8000-000000000100", username: "fixture", role: "owner", display_name: "Fixture Author", email: "fixture@example.test", account_kind: "human", account_status: "active" }] } });
    if (path.endsWith("/users/search")) return route.fulfill({ json: { users: Array.from({ length: 20 }, (_, i) => ({ id: `00000000-0000-4000-8000-${(i + 1).toString().padStart(12, "0")}`, username: `user${i}`, display_name: `Test User ${i}`, email: `user${i}@example.test`, account_kind: "human", account_status: "active" })) } });
    if (path.includes("/browse/")) return route.fulfill({ json: { items: [{ type: "collection", name: "guides", path: "guides" }, { type: "document", name: "Operating guide", path: "guides/reading.md" }] } });
    if (path === "/api/v1/tables/fixture") return route.fulfill({ json: { items: [{ name: "incidents", collection: "guides", description: "Operational incidents", row_count: 1, columns: [{ name: "title", type: "text", required: true }] }] } });
    if (path.endsWith("/rows")) return route.fulfill({ json: { columns: ["id", "title"], items: [{ id: "6ab163e8-6ea4-4d20-8765-bf912716384c", title: "API outage" }], total: 1 } });
    if (path.endsWith("/search")) return route.fulfill({ json: { total: 1, results: [{ title: "Operating guide", uri: "akb://fixture/coll/guides/doc/reading.md", path: "guides/reading.md", vault: "fixture", source_type: "document", score: 1 }] } });
    if (path.includes("/documents/fixture/") && !path.endsWith("/history")) {
      if (documentStatus === 0) return; // Deliberately keep the reader in Loading.
      return route.fulfill(documentStatus === 200
      ? { json: { uri: "akb://fixture/coll/guides/doc/reading.md", path: "guides/reading.md", title: "Operating guide", content: "# Reading\n\nKeep the reader in context.", current_commit: "aaaaaaaaaaaaa", status: "active" } }
      : { status: documentStatus, json: { detail: "Fixture document is unavailable" } });
    }
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
}

for (const status of [0, 200, 403, 404, 503]) {
  test(`preview Close remains clickable with document response ${status}`, async ({ page }) => {
    await fixture(page, status);
    await page.goto("/search");
    await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
    const search = page.getByTestId("global-search-dialog");
    await search.getByRole("combobox").fill("guide");
    await search.getByRole("option", { name: /Operating guide/ }).click();
    const preview = page.getByTestId("document-preview-dialog");
    await expect(preview).toBeVisible();
    if (status !== 0 && status !== 200) await expect(preview.getByRole("heading", { name: status === 403 ? "You don’t have access to this document" : status === 404 ? "Document unavailable" : "Couldn’t load this document", exact: true })).toBeVisible();
    await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
    await expect(preview).toHaveCount(0);
    await expect(search).toBeVisible();
  });
}

test("an unavailable preview offers a direct return to its search results", async ({ page }) => {
  await fixture(page, 403);
  await page.goto("/search");
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  const search = page.getByTestId("global-search-dialog");
  await search.getByRole("combobox").fill("guide");
  await search.getByRole("option", { name: /Operating guide/ }).click();
  const preview = page.getByTestId("document-preview-dialog");
  await preview.getByRole("button", { name: "Close preview", exact: true }).click();
  await expect(preview).toHaveCount(0);
  await expect(search).toBeVisible();
  await expect(search.getByRole("combobox")).toHaveValue("guide");
});

for (const height of [540, 320]) {
test(`mobile Invite keeps footer reachable at height ${height}`, async ({ page }, testInfo) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height });
  await page.goto("/vault/fixture/members");
  await page.getByRole("button", { name: "Invite member", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Invite member" });
  await expect(dialog.getByRole("button", { name: "Select role for user0", exact: true })).toBeVisible();
  await expect(dialog.getByTestId("invite-footer")).toBeInViewport();
  const body = dialog.getByTestId("invite-body");
  expect(await body.evaluate(el => el.scrollHeight > el.clientHeight && getComputedStyle(el).overflowY === "auto")).toBe(true);
  await body.evaluate(el => { el.scrollTop = el.scrollHeight; });
  const lastRole = dialog.getByRole("button", { name: "Select role for user19", exact: true });
  await expect(lastRole).toBeInViewport();
  await lastRole.click();
  await page.getByRole("menuitemradio", { name: "Admin", exact: true }).click();
  await expect(dialog.getByTestId("invite-summary")).toHaveText("Test User 19 will join as Admin.");
  await expect(dialog.getByTestId("invite-footer")).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("invite-mobile.png") });
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog).toHaveCount(0);
});
}

test("Schema rapid toggles never shift the table and narrow workspaces trap focus", async ({ page }, testInfo) => {
  await fixture(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/vault/fixture/table/incidents");
  const toggle = page.getByRole("button", { name: "Schema", exact: true });
  await expect(toggle).toBeVisible();
  const canvas = page.getByTestId("table-workspace-canvas");
  const before = await canvas.boundingBox();
  for (let i = 0; i < 5; i++) {
    await toggle.click();
    await page.keyboard.press("Escape");
    await expect(toggle).toBeFocused();
    await expect(page.getByRole("complementary", { name: "Table schema" })).toHaveCount(0);
    expect(await canvas.evaluate(el => el.scrollLeft)).toBe(0);
  }
  expect(await canvas.boundingBox()).toEqual(before);
  await toggle.click();
  const inspector = page.getByRole("dialog", { name: "Table schema" });
  const backdrop = page.locator('[data-slot="dialog-overlay"]');
  await expect(inspector).toBeVisible();
  await expect(backdrop).toHaveCount(0);
  expect(await canvas.boundingBox()).toEqual(before);
  await page.screenshot({ path: testInfo.outputPath("schema-desktop.png") });
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 680, height: 700 });
  await toggle.click();
  await expect(inspector).toHaveAttribute("data-state", "open");
  await expect(backdrop).toBeVisible();
  await expect(inspector.getByRole("button", { name: "Close table schema" })).toBeFocused();
  await page.keyboard.press("Tab");
  expect(await inspector.evaluate(el => el.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Shift+Tab");
  expect(await inspector.evaluate(el => el.contains(document.activeElement))).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("schema-narrow.png") });
  // Resizing an open inspector swaps Radix's modal/non-modal implementation,
  // but must not restore focus to the toggle as though it had been closed.
  // Wait for the responsive mode: the old Close button can still be focused
  // before the resize handler has mounted the replacement focus scope.
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(backdrop).toHaveCount(0);
  await expect(inspector.getByRole("button", { name: "Close table schema" })).toBeFocused();
  await expect(inspector).toBeVisible();
  await page.setViewportSize({ width: 680, height: 700 });
  await expect(backdrop).toBeVisible();
  await expect(inspector.getByRole("button", { name: "Close table schema" })).toBeFocused();
  await page.keyboard.press("Tab");
  expect(await inspector.evaluate(el => el.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(toggle).toBeFocused();
  expect(await canvas.evaluate(el => el.scrollLeft)).toBe(0);
});

test("a row draft survives backdrop dismissal until explicit discard", async ({ page }) => {
  await fixture(page);
  await page.goto("/vault/fixture/table/incidents");
  await page.getByRole("button", { name: "Add row", exact: true }).click();
  const row = page.getByRole("dialog", { name: "Add row" });
  await row.getByLabel("title", { exact: true }).fill("Keep this row");
  await page.mouse.click(4, 4);
  const discard = page.getByRole("dialog", { name: "Discard row changes?" });
  await expect(discard).toBeVisible();
  await discard.getByRole("button", { name: "Keep editing" }).click();
  await expect(row.getByLabel("title", { exact: true })).toHaveValue("Keep this row");
  await row.getByRole("button", { name: "Cancel", exact: true }).click();
  await discard.getByRole("button", { name: "Discard changes" }).click();
  await expect(row).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add row", exact: true })).toBeFocused();
});
