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
    else if (path === "/api/v1/publications") json = { publications: [], total: 0, complete: true, next_cursor: null };
    else if (path.includes("/publications/")) json = { publications: Array.from({ length: 9 }, (_, index) => ({ slug: `density-link-${index}`, share_url: `https://example.invalid/p/density-link-${index}`, resource_type: "document", resource_uri: `akb://fixture/coll/guides/doc/document-${index}.md`, vault: "fixture", title: index ? `Operating guide ${index}` : "Architecture decisions and operating guidance for the platform team", mode: "live", view_count: index, expires_at: null, max_views: null, password_protected: false, created_at: "2026-09-15T00:00:00Z" })) };
    else if (path.includes("/documents/fixture/")) json = { id: "guide", path: "overview/vault-skill.md", title: "Vault guide", content: "# Vault guide\n\nKeep decisions with their context.", current_commit: "guide-head", metadata: {}, status: "active" };
    else if (path.endsWith("/help/skill-template")) return route.fulfill({ contentType: "text/plain", body: "# {vault} Guide\n\nStarter template." });
    else if (path.includes("/browse/")) json = { path: "", complete: true, next_cursor: null, items: [{ type: "collection", name: "guides", path: "guides", doc_count: 9 }] };
    return route.fulfill({ json });
  });
}

for (const width of [375, 1024, 1920, 2560]) for (const dark of [false, true]) {
  test(`Directory, Overview and global Publications share page identity and gutters at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
    await fixture(page, dark);
    for (const route of ["/vault", "/vault/fixture", "/publications"]) {
      await page.goto(route);
      const directory = route === "/vault";
      const global = route === "/publications";
      const frame = page.locator(global ? ".workspace-density-compact" : directory ? ".vault-directory" : ".vault-overview");
      const heading = frame.getByRole("heading", { level: 1 });
      await expect(heading).toBeVisible();
      await expect(heading).toHaveCSS("font-size", "20px");
      await expect(heading).toHaveCSS("line-height", "28px");
      await expect(heading).toHaveCSS("font-weight", "600");
      if (!directory) {
        const mark = frame.locator(global ? ".workspace-route-mark" : ".vault-overview-title > .vault-library-mark");
        await expect(mark).toHaveCSS("width", "36px");
        await expect(mark).toHaveCSS("height", "36px");
        await expect(mark.locator("svg")).toHaveCSS("width", "20px");
        await expect(mark.locator("svg")).toHaveCSS("height", "20px");
      }
      const viewport = page.locator(global ? "#main" : '[data-slot="vault-route-viewport"]');
      const box = (await frame.boundingBox())!;
      const origin = (await viewport.boundingBox())!;
      const available = await viewport.evaluate(element => element.clientWidth);
      const gutter = width >= 1280 ? 32 : width >= 640 ? 24 : 16;
      const expectedWidth = Math.min(1280, available - gutter * 2);
      expect(box.width).toBe(expectedWidth);
      expect(box.x - origin.x).toBe((available - expectedWidth) / 2);
      expect(box.y - origin.y).toBe(20);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`${directory ? "directory" : global ? "publications" : "overview"}-identity.png`), animations: "disabled", fullPage: true });
    }
  });
}

for (const width of [375, 1024, 1920, 2560]) for (const dark of [false, true]) for (const route of ["publications", "members", "settings"] as const) {
  test(`${route} shares readable compact workspace at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
    await fixture(page, dark);
    await page.goto(`/vault/fixture/${route}`);
    const workspace = page.locator(".workspace-density-compact");
    await expect(workspace).toBeVisible();
    const viewport = page.locator('[data-slot="vault-route-viewport"]');
    const workspaceBox = (await workspace.boundingBox())!;
    const viewportBox = (await viewport.boundingBox())!;
    const viewportWidth = await viewport.evaluate(element => element.clientWidth);
    if (route === "settings") {
      expect(Math.abs(workspaceBox.width - viewportWidth)).toBeLessThanOrEqual(1);
      expect(Math.abs(workspaceBox.x - viewportBox.x)).toBeLessThanOrEqual(1);
      const header = page.getByTestId("vault-settings-header");
      await expect(header).toBeVisible();
      const headerBox = (await header.boundingBox())!;
      expect(headerBox.x - workspaceBox.x).toBeGreaterThanOrEqual(16);
      const leftInset = headerBox.x - workspaceBox.x;
      const rightInset = workspaceBox.x + workspaceBox.width - headerBox.x - headerBox.width;
      expect(Math.abs(leftInset - rightInset)).toBeLessThanOrEqual(1);
      expect(headerBox.y - workspaceBox.y).toBeGreaterThanOrEqual(16);
      const panel = page.getByRole("tabpanel");
      const panelBox = (await panel.boundingBox())!;
      expect(panelBox.width).toBeLessThanOrEqual(1280);
      expect(Math.abs(panelBox.x - headerBox.x)).toBeLessThanOrEqual(1);
      expect(Math.abs(panelBox.width - headerBox.width)).toBeLessThanOrEqual(1);
      await expect(header.getByRole("heading")).toHaveCSS("font-size", "20px");
    } else {
      const gutter = width >= 1280 ? 32 : width >= 640 ? 24 : 16;
      const expectedWidth = Math.min(1280, viewportWidth - gutter * 2);
      const expectedInset = (viewportWidth - expectedWidth) / 2;
      expect(workspaceBox.width).toBe(expectedWidth);
      expect(workspaceBox.x - viewportBox.x).toBe(expectedInset);
      expect(viewportBox.x + viewportWidth - workspaceBox.x - workspaceBox.width).toBe(expectedInset);
      expect(workspaceBox.y - viewportBox.y).toBe(20);
      const header = workspace.locator('[data-slot="workspace-page-header"]');
      await expect(header).toBeVisible();
      await expect(header.getByRole("heading", { level: 1 })).toHaveCSS("font-size", "20px");
      await expect(header.getByRole("heading", { level: 1 })).toHaveCSS("line-height", "28px");
      await expect(header.locator(".workspace-route-mark")).toHaveCSS("width", "36px");
      await expect(header.locator(".workspace-route-mark > svg")).toHaveCSS("width", "20px");
    }

    if (route === "publications") {
      const list = page.getByRole("list", { name: "Publications", exact: true });
      await expect(list.getByRole("listitem")).toHaveCount(9);
      const row = list.getByRole("listitem").first();
      const content = (await row.locator(":scope > div").first().boundingBox())!;
      const actions = (await row.locator(":scope > div").last().boundingBox())!;
      if (workspaceBox.width < 672) expect(actions.y).toBeGreaterThanOrEqual(content.y + content.height);
      else expect(actions.x).toBeGreaterThanOrEqual(content.x + content.width);
      await expect(page.getByRole("searchbox", { name: "Filter publications", exact: true })).toBeVisible();
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
      expect((await page.getByTestId("settings-general-editor").boundingBox())!.width).toBeLessThanOrEqual(1024);
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
