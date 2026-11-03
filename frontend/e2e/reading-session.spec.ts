import { expect, test } from "@playwright/test";
import { readingFixture } from "./fixtures/reading-workspace";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");
const sessionKey = "akb:reading-session:v1:studio-reader";
const content = {
  "reading.md": '# Reading workflow\n\n[Inspect reference](akb://fixture/doc/reference.md)\n\n' + Array.from({ length: 30 }, (_, index) => `## Reading section ${index}\n\n${"A reading position belongs to its own document and pane. ".repeat(10)}`).join("\n\n"),
  "reference.md": '# Reference notes\n\n' + Array.from({ length: 30 }, (_, index) => `## Reference section ${index}\n\n${"The side document retains independent context and scroll position. ".repeat(10)}`).join("\n\n"),
};
async function openSide(page: Parameters<typeof readingFixture>[0], top = 0) {
  await page.locator("#document-reading-canvas").getByRole("link", { name: "Inspect reference", exact: true }).click();
  const preview = page.getByRole("complementary", { name: "Reference notes", exact: true }).locator(".reading-preview-scroll");
  await expect(preview.getByRole("heading", { name: "Reference notes", exact: true })).toBeVisible();
  await preview.evaluate(() => new Promise(resolve => requestAnimationFrame(() => resolve(null))));
  await preview.evaluate((element, top) => { element.scrollTop = top; element.dispatchEvent(new Event("scroll")); }, top);
  await page.getByRole("button", { name: "Keep side by side", exact: true }).click();
}

test("restores tab order, active Raw view, both scroll positions and the side document", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await readingFixture(page, false, content);
  await page.goto("/vault/fixture/doc/reading.md");
  const main = page.locator("#document-reading-canvas");
  await expect(main.getByRole("heading", { name: "Reading workflow", exact: true })).toBeVisible();
  await openSide(page, 900);
  const side = page.getByRole("complementary", { name: "Side document: Reference notes", exact: true });
  await expect(side).toBeVisible();
  const originalCanvas = await main.elementHandle();
  const scroll = side.locator(".reading-preview-scroll");
  await expect.poll(() => scroll.evaluate(element => element.scrollTop)).toBe(900);
  await page.getByRole("group", { name: "Document reading tools", exact: true }).getByRole("radio", { name: "Raw markdown", exact: true }).click();
  await expect(main.getByTestId("doc-raw")).toBeVisible();
  await main.evaluate(() => new Promise(resolve => requestAnimationFrame(() => resolve(null))));
  await main.evaluate(element => { element.scrollTop = 650; element.dispatchEvent(new Event("scroll")); });
  await page.getByRole("button", { name: "Open document list", exact: true }).click();
  await page.getByRole("button", { name: "Tab actions for Reference notes", exact: true }).click();
    await page.getByRole("menuitem", { name: "Move Reference notes left", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect.poll(() => page.evaluate(key => JSON.parse(localStorage.getItem(key) || "{}").tabs?.find((row: { raw?: boolean }) => row.raw)?.rawTop, sessionKey)).toBe(650);
  const stored = await page.evaluate(key => localStorage.getItem(key)!, sessionKey);
  expect(stored).not.toContain("Reference notes"); expect(stored).not.toContain("reading position belongs");
  const splitter = page.getByRole("separator", { name: "Resize side document", exact: true });
  const before = Number(await splitter.getAttribute("aria-valuenow"));
  await splitter.focus(); await page.keyboard.press("ArrowLeft");
  expect(Number(await splitter.getAttribute("aria-valuenow"))).toBeGreaterThan(before);
  const sideTools = page.getByRole("group", { name: "Side document tools", exact: true });
  await sideTools.getByRole("button", { name: "Panel width", exact: true }).click();
  await page.getByRole("menuitem", { name: "Main wider", exact: true }).click();
  const bounds = (await splitter.boundingBox())!;
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + 120); await page.mouse.down(); await page.mouse.move(bounds.x + 100, bounds.y + 120); await page.mouse.up();
  const committedWidth = Number(await splitter.getAttribute("aria-valuenow"));
  const nextBounds = (await splitter.boundingBox())!;
  await page.mouse.move(nextBounds.x + nextBounds.width / 2, nextBounds.y + 120); await page.mouse.down(); await page.mouse.move(nextBounds.x + 60, nextBounds.y + 120);
  await page.keyboard.press("Escape"); await page.mouse.up();
  expect(Number(await splitter.getAttribute("aria-valuenow"))).toBe(committedWidth);
  expect(await originalCanvas!.evaluate(element => element.isConnected)).toBe(true);
  await page.reload();
  await expect(page.getByRole("tablist", { name: "Open documents", exact: true }).getByRole("tab").first()).toContainText("Reference notes");
  await expect(main.getByTestId("doc-raw")).toBeVisible();
  await expect(side).toBeVisible();
  await expect.poll(() => main.evaluate(element => element.scrollTop)).toBe(650);
  await expect.poll(() => scroll.evaluate(element => element.scrollTop)).toBe(900);
  await page.screenshot({ path: testInfo.outputPath("reading-session-restored.png") });
  await page.getByRole("tab", { name: /^Reference notes/ }).click();
  await expect(page).toHaveURL(/reference.md/);
  await expect(side).toBeVisible();
  await expect.poll(() => scroll.evaluate(element => element.scrollTop)).toBe(900);
});

test("adapts a saved side document to a narrow screen without opening a modal automatically", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 }); await readingFixture(page, false, content);
  await page.goto("/vault/fixture/doc/reading.md"); await openSide(page);
  const main = page.locator("#document-reading-canvas");
  await main.evaluate(element => { element.scrollTop = 500; element.dispatchEvent(new Event("scroll")); });
  await page.getByRole("complementary", { name: "Side document: Reference notes" }).locator(".reading-preview-scroll").evaluate(element => { element.scrollTop = 800; element.dispatchEvent(new Event("scroll")); });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("dialog", { name: "Side document", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Open side document", exact: true }).click();
  const sheet = page.getByRole("dialog", { name: "Side document", exact: true });
  await expect(sheet).toBeVisible();
  await expect.poll(() => sheet.locator(".reading-preview-scroll").evaluate(element => element.scrollTop)).toBe(800);
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Open side document", exact: true })).toBeFocused();
  await page.reload();
  await expect(page.getByRole("button", { name: "Open side document", exact: true })).toBeVisible();
  await expect(sheet).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
});

test("rechecks saved identifiers and removes revoked documents without cached labels", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 }); await readingFixture(page, false, content);
  await page.goto("/vault/fixture/doc/reading.md"); await openSide(page);
  await expect(page.getByRole("tab", { name: /^Reference notes/ })).toBeVisible();
  await page.route("**/api/v1/documents/fixture/reference.md*", route => route.fulfill({ status: 403, json: { detail: "Unavailable" } }));
  await page.reload();
  await expect(page.getByRole("tab", { name: "Reading workflow", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: /^Reference notes/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Open side document", exact: true })).toHaveCount(0);
  await expect.poll(() => page.evaluate(key => localStorage.getItem(key), sessionKey)).not.toContain("reference.md");
});

test("opens the current document beside Raw without changing the tab's saved view", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1000 }); await readingFixture(page, false, content);
  await page.goto("/vault/fixture/doc/reading.md?view=raw");
  await expect(page.getByTestId("doc-raw")).toBeVisible();
  await page.getByRole("group", { name: "Document reading tools", exact: true }).getByRole("button", { name: "Actions for Reading workflow", exact: true }).click();
  await page.getByRole("menuitem", { name: "Read side by side", exact: true }).click();
  await expect(page.getByRole("complementary", { name: "Side document: Reading workflow", exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(key => JSON.parse(localStorage.getItem(key) || "{}").tabs?.[0]?.raw, sessionKey)).toBe(true);
  await page.reload();
  await expect(page.getByTestId("doc-raw")).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Side document: Reading workflow", exact: true })).toBeVisible();
});

test("does not restore another account's tab identifiers", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 }); await readingFixture(page, false, content);
  await page.goto("/vault/fixture/doc/reading.md"); await openSide(page);
  await expect(page.getByRole("tab", { name: /^Reference notes/ })).toBeVisible();
  await page.route("**/api/v1/auth/me", route => route.fulfill({ json: { user_id: "another-reader", username: "another", display_name: "Another reader", auth_method: "local", email: "another@example.invalid", is_admin: false } }));
  await page.reload();
  await expect(page.getByRole("tab", { name: "Reading workflow", exact: true })).toBeVisible();
  await expect(page.getByRole("tab", { name: /^Reference notes/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Open side document", exact: true })).toHaveCount(0);
});
