import { expect, test, type Page } from "@playwright/test";
import axe from "axe-core";
import { readingFixture } from "./fixtures/reading-workspace";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");
const path = (index: number) => index === 0 ? "reading.md" : `chapter-${index}.md`;
const title = (index: number) => index === 0 ? "Reading workflow" : `Document ${index + 1}`;

async function setup(page: Page, count = 4, { side = false, edit = false, dark = false } = {}) {
  await readingFixture(page, dark);
  await page.addInitScript(({ paths, side }) => {
    const entry = (path: string, index: number) => ({ uri: `akb://fixture/doc/${path}`, top: index * 50, rawTop: index * 20, sideTop: 120 });
    localStorage.setItem("akb:reading-session:v1:studio-reader", JSON.stringify({ schema: 1,
      tabs: paths.map(entry), closed: [], active: entry(paths[0], 0), companion: side ? entry(paths[1], 1) : null,
    }));
  }, { paths: Array.from({ length: count }, (_, index) => path(index)), side });
  await page.route("**/api/v1/documents/fixture/**", route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/history") || url.pathname.endsWith("/relations")) return route.fallback();
    const docPath = decodeURIComponent(url.pathname.split("/documents/fixture/")[1]);
    const index = Array.from({ length: count }, (_, index) => path(index)).indexOf(docPath);
    if (index < 0) return route.fallback();
    return route.fulfill({ json: { uri: `akb://fixture/doc/${docPath}`, path: docPath, title: title(index),
      content: `# ${title(index)}\n\n` + "Reading paragraph with enough content to preserve the reading position.\n\n".repeat(70),
      current_commit: "aaaaaaaaaaaaa", status: "active", created_by_name: "Reading reviewer", tags: [],
    } });
  });
  await page.goto(`/vault/fixture/doc/reading.md${edit ? "?view=edit" : ""}`);
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveCount(count);
}

async function contextAction(page: Page, document: string, action: string) {
  await page.getByRole("tab", { name: document, exact: true }).click({ button: "right" });
  await page.getByRole("menuitem", { name: action, exact: true }).click();
}

test("closes all 30 tabs and the side pane, then restores the whole group from the Vault", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await setup(page, 30, { side: true });
  const canvas = page.locator("#document-reading-canvas");
  await canvas.evaluate(node => { node.scrollTop = 420; node.dispatchEvent(new Event("scroll")); });
  await page.getByRole("button", { name: "Open document list", exact: true }).click();
  await page.getByRole("button", { name: "Tab list actions", exact: true }).click();
  await page.getByRole("menuitem", { name: "Close all tabs", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveCount(0);
  await expect.poll(() => page.locator("#main").evaluate(node => node === document.activeElement || node.contains(document.activeElement))).toBe(true);
  await expect(page.getByRole("region", { name: "Closed document tabs" })).toContainText("Closed 30 tabs.");
  await expect.poll(() => page.evaluate(() => JSON.parse(localStorage.getItem("akb:reading-session:v1:studio-reader")!).companion)).toBeNull();
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveCount(30);
  await expect(page.getByRole("tab", { name: "Reading workflow", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect.poll(() => canvas.evaluate(node => node.scrollTop)).toBe(420);
  await expect(page.getByRole("button", { name: "Close side document", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Closed document tabs" })).toHaveCount(0);
  await page.goBack();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  await page.goForward();
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveCount(30);
});

test("an undo that exceeds the tab limit explains why without closing a new document", async ({ page }) => {
  await setup(page, 30);
  await contextAction(page, "Reading workflow", "Close all tabs");
  await page.getByRole("button", { name: "Dismiss tab close notice", exact: true }).click();
  await page.locator('a[href="/vault/fixture/doc/reference.md"]').first().click();
  await expect(page.getByRole("tab", { name: "Reference notes", exact: true })).toBeVisible();
  await contextAction(page, "Reference notes", "Undo close 30 tabs");
  await expect(page.getByRole("region", { name: "Closed document tabs" })).toContainText("Make room in the 30-tab workspace");
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveText(["Reference notes"]);
});

for (const [action, remaining] of [
  ["Close other tabs", ["Document 2"]],
  ["Close tabs to the left", ["Document 2", "Document 3", "Document 4"]],
  ["Close tabs to the right", ["Reading workflow", "Document 2"]],
] as const) test(`${action} uses the clicked tab and undo restores order`, async ({ page }) => {
  await setup(page);
  await contextAction(page, "Document 2", action);
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveText([...remaining]);
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveText(["Reading workflow", "Document 2", "Document 3", "Document 4"]);
  await expect(page.getByRole("tab", { name: "Reading workflow", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: "Reading workflow", exact: true })).toBeFocused();
});

test("closing the last tab returns to its Vault and can be undone", async ({ page }) => {
  await setup(page, 1);
  await page.getByRole("button", { name: "Close Reading workflow", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  await expect(page.locator("#main")).toBeFocused();
  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(page.getByRole("tab", { name: "Reading workflow", exact: true })).toBeFocused();
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveCount(1);
});

test("cancelling a dirty editor exit preserves every tab and the unsaved title", async ({ page }) => {
  await setup(page, 4, { edit: true });
  await page.getByRole("textbox", { name: "Document title", exact: true }).fill("Unsaved reader title");
  await contextAction(page, "Reading workflow", "Close all tabs");
  const guard = page.getByRole("dialog", { name: "Leave this document?", exact: true });
  await expect(guard).toBeVisible();
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveCount(4);
  await guard.getByRole("button", { name: "Keep editing", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Document title", exact: true })).toHaveValue("Unsaved reader title");
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveCount(4);
  await contextAction(page, "Document 2", "Close other tabs");
  await expect(guard).toBeVisible();
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveCount(4);
  await guard.getByRole("button", { name: "Leave document", exact: true }).click();
  await expect(page).toHaveURL(/\/doc\/chapter-1.md$/);
  await expect(page.locator('.reading-tabs [role="tab"]')).toHaveText(["Document 2"]);
  expect(await page.evaluate(() => Object.entries(localStorage).some(([key, value]) => key.includes("draft") && value.includes("Unsaved reader title")))).toBe(true);
});

for (const width of [390, 1440]) test(`common menus and undo stay usable at ${width}px`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 760 });
  await setup(page, 4, { dark: width === 390 });
  const tab = page.getByRole("tab", { name: "Reading workflow", exact: true });
  await tab.focus(); await page.keyboard.press("Shift+F10");
  const menu = page.getByRole("menu", { name: "Tab actions for Reading workflow", exact: true });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitem", { name: "Close tabs to the left", exact: true })).toHaveAttribute("data-disabled", "");
  await page.addScriptTag({ content: axe.source });
  const violations = await page.evaluate(async () => (await (window as unknown as { axe: typeof axe }).axe.run(document.querySelector('.tab-manager-action-menu')!, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"] },
  })).violations.map(result => result.id));
  expect(violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("tab-context-menu.png") });
  await menu.getByRole("menuitem", { name: "Close all tabs", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  const notice = page.getByRole("region", { name: "Closed document tabs" });
  await expect(notice.getByRole("button", { name: "Undo", exact: true })).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("closed-tabs-undo.png") });
});

test.describe("short touch viewport", () => {
  test.use({ viewport: { width: 812, height: 375 }, hasTouch: true, isMobile: true });
  test("keeps the last command reachable in both common menus", async ({ page }, testInfo) => {
    await setup(page);
    await page.getByRole("button", { name: "Close Document 4", exact: true }).click();
    for (const surface of ["context", "list"]) {
      if (surface === "context") await page.getByRole("tab", { name: "Reading workflow", exact: true }).click({ button: "right" });
      else {
        await page.getByRole("button", { name: "Open document list", exact: true }).click();
        await page.getByRole("button", { name: "Tab actions for Reading workflow", exact: true }).click();
      }
      const menu = page.getByRole("menu", { name: "Tab actions for Reading workflow", exact: true });
      await expect(menu).toBeVisible();
      const bounds = await menu.boundingBox();
      expect(bounds!.y).toBeGreaterThanOrEqual(11);
      expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(364);
      expect(await menu.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
      await menu.press("End");
      const undo = menu.getByRole("menuitem", { name: "Undo close tab", exact: true });
      await expect(undo).toBeFocused();
      await expect(undo).toBeInViewport();
      await page.screenshot({ path: testInfo.outputPath(`tab-menu-${surface}-landscape.png`) });
      if (surface === "context") await page.keyboard.press("Escape");
      else await undo.click();
    }
    await expect(page.locator('.reading-tabs [role="tab"]')).toHaveCount(4);
  });
});
