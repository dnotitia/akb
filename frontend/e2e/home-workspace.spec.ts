import { expect, test } from "@playwright/test";

test("Personal navigation imports favorites and restores a browser draft", async ({ page }, testInfo) => {
  test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.addInitScript(() => {
    localStorage.setItem("akb_token", "personal-nav-fixture");
    localStorage.setItem("akb_theme", "light");
    localStorage.setItem("akb-vault-favorites", JSON.stringify(["team-id", "revoked-id"]));
    localStorage.setItem("akb.workspaceShortcuts.v1:personal-user", JSON.stringify([{ kind: "collection", vault: "team", path: "guides", title: "Guides" }]));
    localStorage.setItem("akb.recentDocumentViews.v1:personal-user", JSON.stringify([{ vault: "team", path: "guides/a.md", title: "Recently read guide", type: "note", viewedAt: new Date().toISOString() }]));
    localStorage.setItem("akb:document-draft:account:personal-user:team", JSON.stringify({ version: 1, userId: "personal-user", vault: "team", collection: "guides", title: "A recoverable draft", body: "Unsaved writing remains available.", type: "note", domain: "", summary: "", tags: [], assetIds: [], updatedAt: new Date().toISOString() }));
  });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "personal-user", username: "personal", display_name: "Personal User", is_admin: false, auth_method: "local" } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "team-id", name: "team", role: "owner" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: { name: "team", role: "owner", document_count: 0 } });
    if (path.endsWith("/notifications/unread-count")) return route.fulfill({ json: { supported: true, unread_count: 3, snapshot: "test", retention_days: 90 } });
    return route.fulfill({ json: { tokens: [], items: [], changes: [], templates: [] } });
  });
  await page.goto("/");
  const sidebar = page.getByTestId("app-sidebar");
  await sidebar.getByRole("button", { name: "Import saved favorites" }).click();
  const confirmation = page.getByRole("dialog", { name: "Import saved favorites?" });
  await expect(confirmation.getByText("team", { exact: true })).toBeVisible();
  await confirmation.getByRole("button", { name: "Import favorites", exact: true }).click();
  await expect(sidebar.getByText("Favorites", { exact: true })).toBeVisible();
  await expect(sidebar.getByRole("navigation", { name: "Favorite vaults" }).getByRole("link", { name: "team", exact: true })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: /Inbox/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Notifications, 3 unread", exact: true })).toBeVisible();
  await sidebar.locator("summary").filter({ hasText: "Recently viewed" }).click();
  await expect(sidebar.getByRole("link", { name: "Recently read guide" })).toBeVisible();
  await sidebar.locator("summary").filter({ hasText: "Pinned" }).click();
  await expect(sidebar.getByRole("link", { name: "Guides", exact: true })).toHaveAttribute("href", "/vault/team?collection=guides");
  await sidebar.getByRole("button", { name: "Help", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Getting around AKB" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sidebar.getByRole("button", { name: "Help", exact: true })).toBeFocused();
  await sidebar.locator("summary").filter({ hasText: "Drafts" }).click();
  await page.screenshot({ path: testInfo.outputPath("personal-sidebar.png"), fullPage: true });
  await sidebar.getByRole("link", { name: "A recoverable draft" }).click();
  await expect(page.getByPlaceholder("Document title", { exact: true })).toHaveValue("A recoverable draft");
  await expect(page.getByText("Unsaved writing remains available.", { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("restored-draft.png"), fullPage: true });
});

for (const width of [2560, 1440, 768, 375]) for (const dark of [false, true]) {
  test(`Home workspace ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures, not MSW.");
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
    const vaults = ["platform", "research", "team-handbook", "customer-knowledge"]
      .map((name, index) => ({ id: `vault-${index}`, name, role: index === 1 ? "reader" : "writer", description: ["Deployment decisions and operating guides.", "Research reports and technical comparisons.", "The team's processes, policies, and getting-started guides.", "Customer questions and product knowledge."][index] }));
    await page.addInitScript(({ vaults, dark }) => {
      localStorage.setItem("akb_token", "home-workspace-fixture");
      localStorage.setItem("akb_theme", dark ? "dark" : "light");
      localStorage.setItem("akb-vault-favorites:v2:workspace-user", JSON.stringify(vaults.map(v => v.id)));
      localStorage.setItem("akb.recentDocumentViews.v1:workspace-user", JSON.stringify(vaults.map((vault, index) => ({
        vault: vault.name, path: "guides/start.md", title: ["Production deployment and recovery checklist", "검색 품질 개선을 위한 실험 결과와 후속 계획", "Welcome to the team", "Getting started with AKB"][index],
        type: "note", viewedAt: new Date(Date.now() - index * 3600000).toISOString(),
      }))));
    }, { vaults, dark });
    let empty = false;
    await page.route("**/health/**", route => route.fulfill({ json: {} }));
    await page.route("**/api/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
      if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "workspace-user", username: "fixture", display_name: "임근우", email: "fixture@example.invalid", is_admin: false, auth_method: "local" } });
      if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: empty ? [] : vaults } });
      if (path.endsWith("/info")) return route.fulfill({ json: { document_count: 24, table_count: 2, file_count: 3, role: "writer" } });
      if (path.endsWith("/recent")) return route.fulfill({ json: { scope: new URL(route.request().url()).searchParams.get("scope") || "all", next_cursor: null, changes: empty ? [] : vaults.map((vault, i) => ({
        doc_id: `doc-${i}`, title: ["Updated deployment checklist", "Research notes — retrieval quality", "Team onboarding guide", "Frequently asked questions"][i],
        vault: vault.name, path: "guides/start.md", changed_at: new Date().toISOString(), updated_by_name: "Mina Park",
        excerpt: "A short document preview helps you recognize the topic before opening it.",
      })) } });
      if (path.endsWith("/notifications/unread-count")) return route.fulfill({ json: { supported: true, unread_count: 0, snapshot: "fixture", retention_days: 90 } });
      if (path.endsWith("/vaults/templates")) return route.fulfill({ json: [] });
      return route.fulfill({ json: { tokens: [], templates: [], items: [], changes: [] } });
    });
    await page.goto("/");
    if (width >= 1024) {
      await expect(page.getByRole("navigation", { name: "Current page", exact: true })).toHaveText("Home");
    }
    await page.evaluate(value => document.documentElement.classList.toggle("dark", value), dark);
    const viewed = page.getByRole("region", { name: "Recently viewed" });
    const directory = page.getByRole("region", { name: "Your vaults" });
    await expect(viewed.getByRole("link")).toHaveCount(4);
    await expect(directory.getByRole("heading", { level: 3 })).toHaveCount(4);
    await expect(directory.getByText("Documents", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Hide connection guide" })).toBeVisible();
    await expect(page.getByRole("tablist", { name: "Recent updates scope" })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Watched documents", exact: true })).toBeVisible();
    const homePaper = await page.locator("#main").evaluate(element => getComputedStyle(element.closest(".min-h-screen")!).backgroundColor);
    expect(homePaper).toBe(dark ? "rgb(18, 24, 33)" : "rgb(255, 255, 255)");
    if (width >= 1024) {
      const sidebar = page.getByTestId("app-sidebar");
      const title = page.getByRole("navigation", { name: "Current page", exact: true });
      const assertHomeAlignment = async () => {
        expect((await title.boundingBox())!.x - (await page.locator("header.app-header").boundingBox())!.x).toBe(20);
      };
      await assertHomeAlignment();
      const searchRight = (await page.locator("#global-search-trigger").boundingBox())!;
      const favorites = sidebar.getByRole("navigation", { name: "Favorite vaults" });
      await expect(favorites.getByRole("link")).toHaveCount(4);
      await sidebar.getByRole("button", { name: "Collapse favorite vaults" }).click();
      await expect(favorites).toBeHidden();
      await sidebar.getByRole("button", { name: "Expand favorite vaults" }).click();
      await expect(favorites).toBeVisible();
      const sidebarColor = dark ? "rgb(18, 24, 33)" : "rgb(255, 255, 255)";
      await expect(sidebar).toHaveCSS("background-color", sidebarColor);
      await expect(sidebar.getByRole("link", { name: "AKB home", exact: true })).toBeVisible();
      expect((await sidebar.boundingBox())!.y).toBe(0);
      const logoRow = (await sidebar.locator(":scope > div").first().boundingBox())!;
      const appHeader = (await page.locator("header.app-header").boundingBox())!;
      expect(logoRow.y + logoRow.height).toBe(appHeader.y + appHeader.height);
      expect((await page.locator("header.app-header").boundingBox())!.x).toBe(208);
      await expect(sidebar.getByRole("link", { name: "Home", exact: true })).toHaveCSS("color", dark ? "rgb(159, 212, 230)" : "rgb(0, 64, 89)");
      const search = sidebar.getByRole("link", { name: "Search", exact: true });
      await search.hover();
      await expect(search).toHaveCSS("background-color", dark ? "rgb(31, 41, 53)" : "rgb(240, 242, 245)");
      await search.focus();
      await expect(search).toBeFocused();
      await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
      await expect(sidebar).toHaveAttribute("data-compact", "true");
      await expect(sidebar).toHaveCSS("background-color", sidebarColor);
      await expect(sidebar.getByRole("link", { name: "AKB home", exact: true })).toBeVisible();
      await expect.poll(async () => (await page.locator("header.app-header").boundingBox())!.x).toBe(56);
      await assertHomeAlignment();
      const collapsedSearch = (await page.locator("#global-search-trigger").boundingBox())!;
      expect(collapsedSearch.x + collapsedSearch.width).toBe(searchRight.x + searchRight.width);
      await page.screenshot({ path: testInfo.outputPath("home-sidebar-collapsed.png"), fullPage: true });
      await page.getByRole("button", { name: "Expand sidebar", exact: true }).click();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const cards = directory.locator("li");
    const first = (await cards.nth(0).boundingBox())!;
    const fourth = (await cards.nth(3).boundingBox())!;
    if (width >= 1280) {
      expect(Math.abs(first.y - fourth.y)).toBeLessThan(2);
      expect(first.width).toBeGreaterThan(200);
    } else expect(fourth.y).toBeGreaterThan(first.y);
    expect((await viewed.boundingBox())!.y).toBeLessThan((await directory.boundingBox())!.y);
    await page.screenshot({ path: testInfo.outputPath("home-workspace.png"), fullPage: true });
    await page.getByRole("button", { name: "Set up a connection" }).click();
    const connection = page.getByRole("dialog", { name: "Connect an agent" });
    await expect(connection.getByLabel("AI tool", { exact: true })).toBeVisible();
    await connection.getByLabel("Access token", { exact: true }).click();
    await page.getByRole("menuitemradio", { name: "Use a saved token" }).click();
    await connection.getByLabel("Full saved token").fill("akb_fixture_example_only");
    await expect(connection.getByRole("heading", { name: "3. Try it in your agent" })).toBeVisible();
    await expect(connection.getByText(/This browser cannot verify/)).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("connection-setup.png"), fullPage: true });
    await page.keyboard.press("Escape");
    await expect(connection).not.toBeVisible();
    await page.getByRole("button", { name: "Hide connection guide" }).click();
    await expect(page.getByRole("button", { name: "Show connection guide" })).toBeFocused();
    await page.getByRole("button", { name: "Show connection guide" }).click();
    await expect(page.getByRole("button", { name: "Hide connection guide" })).toBeFocused();
    await page.addInitScript(() => {
      const key = "akb.recentDocumentViews.v1:workspace-user";
      const views = JSON.parse(localStorage.getItem(key) || "[]");
      localStorage.setItem(key, JSON.stringify(views.slice(0, 1)));
    });
    await page.reload();
    await expect(viewed.getByRole("link")).toHaveCount(1);
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toHaveClass("sr-only");
    const single = (await viewed.getByRole("link").boundingBox())!;
    expect(Math.abs(single.width - (await viewed.boundingBox())!.width)).toBeLessThan(4);
    await page.screenshot({ path: testInfo.outputPath("home-single-recent.png"), fullPage: true });
    empty = true;
    await page.reload();
    await expect(page.getByRole("heading", { name: "Create your first vault" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Recently viewed" })).toHaveCount(0);
    await page.getByRole("button", { name: "Create a vault", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Create a vault" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Create a vault", exact: true })).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("home-first-run.png"), fullPage: true });
    await page.goto("/settings?tab=tokens");
    if (width >= 1024) {
      await expect(page.getByRole("navigation", { name: "Current page", exact: true })).toHaveText("Agent connections");
    }
    await page.evaluate(value => document.documentElement.classList.toggle("dark", value), dark);
    await expect(page.getByRole("heading", { name: "Connect an agent" })).toBeVisible();
    await expect(page.getByLabel("AI tool", { exact: true })).toBeVisible();
    await expect(page.getByTestId("settings-workspace")).toHaveCSS("background-color", dark ? "rgb(18, 24, 33)" : "rgb(255, 255, 255)");
    if (width >= 1024) {
      const expectedNavigation = dark ? "rgb(18, 24, 33)" : "rgb(255, 255, 255)";
      await expect(page.getByTestId("app-sidebar")).toHaveCSS("background-color", expectedNavigation);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("settings-unchanged.png"), fullPage: true });
    if (width >= 1024) {
      empty = false;
      await page.goto("/vault/platform/search");
      const pageLocation = page.getByRole("navigation", { name: "Current page", exact: true });
      await expect(pageLocation).toHaveText("platform/Search");
      await expect(pageLocation.getByRole("link", { name: "platform" })).toHaveAttribute("href", "/vault/platform");
      await expect(page.getByRole("navigation", { name: "Breadcrumb", exact: true })).toHaveCount(0);
      const vaultList = page.getByRole("navigation", { name: "Vaults", exact: true });
      await expect(page.locator('[data-slot="workspace-sidebar-heading"]')).toHaveCSS("height", "40px");
      expect((await page.locator("#workspace-navigation").boundingBox())!.y).toBe(96);
      await page.getByRole("button", { name: "Expand sidebar", exact: true }).click();
      await expect(page.getByTestId("app-sidebar")).toHaveAttribute("data-compact", "false");
      await expect.poll(async () => (await vaultList.boundingBox())!.x).toBe(208);
      await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
      await expect.poll(async () => (await vaultList.boundingBox())!.x).toBe(56);
      await expect(vaultList).toBeVisible();
      expect((await vaultList.boundingBox())!.y).toBe(0);
      const switcher = vaultList.getByRole("button", { name: "Switch vault", exact: true });
      await expect(switcher).toHaveCSS("border-top-width", "0px");
      await expect(switcher.locator("svg")).toHaveCount(2);
      await switcher.click();
      await expect(page.getByRole("menuitemradio", { name: "research Favorite", exact: true })).toBeVisible();
      await page.keyboard.press("Escape");
      const identity = (await page.locator('[data-slot="vault-identity-header"]').boundingBox())!;
      expect((await vaultList.getByRole("button", { name: "New vault", exact: true }).boundingBox())!.y).toBeGreaterThanOrEqual(identity.y + identity.height);
      await page.getByRole("button", { name: "Show collection tree", exact: true }).click();
      const nav = page.locator("#vault-workspace-navigation");
      const bounds = (await nav.boundingBox())!;
      expect(bounds.y).toBe(0);
      await expect.poll(async () => {
        const current = (await nav.boundingBox())!;
        return Math.abs((await page.locator("header.app-header").boundingBox())!.x - (current.x + current.width + 1));
      }).toBeLessThanOrEqual(1);
      await expect(page.getByRole("separator", { name: "Resize tree panel" })).toHaveCSS("width", "1px");
      const handle = (await page.getByRole("separator", { name: "Resize tree panel" }).boundingBox())!;
      await page.mouse.move(handle.x - 2, handle.y + 140);
      await page.mouse.down();
      await page.mouse.move(handle.x + 30, handle.y + 140);
      await page.mouse.up();
      await expect.poll(async () => (await page.getByRole("separator", { name: "Resize tree panel" }).boundingBox())!.x).toBeGreaterThan(handle.x + 20);
      await page.screenshot({ path: testInfo.outputPath("vault-full-height-navigation.png"), fullPage: true });
      const titleRow = page.locator('nav[aria-label="Breadcrumb"]').locator("..");
      const titleBottom = (await titleRow.boundingBox())!;
      for (const selector of ['[data-slot="vault-management-row"]', '[data-slot="collection-management-row"]']) {
        const row = (await page.locator(selector).boundingBox())!;
        expect(row.y + row.height).toBe(titleBottom.y + titleBottom.height);
      }
      await page.getByRole("button", { name: "Minimize vault list to a rail", exact: true }).click();
      expect((await vaultList.boundingBox())!.width).toBe(56);
      expect((await vaultList.boundingBox())!.y).toBe(0);
      await page.getByRole("button", { name: "Expand vault list", exact: true }).click();
      await vaultList.getByRole("link").filter({ hasText: "research" }).first().click();
      await expect(page).toHaveURL(/\/vault\/research$/);
      await expect(vaultList).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.goto("/vault");
      await expect(vaultList).toBeVisible();
      const unselected = (await page.locator("#vault-workspace-navigation").boundingBox())!;
      expect(unselected.y).toBe(0);
      expect((await page.locator("header.app-header").boundingBox())!.x).toBe(unselected.x + unselected.width);
      await page.screenshot({ path: testInfo.outputPath("vault-unselected-lines.png"), fullPage: true });
    }
  });
}
