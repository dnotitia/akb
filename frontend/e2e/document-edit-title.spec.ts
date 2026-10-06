import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");

const documentPath = "guides/team/operations/reading.md";
const documentTitle = "Team operating guide";
const documentBody = Array.from({ length: 30 }, (_, index) => `## Section ${index + 1}\n\nA practical guide to keeping shared knowledge current.`).join("\n\n");

async function authoringScrollport(page: Page) {
  return page.locator(".document-authoring-writing").evaluateHandle(writing => {
    let element: HTMLElement | null = writing as HTMLElement;
    while (element) {
      if (["auto", "scroll"].includes(getComputedStyle(element).overflowY)) return element;
      element = element.parentElement;
    }
    throw new Error("The authoring canvas has no scroll owner");
  });
}

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
  { width: 2560, height: 1440, dark: false },
  { width: 2560, height: 1440, dark: true },
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
  const writingBounds = (await page.locator(".document-authoring-writing").boundingBox())!;
  const scrollport = await authoringScrollport(page);
  const canvasBounds = (await scrollport.boundingBox())!;
  // The fluid title follows the writing column's edge beside the details rail.
  const inset = scenario.width < 640 ? 16 : 24;
  expect(Math.abs(titleBounds.x - writingBounds.x - inset)).toBeLessThanOrEqual(1);
  expect(titleBounds.width).toBeGreaterThan(writingBounds.width - inset * 2 - 20);
  await testInfo.attach("initial-edit-position", { body: JSON.stringify({ titleBounds, writingBounds, canvasBounds, scrollTop: await scrollport.evaluate(element => element.scrollTop) }), contentType: "application/json" });
  expect(titleBounds.y).toBeGreaterThanOrEqual(canvasBounds.y);
  expect(titleBounds.y + titleBounds.height).toBeLessThanOrEqual(canvasBounds.y + canvasBounds.height);
  expect(await scrollport.evaluate(element => element.scrollTop)).toBe(0);
  await expect(title).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  if (scenario.width >= 1000) {
    await scrollport.evaluate(element => { element.scrollTop = 600; });
    const toolbar = page.getByRole("toolbar", { name: "Text formatting" });
    await expect(toolbar).toBeInViewport({ ratio: 1 });
    const toolbarBounds = (await toolbar.boundingBox())!;
    expect(Math.abs(toolbarBounds.y - canvasBounds.y)).toBeLessThanOrEqual(2);
    await expect(page.getByRole("button", { name: "Editor mode: Visual" })).toBeInViewport({ ratio: 1 });
  }
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toBeFocused();
});

for (const scenario of [
  { width: 1440, height: 900, dark: false },
  { width: 1440, height: 900, dark: true },
  { width: 375, height: 812, dark: false },
]) test(`Local draft feedback has breathing room at ${scenario.width}x${scenario.height} ${scenario.dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize(scenario);
  await fixture(page, scenario.dark);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(documentPath)}`);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Document body (markdown)", exact: true })).toBeVisible();
  await page.getByRole("textbox", { name: "Document title", exact: true }).fill("Updated operating guide");
  const notice = page.getByRole("status").filter({ hasText: "Draft saved locally" });
  await expect(notice).toHaveCount(1);
  const scrollport = await authoringScrollport(page);
  // On narrow canvases the details continue below this footer in the same flow.
  await scrollport.evaluate((element, feedback) => {
    if (!feedback) throw new Error("Draft feedback is missing");
    element.scrollTop += feedback.getBoundingClientRect().bottom - element.getBoundingClientRect().bottom;
  }, await notice.elementHandle());
  // Fractional line heights can round the footer box slightly past the scrollport.
  await expect(notice).toBeInViewport({ ratio: 0.99 });
  await page.screenshot({ path: testInfo.outputPath("draft-feedback.png") });
  const spacing = await notice.evaluate(element => {
    const bounds = element.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(element);
    const text = range.getBoundingClientRect();
    return {
      left: text.left - bounds.left,
      right: bounds.right - text.right,
      top: text.top - bounds.top,
      bottom: bounds.bottom - text.bottom,
      textBottom: text.bottom,
    };
  });
  expect(spacing.left).toBeGreaterThanOrEqual(scenario.width < 640 ? 16 : 24);
  expect(spacing.right).toBeGreaterThanOrEqual(16);
  expect(spacing.top).toBeGreaterThanOrEqual(12);
  expect(spacing.bottom).toBeGreaterThanOrEqual(12);
  const canvasBounds = (await scrollport.boundingBox())!;
  expect(canvasBounds.y + canvasBounds.height - spacing.textBottom).toBeGreaterThanOrEqual(12);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
