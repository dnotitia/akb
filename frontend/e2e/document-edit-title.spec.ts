import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");

const documentPath = "guides/team/operations/reading.md";
const documentTitle = "Team operating guide";
const documentBody = Array.from({ length: 30 }, (_, index) => `## Section ${index + 1}\n\nA practical guide to keeping shared knowledge current.`).join("\n\n");

async function fixture(page: Page, dark: boolean) {
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "document-edit-title-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, { dark });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (pathname.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "title-fixture", username: "fixture", display_name: "Title reviewer", email: "reader@example.invalid", auth_method: "local", is_admin: false } });
    if (pathname.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "title-vault", name: "fixture", role: "writer" }] } });
    if (pathname.endsWith("/info")) return route.fulfill({ json: { name: "fixture", role: "writer", is_archived: false, is_external_git: false, document_count: 1 } });
    if (pathname.includes("/browse/")) return route.fulfill({ json: { items: [
      { type: "collection", path: "guides", name: "Guides" },
      { type: "collection", path: "guides/team", name: "Team practices" },
      { type: "collection", path: "guides/team/operations", name: "Operations" },
      { type: "document", path: documentPath, name: documentTitle },
    ] } });
    if (pathname.includes("/documents/fixture/") && !pathname.endsWith("/history")) return route.fulfill({ json: {
      uri: `akb://fixture/coll/guides/team/operations/doc/reading.md`, path: documentPath,
      title: documentTitle, content: documentBody, status: "active", current_commit: "aaaaaaaaaaaaa",
      updated_at: "2026-09-14T09:42:30Z", created_at: "2026-08-25T01:05:00Z", tags: [], is_public: false,
    } });
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
}

for (const scenario of [
  { width: 1440, height: 900, dark: false },
  { width: 1440, height: 900, dark: true },
  { width: 375, height: 812, dark: false },
  { width: 667, height: 375, dark: false },
]) test(`Edit keeps the title visible at ${scenario.width}x${scenario.height} ${scenario.dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize(scenario);
  await fixture(page, scenario.dark);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(documentPath)}`);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Document body (markdown)", exact: true })).toBeVisible();
  const title = page.getByRole("textbox", { name: "Document title", exact: true });
  await page.screenshot({ path: testInfo.outputPath("edit-title.png") });
  const titleBounds = (await title.boundingBox())!;
  const canvasBounds = (await page.locator("#document-reading-canvas").boundingBox())!;
  await testInfo.attach("initial-edit-position", { body: JSON.stringify({ titleBounds, canvasBounds, scrollTop: await page.locator("#document-reading-canvas").evaluate(element => element.scrollTop) }), contentType: "application/json" });
  expect(titleBounds.y).toBeGreaterThanOrEqual(canvasBounds.y);
  expect(titleBounds.y + titleBounds.height).toBeLessThanOrEqual(canvasBounds.y + canvasBounds.height);
  expect(await page.locator("#document-reading-canvas").evaluate(element => element.scrollTop)).toBe(0);
  await expect(title).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  if (scenario.width >= 1000) {
    await page.locator("#document-reading-canvas").evaluate(element => { element.scrollTop = 600; });
    const toolbar = page.getByRole("toolbar", { name: "Text formatting" });
    await expect(toolbar).toBeInViewport({ ratio: 1 });
    const toolbarBounds = (await toolbar.boundingBox())!;
    expect(Math.abs(toolbarBounds.y - canvasBounds.y)).toBeLessThanOrEqual(2);
    await expect(page.getByRole("button", { name: "Editor mode: Visual" })).toBeInViewport({ ratio: 1 });
  }
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toBeFocused();
});
