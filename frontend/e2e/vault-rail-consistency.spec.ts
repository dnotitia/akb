import { expect, test } from "@playwright/test";

for (const dark of [false, true]) test(`Vault navigation rails ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
  await page.addInitScript(dark => {
    localStorage.setItem("akb_token", "rail-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    localStorage.setItem("akb.vaultRailCollapsed", "0");
    localStorage.setItem("akb.treeVisible", "1");
  }, dark);
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "rail-user", username: "reader", display_name: "Workspace User", is_admin: false, auth_method: "local" } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "team-id", name: "team", role: "owner" }, { id: "research-id", name: "research", role: "reader" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: { name: "team", role: "owner", document_count: 1 } });
    if (path.includes("/browse/")) return route.fulfill({ json: { vault: "team", path: "", archive_scope: url.searchParams.get("archive_scope") || "unarchived", items: [
      { type: "collection", name: "guides", path: "guides", doc_count: 1 },
      { type: "document", name: "Getting started", path: "guides/start.md" },
    ] } });
    return route.fulfill({ json: { tokens: [], items: [], changes: [], templates: [] } });
  });
  await page.goto("/vault/team?collection=guides");
  const collections = page.getByRole("complementary", { name: "team collections", exact: true });
  await expect(collections.getByRole("button", { name: "guides", exact: true })).toBeVisible();
  await expect(collections.locator('[data-slot="collection-identity-header"]')).toHaveText("guides");
  await expect(collections.getByText("Manage content")).toHaveCount(0);
  for (const slot of ["identity-header", "management-row", "filter-row"]) {
    const vault = (await page.locator(`[data-slot="vault-${slot}"]`).boundingBox())!;
    const collection = (await collections.locator(`[data-slot="collection-${slot}"]`).boundingBox())!;
    expect(Math.abs(vault.y - collection.y)).toBeLessThan(1);
    expect(vault.height).toBe(collection.height);
  }
  const collectionRow = collections.getByRole("button", { name: "guides", exact: true }).locator("..");
  expect((await collectionRow.boundingBox())!.height).toBe(36);
  await expect(collections.getByRole("button", { name: "Collection document state" })).toBeHidden();
  await page.screenshot({ path: testInfo.outputPath("rails.png"), fullPage: true });
  await collections.getByRole("searchbox").fill("not found");
  await collections.getByRole("button", { name: "Filter collections", exact: true }).click();
  await collections.getByRole("button", { name: "Collection document state" }).click();
  await page.getByRole("menuitemradio", { name: /^All documents/ }).click();
  await expect(collections.getByRole("searchbox")).toHaveValue("not found");
  await collections.getByRole("button", { name: "Filter collections, 1 active" }).click();
  await collections.getByRole("button", { name: "Reset filters" }).click();
  await collections.getByRole("button", { name: "Clear filter resources" }).click();
  await expect(collections.getByRole("searchbox")).toBeFocused();
  const vaultToggle = page.getByRole("button", { name: "Minimize vault list to a rail" });
  const treeToggle = page.getByRole("button", { name: "Collapse collection tree" });
  expect(await vaultToggle.locator("svg").getAttribute("class")).toBe(await treeToggle.locator("svg").getAttribute("class"));
  await treeToggle.click();
  await page.getByRole("button", { name: "Show collection tree" }).click();
  await expect(collections).toBeVisible();
  await vaultToggle.click();
  await page.getByRole("button", { name: "Expand vault list" }).click();
  await expect(page.getByRole("searchbox", { name: "Filter vaults" })).toBeVisible();
});
