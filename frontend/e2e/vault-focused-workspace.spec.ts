import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures, never a user's data.");
test.use({ contextOptions: { reducedMotion: "reduce" }, launchOptions: { args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] } });

async function fixture(page: Page, dark = false, navigationVisible = "1") {
  await page.addInitScript(({ dark, navigationVisible }) => {
    localStorage.setItem("akb_token", "focused-vault-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    if (!localStorage.getItem("focused-fixture-ready")) {
      localStorage.setItem("akb.treeVisible", navigationVisible);
      localStorage.setItem("akb.focusedNavigationWidth.v1", "286");
      // Old independent-column widths must not size the unified context panel.
      localStorage.setItem("akb.treeWidth.v2", "320");
      localStorage.setItem("akb.vaultRailWidth.v2", "218");
      localStorage.setItem("focused-fixture-ready", "1");
    }
  }, { dark, navigationVisible });
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    let json: object = { items: [], history: [], changes: [], relations: [], templates: [], unread_count: 0 };
    if (path.endsWith("/auth/config")) json = { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
    else if (path.endsWith("/auth/me")) json = { user_id: "focus-owner", username: "owner", display_name: "Review owner", email: "owner@example.invalid", auth_method: "local", is_admin: false };
    else if (path.endsWith("/vaults")) json = { vaults: [{ id: "focus-vault", name: "fixture", role: "owner" }] };
    else if (path.endsWith("/info")) json = { name: "fixture", role: "owner", public_access: "none", description: "A place for operating knowledge.", owner_display_name: "Review owner", created_at: "2026-09-01T09:00:00Z", member_count: 1, document_count: 2, collection_count: 1, table_count: 0, file_count: 0 };
    else if (path.endsWith("/members")) json = { members: [{ user_id: "focus-owner", username: "owner", display_name: "Review owner", email: "owner@example.invalid", role: "owner", since: "2026-09-01T09:00:00Z" }] };
    else if (path.includes("/browse/")) json = { items: [
      { type: "collection", path: "guides", name: "guides", doc_count: 2 },
      { type: "document", path: "guides/start.md", name: "Getting started" },
      { type: "document", path: "guides/runbook.md", name: "Operations" },
    ] };
    else if (path.includes("/publications/")) json = { publications: [] };
    else if (path.endsWith("/activity")) json = { activity: [], total: 0 };
    else if (path.endsWith("/graph/overview")) json = { nodes: [
      { uri: "akb://fixture/coll/guides/doc/start.md", name: "Getting started", resource_type: "document", degree: 1 },
      { uri: "akb://fixture/coll/guides/doc/runbook.md", name: "Operations", resource_type: "document", degree: 1 },
    ], edges: [{ source: "akb://fixture/coll/guides/doc/start.md", target: "akb://fixture/coll/guides/doc/runbook.md", relation: "references", kind: "explicit" }], nodes_total: 2, returned: 2, edges_total: 1, truncated: false, orphans_returned: 0, orphans_truncated: false };
    else if (path.includes("/documents/fixture/")) json = { id: "guide", path: "overview/vault-skill.md", title: "Vault guide", content: "# Working together\n\nKeep decisions with their context.", current_commit: "guide-head", metadata: {}, status: "active" };
    return route.fulfill({ json });
  });
}

const routes = {
  settings: { label: "Settings", action: "Vault settings" },
  members: { label: "Members", action: "Members" },
  publications: { label: "Public links", action: "Public links" },
  graph: { label: "Graph", action: "Graph" },
  activity: { label: "Activity", action: "Activity" },
} as const;

for (const width of [375, 1920]) for (const dark of [false, true]) for (const [route, { label }] of Object.entries(routes)) {
  test(`${label} retains focused navigation at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, dark);
    await page.goto(`/vault/fixture/${route}`);
    const trail = page.getByRole("navigation", { name: "Current page", exact: true });
    await expect(trail.locator('[aria-current="page"]')).toHaveText(label);
    await expect(trail.getByRole("button")).toHaveCount(0);
    if (route !== "activity") {
      const back = trail.getByRole("link", { name: "Back to fixture", exact: true });
      await expect(back).toHaveAttribute("href", "/vault/fixture");
      const box = (await back.boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(44);
      expect(box.width).toBeGreaterThanOrEqual(44);
    }
    await expect(page.getByRole("navigation", { name: "Vault pages", exact: true })).toHaveCount(0);
    await expect(page.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
    await expect(page.getByRole("separator", { name: "Resize tree panel", exact: true })).toHaveCount(0);
    if (route === "settings") {
      for (const region of ["Vault details", "Access permissions", "Vault guide", "Danger zone"]) await expect(page.getByRole("region", { name: region, exact: true })).toBeVisible();
      await expect(page.getByRole("tablist")).toHaveCount(0);
      await expect(page.getByRole("complementary", { name: "Vault details" })).toHaveCount(0);
    } else if (route === "members") await expect(page.getByRole("table", { name: "Vault members" })).toBeVisible();
    else if (route === "publications") await expect(page.getByText("No public links yet", { exact: true })).toBeVisible();
    else if (route === "graph") await expect(page.getByRole("region", { name: "Graph exploration workspace" })).toBeVisible();
    else await expect(page.getByRole("region", { name: "Activity", exact: true })).toBeVisible();

    const panel = page.locator("#vault-workspace-navigation");
    await expect(panel).toHaveCount(1);
    if (width < 1024) {
      await expect(panel).toBeHidden();
      await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
    }
    await expect(panel).toBeVisible();
    await expect(page.getByRole("button", { name: "Close vault navigation", exact: true })).toHaveCount(1);
    await expect(panel.locator('[data-slot="vault-identity-header"]')).toHaveCount(1);
    await expect(panel.locator('[data-slot="collection-identity-header"]')).toHaveCount(0);
    const explorer = panel.getByRole("navigation", { name: "fixture explorer", exact: true });
    await expect(explorer).toBeVisible();
    await expect(explorer.getByRole("button", { name: "guides", exact: true })).toHaveAttribute("aria-expanded", "false");
    await expect(panel.getByRole("tree")).toHaveCount(0);
    await expect(panel.getByRole("button", { name: "Add content", exact: true })).toHaveCount(1);
    await expect(panel.getByRole("searchbox", { name: "Filter resources", exact: true })).toBeVisible();

    const picker = panel.getByRole("button", { name: "Switch vault: fixture", exact: true });
    await picker.click();
    await expect(page.getByRole("searchbox", { name: "Find a vault", exact: true })).toBeFocused();
    await expect(page.getByRole("navigation", { name: "Accessible vaults", exact: true }).getByRole("link", { name: "fixture, current vault", exact: true })).toHaveAttribute("href", "/vault/fixture");
    await page.keyboard.press("Escape");
    await expect(picker).toBeFocused();
    await expect(panel).toBeVisible();

    await expect(panel.getByRole("button", { name: "Vault actions for fixture", exact: true })).toHaveCount(0);
    await expect(panel.getByRole("link", { name: "Back to all vaults", exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Back to vault fixture", exact: true })).toHaveAttribute("href", "/vault/fixture");
    await expect(panel.getByRole("button", { name: "Close vault navigation", exact: true })).toBeVisible();
    if (width < 1024) {
      await expect(page.getByRole("separator", { name: "Resize vault navigation", exact: true })).toHaveCount(0);
      // Escape on an ordinary collection disclosure closes the outer drawer.
      await explorer.getByRole("button", { name: "guides", exact: true }).focus();
      await page.keyboard.press("Escape");
      await expect(panel).toBeHidden();
      await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toBeFocused();
    } else {
      const resize = page.getByRole("separator", { name: "Resize vault navigation", exact: true });
      await expect(resize).toHaveCount(1);
      await expect(resize).toHaveAttribute("aria-valuemin", "224");
      await expect(resize).toHaveAttribute("aria-valuemax", "360");
      await expect(resize).toHaveAttribute("aria-valuenow", "286");
      const navigation = (await panel.boundingBox())!;
      expect(navigation.width).toBe(286);
      const header = (await page.locator(".app-header").boundingBox())!;
      expect(Math.abs(header.x - navigation.x - navigation.width - 1)).toBeLessThan(2);
    }
    await page.screenshot({ path: testInfo.outputPath(`${route}.png`), animations: "disabled" });

    // Keep context available while breadcrumbs describe only location.
    if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
    await picker.getByText("fixture", { exact: true }).click();
    await page.getByRole("navigation", { name: "Accessible vaults", exact: true }).getByRole("link", { name: "fixture, current vault", exact: true }).click();
    await expect(page).toHaveURL(/\/vault\/fixture$/);
    await expect(trail.locator('[aria-current="page"]')).toHaveText("fixture");
    // Overview owns scoped task discovery after removing the duplicate menu.
    const destination = route === "activity"
      ? page.getByRole("link", { name: "Full commit log", exact: true })
      : page.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link", { name: label, exact: true });
    await destination.focus();
    await page.keyboard.press("Enter");
    await expect(trail.locator('[aria-current="page"]')).toHaveText(label);
    await expect(page.locator("#main")).toBeFocused();
    if (width < 1024) await expect(panel).toBeHidden();
    else await expect(explorer).toBeVisible();
    await expect.poll(() => page.evaluate(() => document.body.style.overflow)).not.toBe("hidden");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.evaluate(() => localStorage.getItem("akb.treeVisible"))).toBe("1");
    expect(await page.evaluate(() => localStorage.getItem("akb.focusedNavigationWidth.v1"))).toBe("286");
  });
}

for (const navigationVisible of ["0", "1"]) test(`Navigation choice ${navigationVisible} survives page changes, history and reload`, async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await fixture(page, false, navigationVisible);
  await page.goto("/vault/fixture/settings");
  const trail = page.getByRole("navigation", { name: "Current page", exact: true });
  const panel = page.locator("#vault-workspace-navigation");
  const checkPreference = async (label: string) => {
    await expect(trail.locator('[aria-current="page"]')).toHaveText(label);
    if (navigationVisible === "1") await expect(panel).toBeVisible();
    else await expect(panel).toBeHidden();
    await expect(panel).toHaveCSS("width", "286px");
  };
  await checkPreference("Settings");
  await trail.getByRole("link", { name: "Back to fixture", exact: true }).click();
  await checkPreference("fixture");
  await page.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link", { name: "Members", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeFocused();
  await checkPreference("Members");
  await page.goBack();
  await checkPreference("fixture");
  await page.goBack();
  await checkPreference("Settings");
  await page.goForward();
  await checkPreference("fixture");
  await page.goForward();
  await checkPreference("Members");
  await page.reload();
  await checkPreference("Members");
  expect(await page.evaluate(() => localStorage.getItem("akb.treeVisible"))).toBe(navigationVisible);
  expect(await page.evaluate(() => localStorage.getItem("akb.treeWidth.v2"))).toBe("320");
  expect(await page.evaluate(() => localStorage.getItem("akb.vaultRailWidth.v2"))).toBe("218");

  const toggle = page.getByRole("button", { name: navigationVisible === "1" ? "Close vault navigation" : "Open vault navigation", exact: true });
  await expect(toggle).toHaveCount(1);
  await toggle.click();
  const next = navigationVisible === "1" ? "0" : "1";
  await expect.poll(() => page.evaluate(() => localStorage.getItem("akb.treeVisible"))).toBe(next);
  await page.reload();
  if (next === "1") await expect(panel).toBeVisible();
  else await expect(panel).toBeHidden();
  await expect(panel).toHaveCSS("width", "286px");
});

for (const width of [375, 1920]) test(`Vault directory keeps a populated navigation panel at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  await fixture(page);
  await page.goto("/vault");
  await expect(page.getByRole("heading", { name: "Vaults", exact: true, level: 1 })).toBeVisible();
  const directory = page.getByRole("list", { name: "Available vaults", exact: true });
  await expect(directory.getByRole("link")).toHaveAttribute("href", "/vault/fixture");
  const panel = page.locator("#vault-workspace-navigation");
  await expect(panel).toHaveCount(1);
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("navigation", { name: "Vaults", exact: true }).getByRole("link", { name: "fixture", exact: true })).toHaveAttribute("href", "/vault/fixture");
  await expect(panel.getByRole("searchbox", { name: "Find a vault in sidebar", exact: true })).toBeVisible();
  await expect(panel.getByRole("navigation", { name: "fixture explorer", exact: true })).toHaveCount(0);
  if (width < 1024) {
    await page.keyboard.press("Escape");
    await expect(panel).toBeHidden();
  } else await expect(page.getByRole("separator", { name: "Resize vault navigation", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await directory.getByRole("link").click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  await expect(page.locator("#vault-workspace-navigation")).toHaveCount(1);
});

for (const dark of [false, true]) test(`Responsive navigation keeps keyboard focus visible ${dark ? "dark" : "light"}`, async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 900 });
  await fixture(page, dark);
  await page.goto("/vault/fixture/settings");
  const picker = page.getByRole("button", { name: "Switch vault: fixture", exact: true });
  await picker.focus();
  await page.setViewportSize({ width: 320, height: 740 });
  const toggle = page.getByRole("button", { name: "Open vault navigation", exact: true });
  await expect(toggle).toBeFocused();
  await expect(page.locator("#vault-workspace-navigation")).toBeHidden();
  await toggle.click();
  const identity = page.locator('[data-slot="vault-identity-header"]').getByRole("button", { name: "Close vault navigation", exact: true });
  await expect(identity).toBeFocused();
  await page.setViewportSize({ width: 1920, height: 900 });
  await expect(identity).toBeFocused();
  await expect(page.locator("#vault-workspace-navigation")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
