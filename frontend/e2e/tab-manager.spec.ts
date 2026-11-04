import { expect, test, type Page } from "@playwright/test";
import axe from "axe-core";
import { readingFixture } from "./fixtures/reading-workspace";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");
const titles: Record<string, string> = {
  "reading.md": "Reading workflow",
  "architecture/review.md": "Architecture decisions and accessibility review for a responsive document reading workspace",
  "notes/review.md": "Review notes",
  "archive/review.md": "Review notes",
  "archive/untitled.md": "",
  ...Object.fromEntries(Array.from({ length: 12 }, (_, index) => [`notes/chapter-${index}.md`, `Chapter ${index + 1}`])),
  "closed.md": "Previous research notes",
};
async function fixture(page: Page, dark = false) {
  await readingFixture(page, dark);
  await page.addInitScript(paths => localStorage.setItem("akb:reading-session:v1:studio-reader", JSON.stringify({ schema: 1,
    tabs: paths.filter(path => path !== "closed.md").map(path => ({ uri: `akb://fixture/doc/${path}` })),
    closed: [{ uri: "akb://fixture/doc/closed.md" }], companion: null,
  })), Object.keys(titles));
  await page.route("**/api/v1/documents/fixture/**", route => {
    const url = new URL(route.request().url());
    const path = decodeURIComponent(url.pathname.split("/documents/fixture/")[1]);
    if (!Object.hasOwn(titles, path)) return route.fallback();
    return route.fulfill({ json: { uri: `akb://fixture/doc/${path}`, path, title: titles[path], content: `# ${titles[path]}\n\nReading content.`, current_commit: "aaaaaaaaaaaaa", status: "active", created_by_name: "Reading reviewer" } });
  });
  await page.goto("/vault/fixture/doc/reading.md");
  await expect(page.getByRole("tablist", { name: "Open documents", exact: true }).getByRole("tab")).toHaveCount(Object.keys(titles).length - 1);
  await page.getByRole("button", { name: "Open document list", exact: true }).click();
  const manager = page.getByRole("dialog", { name: "Tabs", exact: true });
  await expect(manager.getByRole("searchbox", { name: "Find open documents", exact: true })).toBeFocused();
  await page.addScriptTag({ content: axe.source });
  const violations = await page.evaluate(async () => (await (window as unknown as { axe: typeof axe }).axe.run(document.querySelector(".tab-manager")!, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"] },
  })).violations.map(result => ({ id: result.id, targets: result.nodes.map(node => node.target) })));
  expect(violations).toEqual([]);
  return manager;
}

test("shows only titles in the strip and reveals paths for matching titles", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 720 });
  const manager = await fixture(page);
  const listButton = page.getByRole("button", { name: "Open document list", exact: true });
  await page.keyboard.press("Escape");
  await expect(listButton).toBeFocused();

  const strip = page.getByRole("tablist", { name: "Open documents", exact: true });
  const reviews = strip.getByRole("tab", { name: "Review notes", exact: true });
  await expect(reviews).toHaveText(["Review notes", "Review notes"]);
  await expect(strip.getByRole("tab", { name: "untitled.md", exact: true })).toHaveAccessibleDescription("fixture / archive/untitled.md");
  const longTitle = titles["architecture/review.md"];
  const longTab = strip.getByRole("tab", { name: longTitle, exact: true });
  expect(await longTab.locator("span").evaluate(label => label.scrollWidth > label.clientWidth)).toBe(true);

  await longTab.hover();
  await expect(page.getByRole("tooltip")).toHaveText("fixture / architecture/review.md");
  await expect(page.getByText(longTitle, { exact: true }).filter({ visible: true })).toHaveCount(2);
  await longTab.press("ArrowRight");
  await expect(reviews.first()).toBeFocused();
  await expect(page.getByRole("tooltip")).toHaveText("fixture / notes/review.md");
  await reviews.first().press("ArrowRight");
  await expect(reviews.last()).toHaveAccessibleDescription("fixture / archive/review.md");

  await listButton.click();
  const search = manager.getByRole("searchbox", { name: "Find open documents" });
  await search.fill("archive/review.md");
  await expect(manager.getByRole("listitem")).toHaveCount(1);
  await expect(manager.getByRole("listitem")).toContainText("fixture / archive/review.md");
});

test("filters by path, keeps contextual actions reachable and opens a side document", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  const manager = await fixture(page);
  const search = manager.getByRole("searchbox", { name: "Find open documents", exact: true });
  await search.fill("architecture");
  const rows = manager.getByRole("list", { name: "Open tabs", exact: true }).getByRole("listitem");
  await expect(rows).toHaveCount(1);
  await search.press("ArrowDown");
  await expect(rows.getByRole("button", { name: /Architecture decisions.*fixture \/ architecture/ })).toBeFocused();
  await rows.getByRole("button", { name: /^Tab actions for Architecture decisions/ }).click();
  await expect(page.getByRole("menuitem", { name: /^Read Architecture decisions/ })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(manager).toBeVisible();
  await expect(rows.getByRole("button", { name: /^Tab actions for Architecture decisions/ })).toBeFocused();
  await search.fill("");
  await page.screenshot({ path: testInfo.outputPath("tab-manager-desktop.png") });
  await manager.getByRole("button", { name: /^Tab actions for Architecture decisions/ }).click();
  await page.getByRole("menuitem", { name: /^Read Architecture decisions/ }).click();
  await expect(manager).toHaveCount(0);
  await expect(page.getByRole("complementary", { name: `Side document: ${titles["architecture/review.md"]}`, exact: true })).toBeVisible();
  await expect(page).toHaveURL(/reading.md$/);
});

for (const viewport of [{ width: 375, height: 812 }, { width: 812, height: 375 }]) {
  test(`keeps tabs, history and close recovery usable at ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const manager = await fixture(page, true);
    const bounds = (await manager.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0); expect(bounds.y).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(viewport.width);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(viewport.height);
    await page.screenshot({ path: testInfo.outputPath("tab-manager-narrow.png") });
    await manager.getByRole("tab", { name: /^Recently closed/ }).click();
    await manager.getByRole("searchbox", { name: "Find recently closed documents" }).fill("no such tab");
    await expect(manager.getByRole("status").filter({ hasText: "No matching tabs" })).toBeVisible();
    await manager.getByRole("tab", { name: /^Open \(/ }).click();
    const search = manager.getByRole("searchbox", { name: "Find open documents" });
    await search.fill("notes/review.md");
    await manager.getByRole("button", { name: "Close Review notes from list", exact: true }).click();
    await expect(search).toBeFocused();
    if (viewport.width > 500) await search.press("Alt+Shift+T");
    else await manager.getByRole("button", { name: "Reopen last closed", exact: true }).click();
    await expect(manager).toHaveCount(0);
    await expect(page).toHaveURL(/notes%2Freview.md$/);
    await page.getByRole("button", { name: "Open document list", exact: true }).click();
    await manager.getByRole("button", { name: "Close tab manager", exact: true }).click();
    await expect(page.getByRole("button", { name: "Open document list", exact: true })).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  });
}
