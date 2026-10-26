import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures; no account data is changed.");

async function fixture(page: Page, dark: boolean) {
  await page.addInitScript(dark => {
    localStorage.setItem("akb_token", "admin-density-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    localStorage.setItem("akb.treeVisible", "1");
  }, dark);
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    let json: object = { items: [], vaults: [], history: [], templates: [], relations: [], unread_count: 0 };
    if (path.endsWith("/auth/config")) json = { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
    else if (path.endsWith("/auth/me")) json = { user_id: "density-owner", username: "owner", display_name: "Review owner", email: "owner@example.invalid", auth_method: "local", is_admin: false };
    else if (path.endsWith("/vaults")) json = { vaults: [{ id: "density-vault", name: "fixture", role: "owner" }] };
    else if (path.endsWith("/info")) json = { name: "fixture", role: "owner", role_source: "member", public_access: "none", description: "Shared operating knowledge.", document_count: 9, collection_count: 1, table_count: 0, file_count: 0, member_count: 9 };
    else if (path.endsWith("/members")) json = { members: Array.from({ length: 9 }, (_, index) => ({ id: index ? `member-${index}` : "density-owner", username: index ? `member-${index}` : "owner", display_name: index ? `Member ${index}` : "Review owner", email: `member${index}@example.invalid`, role: index ? "reader" : "owner", account_kind: "human", account_status: "active", since: "2026-09-15T00:00:00Z" })) };
    else if (path.includes("/publications/")) json = { publications: Array.from({ length: 9 }, (_, index) => ({ slug: `density-link-${index}`, share_url: `https://example.invalid/p/density-link-${index}`, resource_type: "document", resource_uri: `akb://fixture/coll/guides/doc/document-${index}.md`, vault: "fixture", title: index ? `Operating guide ${index}` : "Architecture decisions and operating guidance for the platform team", mode: "live", view_count: index, expires_at: null, max_views: null, password_protected: false, created_at: "2026-09-15T00:00:00Z" })) };
    else if (path.includes("/documents/fixture/")) json = { id: "guide", path: "overview/vault-skill.md", title: "Vault guide", content: "# Vault guide\n\nKeep decisions with their context.", current_commit: "guide-head", metadata: {}, status: "active" };
    else if (path.endsWith("/help/skill-template")) return route.fulfill({ contentType: "text/plain", body: "# {vault} Guide\n\nStarter template." });
    else if (path.includes("/browse/")) json = { items: [{ type: "collection", name: "guides", path: "guides", doc_count: 9 }] };
    return route.fulfill({ json });
  });
}

for (const width of [375, 1024, 1920, 2560]) for (const dark of [false, true]) for (const route of ["publications", "members", "settings"] as const) {
  test(`${route} shares readable compact workspace at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
    await fixture(page, dark);
    let overviewBox: { x: number; y: number; width: number; height: number } | null = null;
    if (width >= 1920) {
      await page.goto("/vault/fixture");
      const overview = page.getByRole("region", { name: "fixture Vault overview", exact: true });
      await expect(overview).toBeVisible();
      overviewBox = await overview.boundingBox();
    }
    await page.goto(`/vault/fixture/${route}`);
    const workspace = page.locator(".workspace-density-compact");
    await expect(workspace).toBeVisible();
    const viewport = page.locator('[data-slot="vault-route-viewport"]');
    const workspaceBox = (await workspace.boundingBox())!;
    const viewportBox = (await viewport.boundingBox())!;
    const viewportWidth = await viewport.evaluate(element => element.clientWidth);
    const gutter = width >= 1280 ? 32 : width >= 640 ? 24 : 16;
    const expectedWidth = Math.min(1280, viewportWidth - gutter * 2);
    const expectedInset = (viewportWidth - expectedWidth) / 2;
    expect(workspaceBox.width).toBe(expectedWidth);
    expect(workspaceBox.x - viewportBox.x).toBe(expectedInset);
    expect(viewportBox.x + viewportWidth - workspaceBox.x - workspaceBox.width).toBe(expectedInset);
    expect(workspaceBox.y - viewportBox.y).toBe(20);
    if (overviewBox) {
      expect(workspaceBox.x).toBe(overviewBox.x);
      expect(workspaceBox.width).toBe(overviewBox.width);
      expect(workspaceBox.y).toBe(overviewBox.y);
    }
    const header = workspace.locator('[data-slot="workspace-section-header"]');
    await expect(header).toBeVisible();

    if (route === "publications") {
      const list = page.getByRole("list", { name: "Public links", exact: true });
      await expect(list.getByRole("listitem")).toHaveCount(9);
      const row = list.getByRole("listitem").first();
      const content = (await row.locator(":scope > div").first().boundingBox())!;
      const actions = (await row.locator(":scope > div").last().boundingBox())!;
      if (workspaceBox.width < 672) expect(actions.y).toBeGreaterThanOrEqual(content.y + content.height);
      else expect(actions.x).toBeGreaterThanOrEqual(content.x + content.width);
      await expect(page.getByRole("searchbox", { name: "Filter public links", exact: true })).toBeVisible();
      const copy = row.getByRole("button", { name: /Copy/ }).first();
      expect((await copy.boundingBox())!.height).toBeGreaterThanOrEqual(width < 640 ? 44 : 32);
    } else if (route === "members") {
      const roster = page.getByRole("table", { name: "Vault members", exact: true });
      await expect(roster).toBeVisible();
      await expect(page.getByRole("searchbox", { name: "Filter members", exact: true })).toBeVisible();
      const first = roster.locator("tbody tr").first();
      if (workspaceBox.width >= 512) expect((await first.boundingBox())!.height).toBeLessThanOrEqual(68);
      expect((await page.getByRole("button", { name: "Invite member", exact: true }).boundingBox())!.height).toBeGreaterThanOrEqual(width < 640 ? 44 : 32);
    } else {
      const description = page.getByRole("textbox", { name: "Description", exact: true });
      await expect(description).toBeVisible();
      expect((await description.boundingBox())!.height).toBeGreaterThanOrEqual(80);
      expect((await page.getByTestId("settings-access-editor").boundingBox())!.width).toBeLessThanOrEqual(1024);
      await description.fill("Unsaved layout review");
      const save = page.getByRole("button", { name: "Save changes", exact: true });
      await expect(save).toBeEnabled();
      expect((await save.boundingBox())!.height).toBeGreaterThanOrEqual(width < 640 ? 44 : 32);
      await page.getByRole("button", { name: "Discard", exact: true }).click();
      await expect(description).toHaveValue("Shared operating knowledge.");
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`${route}-density.png`), animations: "disabled", fullPage: true });
  });
}
