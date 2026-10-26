import { expect, test } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");

for (const dark of [false, true]) test(`Focused vault navigation ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
  await page.addInitScript(dark => {
    localStorage.setItem("akb_token", "rail-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    localStorage.setItem("akb.treeVisible", "1");
    localStorage.setItem("akb.focusedNavigationWidth.v1", "264");
  }, dark);
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const vault = path.includes("/research") ? "research" : "team";
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "rail-user", username: "reader", display_name: "Workspace User", is_admin: false, auth_method: "local" } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "team-id", name: "team", role: "owner" }, { id: "research-id", name: "research", role: "reader" }, { id: "work-id", name: "work-notes", role: "writer" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: { name: vault, role: vault === "team" ? "owner" : "reader", document_count: 1 } });
    if (path.includes("/browse/")) return route.fulfill({ json: { vault, path: "", name_query: url.searchParams.get("name_query"), complete: true, next_cursor: null, archive_scope: url.searchParams.get("archive_scope") || "unarchived", items: [
      { type: "collection", name: "guides", path: "guides", doc_count: 1 },
      { type: "document", name: "Getting started", path: "guides/start.md" },
    ].filter(item => !url.searchParams.get("name_query") || `${item.name} ${item.path}`.toLowerCase().includes(url.searchParams.get("name_query")!.toLowerCase())) } });
    if (path.includes("/documents/")) return route.fulfill({ json: { id: "getting-started", path: "guides/start.md", title: "Getting started", content: "# Getting started\n\nStart with a collection.", current_commit: "fixture-head", metadata: {}, status: "active" } });
    return route.fulfill({ json: { tokens: [], items: [], changes: [], templates: [], relations: [], history: [] } });
  });

  await page.goto("/vault/team?collection=guides");
  const panel = page.locator("#vault-workspace-navigation");
  const collections = panel.getByRole("complementary", { name: "team collections", exact: true });
  const explorer = collections.getByRole("navigation", { name: "team explorer", exact: true });
  const collection = explorer.getByRole("button", { name: "guides", exact: true });
  const document = explorer.getByRole("link", { name: "Document: Getting started", exact: true });
  const picker = panel.getByRole("button", { name: "Switch vault: team", exact: true });
  await expect(picker).toBeVisible();
  await expect(panel.locator('[data-slot="vault-identity-header"]')).toHaveCount(1);
  await expect(panel.locator('[data-slot="collection-identity-header"]')).toHaveCount(0);
  const addContent = panel.getByRole("button", { name: "Add content", exact: true });
  await expect(addContent).toHaveCount(1);
  await expect(addContent).toHaveText("");
  await expect(panel.getByText("New content", { exact: true })).toHaveCount(0);
  await expect(panel.getByRole("link", { name: "Overview", exact: true })).toHaveCount(0);
  await expect(picker.getByText("team", { exact: true })).toBeVisible();
  await expect(panel.getByRole("link", { name: "Back to all vaults", exact: true })).toHaveCount(0);
  const parentLink = page.getByRole("link", { name: "Back to all vaults", exact: true });
  await expect(parentLink).toHaveAttribute("href", "/vault");
  await expect(parentLink).toHaveText("");
  await expect(panel.getByRole("button", { name: "Close vault navigation", exact: true })).toBeVisible();
  await expect(explorer).toBeVisible();
  await expect(explorer.getByRole("treeitem")).toHaveCount(0);
  await expect(page.getByRole("tree")).toHaveCount(0);
  await expect(collection).toHaveAttribute("aria-current", "page");
  await expect(collection).toHaveAttribute("aria-expanded", "true");
  await expect(document).toHaveAttribute("href", "/vault/team/doc/guides%2Fstart.md");

  // Collection buttons disclose children; resources retain native links.
  // Return focus to the parent before hiding its descendants.
  await document.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(collection).toBeFocused();
  await page.keyboard.press("ArrowLeft");
  await expect(collection).toHaveAttribute("aria-expanded", "false");
  await expect(document).toHaveCount(0);
  await page.keyboard.press("ArrowRight");
  await expect(collection).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("ArrowRight");
  await expect(document).toBeFocused();

  const filter = collections.getByRole("searchbox", { name: "Filter resources", exact: true });
  const toggle = collections.getByRole("button", { name: /^Filter content(?:, \d+ active)?$/ });
  await expect(filter).toBeVisible();
  await toggle.click();
  await expect(filter).toBeFocused();
  await filter.fill("not found");
  await collections.getByRole("button", { name: "Collection document state" }).click();
  await page.getByRole("menuitemradio", { name: /^All documents/ }).click();
  await collections.getByRole("button", { name: "Filter content, 3 active", exact: true }).click();
  await expect(filter).toBeVisible();
  await expect(collections.getByText("Name: not found · Collection: guides · Includes archived", { exact: true })).toBeVisible();
  await collections.getByRole("button", { name: "Reset filters", exact: true }).click();
  await expect(filter).toBeFocused();
  await expect(filter).toHaveValue("");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(collections.getByRole("button", { name: "Reset filters", exact: true })).toHaveCount(0);
  await expect(document).toBeVisible();

  const resize = page.getByRole("separator", { name: "Resize vault navigation", exact: true });
  await expect(resize).toHaveCount(1);
  await resize.focus();
  await page.keyboard.press("Home");
  await expect(resize).toHaveAttribute("aria-valuenow", "224");
  await page.keyboard.press("End");
  await expect(resize).toHaveAttribute("aria-valuenow", "360");
  await page.keyboard.press("ArrowLeft");
  await expect(resize).toHaveAttribute("aria-valuenow", "352");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("akb.focusedNavigationWidth.v1"))).toBe("352");
  await page.screenshot({ path: testInfo.outputPath("focused-navigation.png"), animations: "disabled", fullPage: true });

  // One persistent toggle controls the whole context panel.
  const close = page.getByRole("button", { name: "Close vault navigation", exact: true });
  await expect(close).toHaveCount(1);
  await close.click();
  await expect(panel).toBeHidden();
  await expect(resize).toHaveCount(0);
  const open = page.getByRole("button", { name: "Open vault navigation", exact: true });
  await expect(open).toBeFocused();
  await open.click();
  await expect(panel).toBeVisible();

  await document.click();
  await expect(page).toHaveURL(/\/vault\/team\/doc\/guides%2Fstart\.md$/);
  await expect(document).toHaveAttribute("aria-current", "page");
  await picker.click();
  const findVault = page.getByRole("searchbox", { name: "Find a vault", exact: true });
  await expect(findVault).toBeFocused();
  const vaults = page.getByRole("navigation", { name: "Accessible vaults", exact: true });
  await expect(vaults.getByRole("link", { name: "team, current vault", exact: true })).toHaveAttribute("aria-current", "location");
  await expect(page.getByRole("dialog", { name: "Vaults", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(picker).toBeFocused();
  // Global navigation remains a native directory link; the contextual
  // switcher opens a popover immediately below its own control.
  const globalRail = page.getByTestId("app-sidebar");
  await expect(globalRail).toHaveAttribute("data-compact", "true");
  const globalVaults = globalRail.getByRole("link", { name: "Vaults", exact: true });
  await expect(globalVaults).toHaveText("");
  await expect(globalVaults).toHaveAttribute("href", "/vault");
  await expect(globalRail.getByRole("button", { name: "Vaults", exact: true })).toHaveCount(0);
  await picker.click();
  const chooser = page.getByRole("dialog", { name: "Vaults", exact: true });
  await expect(chooser).toHaveAttribute("data-side", "bottom");
  await expect(picker).toHaveAttribute("aria-expanded", "true");
  const iconBounds = (await picker.boundingBox())!;
  const popupBounds = (await chooser.boundingBox())!;
  expect(popupBounds.y - iconBounds.y - iconBounds.height).toBeGreaterThanOrEqual(4);
  expect(popupBounds.y - iconBounds.y - iconBounds.height).toBeLessThanOrEqual(16);
  await expect(page.locator('[data-slot="dialog-overlay"]')).toHaveCount(0);
  await chooser.getByRole("button", { name: "Filter by role", exact: true }).click();
  const roles = chooser.getByRole("group", { name: "Filter vaults by your role", exact: true });
  await roles.getByRole("button", { name: "Can edit", exact: true }).click();
  await expect(vaults.getByRole("link")).toHaveCount(2);
  await expect(vaults.getByRole("link", { name: "research", exact: true })).toHaveCount(0);
  await roles.getByRole("button", { name: "Owned", exact: true }).click();
  await expect(vaults.getByRole("link")).toHaveCount(1);
  await expect(vaults.getByRole("link", { name: "team, current vault", exact: true })).toBeVisible();
  await chooser.getByRole("button", { name: "Filter by role, 1 active", exact: true }).click();
  await expect(roles).toBeHidden();
  await expect(chooser.getByText("Owned", { exact: true }).filter({ visible: true })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("akb-vault-role-scope"))).toBe("owned");
  await page.screenshot({ path: testInfo.outputPath("anchored-vault-picker.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await expect(picker).toBeFocused();
  await expect(picker).toHaveAttribute("aria-expanded", "false");
  await picker.click();
  await expect(chooser).toHaveAttribute("data-side", "bottom");
  await expect(picker).toHaveAttribute("aria-expanded", "true");
  await expect(chooser.getByText("Owned", { exact: true }).filter({ visible: true })).toBeVisible();
  await findVault.fill("research");
  await expect(vaults.getByRole("link")).toHaveCount(0);
  await chooser.getByRole("button", { name: "Reset filters", exact: true }).click();
  await expect(findVault).toHaveValue("");
  await expect(vaults.getByRole("link", { name: "research", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(picker).toBeFocused();
  await picker.focus();
  await page.keyboard.press("Enter");
  await expect(findVault).toBeFocused();
  await findVault.fill("research");
  await expect(vaults.getByRole("link")).toHaveCount(1);
  const research = vaults.getByRole("link", { name: "research", exact: true });
  await expect(research).toHaveAttribute("href", "/vault/research");
  await research.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: "Vaults", exact: true })).toHaveCount(0);
  await expect(page.locator("#main")).toBeFocused();
  await expect(page).toHaveURL(/\/vault\/research$/);
  await expect(panel.getByRole("button", { name: "Switch vault: research", exact: true })).toBeVisible();
  await expect(panel.getByRole("navigation", { name: "research explorer", exact: true })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Add content", exact: true })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Vault actions for team", exact: true })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Vault actions for research", exact: true })).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link", { name: "Members", exact: true })).toHaveAttribute("href", "/vault/research/members");
});
