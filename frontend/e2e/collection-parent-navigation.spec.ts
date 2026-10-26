import { expect, test } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");

const parentPath = "팀 자료 & 운영/설계 #1";
const selectedPath = `${parentPath}/child?`;
const target = (path: string) => `/vault/fixture?collection=${encodeURIComponent(path)}`;

for (const width of [375, 1440]) for (const dark of [false, true]) {
  test(`Collections return to the direct parent at ${width}px ${dark ? "dark" : "light"}`, async ({ page, context }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce", colorScheme: dark ? "dark" : "light" });
    await context.addInitScript(dark => {
      localStorage.setItem("akb_token", "collection-parent-fixture");
      localStorage.setItem("akb_theme", dark ? "dark" : "light");
    }, dark);
    await context.route("**/health/**", route => route.fulfill({ json: {} }));
    await context.route("**/api/v1/**", route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
      if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "parent-reader", username: "reader", email: "reader@example.invalid", display_name: "Reader", auth_method: "local", is_admin: false } });
      if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "fixture", name: "fixture", role: "reader" }] } });
      if (path.endsWith("/info")) return route.fulfill({ json: { name: "fixture", role: "reader", document_count: 1, collection_count: 4, table_count: 0, file_count: 0 } });
      if (path.includes("/browse/")) return route.fulfill({ json: { path: url.searchParams.get("collection") || "", complete: true, next_cursor: null, name_query: url.searchParams.get("name_query"), items: [
        { type: "collection", name: "guides", path: "guides" },
        { type: "collection", name: "팀 자료 & 운영", path: "팀 자료 & 운영" },
        { type: "collection", name: "설계 #1", path: parentPath },
        { type: "collection", name: "child?", path: selectedPath },
        { type: "document", name: "Runbook", path: `${selectedPath}/runbook.md` },
      ] } });
      if (path.includes("/documents/")) return route.fulfill({ status: 404, json: { detail: "No guide" } });
      return route.fulfill({ json: { items: [], changes: [], history: [], templates: [], unread_count: 0 } });
    });
    await page.goto(target(selectedPath));
    const workspace = page.getByRole("region", { name: "Collection workspace", exact: true });
    const trail = workspace.getByRole("navigation", { name: "Collection location", exact: true });
    const parent = trail.getByRole("link", { name: `Back to collection ${parentPath}`, exact: true });
    await expect(parent).toHaveAttribute("href", target(parentPath));
    await expect(trail.locator('[aria-current="page"]').getByRole("heading")).toHaveText("child?");
    const closeNavigation = page.getByRole("button", { name: "Close vault navigation", exact: true });
    if (await closeNavigation.isVisible()) await closeNavigation.click();
    await expect(trail.locator(".lucide-arrow-left")).toHaveCount(1);
    await expect(parent).toBeVisible();
    if (width < 640) expect((await parent.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    expect(await trail.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("collection-parent-trail.png"), animations: "disabled" });
    await parent.click();
    await expect(page).toHaveURL(target(parentPath));
    await expect(workspace.getByRole("heading", { name: "설계 #1", exact: true })).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(target(selectedPath));
    await expect(parent).toBeVisible();
    await page.goForward();
    await expect(page).toHaveURL(target(parentPath));
    await trail.getByRole("link", { name: "All collections", exact: true }).click();
    await workspace.getByRole("link", { name: "Open collection guides", exact: true }).click();
    await expect(workspace.getByRole("heading", { name: "guides", exact: true })).toBeVisible();
    await trail.getByRole("link", { name: "All collections", exact: true }).click();
    await workspace.getByRole("textbox", { name: "Search collections and content", exact: true }).fill("child?");
    await workspace.getByRole("link", { name: `Open collection ${selectedPath}`, exact: true }).click();
    await expect(parent).toBeVisible();
    await expect(workspace.getByRole("textbox", { name: "Search collections and content", exact: true })).toHaveValue("");
    await parent.click();
    await expect(page).toHaveURL(target(parentPath));
    if (width < 1024) await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toHaveAttribute("aria-expanded", "false");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const grandparent = trail.getByRole("link", { name: "Back to collection 팀 자료 & 운영", exact: true });
    const opened = context.waitForEvent("page");
    await grandparent.click({ modifiers: ["ControlOrMeta"] });
    const newPage = await opened;
    await expect(newPage).toHaveURL(target("팀 자료 & 운영"));
    await newPage.close();
    await expect(page).toHaveURL(target(parentPath));
  });
}
