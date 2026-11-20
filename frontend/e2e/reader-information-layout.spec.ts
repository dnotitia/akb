import { readerNavigation, readingSettings } from "./fixtures/reader-navigation";
import { expect, test, type Page } from "@playwright/test";
import { readingFixture } from "./fixtures/reading-workspace";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");
const body = '# Reading workflow\n\n- Ordinary list text remains visible.\n- Another ordinary item.\n\n## Parent section\n\n- Parent list remains visible.\n\n### Nested detail\n\nA hidden needle.\n\n## Final notes\n\nFinal body.';

async function fixture(page: Page, dark = false) {
  await readingFixture(page, dark, { "reading.md": body });
  await page.addInitScript(() => localStorage.setItem("akb.treeVisible", "1"));
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.route("**/history/fixture/reading.md*", route => route.fulfill({ json: { history: [
    { hash: "aaaaaaaaaaaaa", author: "Reading reviewer", date: "2026-11-01T00:00:00Z", message: "Clarify a very long reading decision with enough context to wrap naturally without overlapping the comparison control" },
    { hash: "bbbbbbbbbbbbb", author: "Another contributor", date: "2026-10-31T00:00:00Z", message: "Initial document" },
  ] } }));
}

async function contents(page: Page) {
  const trigger = page.getByRole("button", { name: "Open contents", exact: true });
  if (await trigger.isVisible()) await trigger.click();
  return page.getByRole("navigation", { name: "Document outline", exact: true });
}

test("keeps navigation hairlines fixed when entering and leaving a document", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 }); await fixture(page);
  await page.goto("/vault/fixture");
  const row = page.locator('[data-slot="collection-management-row"]:visible');
  await expect(row).toBeVisible();
  const before = (await row.boundingBox())!;
  await page.locator("#vault-workspace-navigation:visible").getByRole("link", { name: "Document: Reading workflow", exact: true }).click();
  await expect(page.locator("#document-reading-canvas").getByRole("heading", { name: "Reading workflow", exact: true })).toBeVisible();
  const after = (await row.boundingBox())!;
  expect(after.height).toBe(before.height);
  for (const selector of ['[data-slot="workspace-sidebar-heading"]:visible', '.reading-studio-header']) {
    const box = (await page.locator(selector).boundingBox())!;
    expect(box.y + box.height).toBeCloseTo(after.y + after.height, 0);
  }
  await page.getByRole("link", { name: "Back to vault fixture", exact: true }).click();
  expect((await row.boundingBox())!.height).toBe(before.height);
});

for (const scenario of [
  { width: 1440, height: 1000, dark: false }, { width: 1920, height: 1080, dark: true },
  { width: 1024, height: 900, dark: false }, { width: 375, height: 812, dark: false },
  { width: 812, height: 375, dark: true },
]) test(`reading navigation and information remain separate at ${scenario.width}×${scenario.height}`, async ({ page }, testInfo) => {
  await page.setViewportSize(scenario); await fixture(page, scenario.dark);
  await page.goto("/vault/fixture/doc/reading.md");
  const canvas = page.locator("#document-reading-canvas");
  await expect(canvas.getByRole("heading", { name: "Reading workflow", exact: true })).toBeVisible();
  const toolbar = page.getByRole("group", { name: "Document reading tools", exact: true });
  await expect(toolbar.getByRole("button", { name: "Table of contents", exact: true })).toHaveCount(0);
  await expect(toolbar.getByRole("button", { name: "Reading settings", exact: true })).toHaveCount(0);
  const more = toolbar.getByRole("button", { name: "Actions for Reading workflow", exact: true });
  await more.click();
  for (const name of ["Document details", "Details", "Version history", "Table of contents"]) await expect(page.getByRole("menuitem", { name, exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  const wide = await page.getByRole("complementary", { name: "Contents", exact: true }).isVisible();
  if (scenario.width >= 1440) expect(wide).toBe(true);
  const outline = await contents(page);
  await expect(outline).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Document outline", exact: true })).toHaveCount(1);
  if (!wide) {
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Open contents", exact: true })).toBeFocused();
  }
  await canvas.getByRole("button", { name: "Collapse section: Parent section", exact: true }).click();
  await expect(canvas.getByRole("heading", { name: "Nested detail", exact: true })).not.toBeVisible();
  const link = (await contents(page)).getByRole("link", { name: "Nested detail", exact: true });
  await link.click();
  const nested = canvas.getByRole("heading", { name: "Nested detail", exact: true });
  await expect(nested).toBeVisible();
  if (!wide) await expect(nested).toBeFocused();
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  const details = canvas.getByRole("button", { name: "Details", exact: true });
  await details.scrollIntoViewIfNeeded(); await details.click();
  const dialog = page.getByRole("dialog", { name: "Details", exact: true });
  await expect(dialog.getByRole("tab", { name: "Properties", exact: true })).toBeFocused();
  await expect(dialog.getByRole("navigation", { name: "Document outline" })).toHaveCount(0);
  await expect(dialog.getByRole("tab", { name: /Related|Connections/ })).toHaveCount(0);
  await dialog.getByRole("tab", { name: "Version history", exact: true }).click();
  const revision = dialog.getByRole("button", { name: /^View version aaaaaaa:/ });
  const compare = dialog.getByRole("button", { name: "View changes in version aaaaaaa", exact: true });
  await expect(revision).toBeVisible(); await expect(compare).toBeInViewport({ ratio: 1 });
  const revisionBox = (await revision.boundingBox())!; const compareBox = (await compare.boundingBox())!;
  expect(revisionBox.x + revisionBox.width).toBeLessThanOrEqual(compareBox.x);
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("reader-history.png") });
  await page.keyboard.press("Escape"); await expect(details).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("reader-information.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(scenario.width);
});

for (const width of [1440, 390]) test(`ordinary list gaps never activate section folding at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 }); await fixture(page);
  await page.goto("/vault/fixture/doc/reading.md");
  const canvas = page.locator("#document-reading-canvas");
  const settings = page.getByRole("group", { name: "Document reading tools", exact: true }).getByRole("button", { name: "Actions for Reading workflow", exact: true });
  await readingSettings(page);
  await page.getByRole("button", { name: "Increase text size", exact: true }).click();
  await page.getByRole("button", { name: "Increase text size", exact: true }).click();
  await page.keyboard.press("Escape"); await expect(settings).toBeFocused();
  for (const text of ["Ordinary list text remains visible.", "Parent list remains visible."]) {
    const paragraph = canvas.getByText(text, { exact: true }); await paragraph.scrollIntoViewIfNeeded();
    const point = await paragraph.evaluate(element => {
      const range = document.createRange(); range.selectNodeContents(element);
      const box = range.getBoundingClientRect(); return { x: box.x - 4, y: box.y + box.height / 2 };
    });
    await page.mouse.click(point.x, point.y);
    await expect(paragraph).toBeVisible();
    await expect(canvas.locator('[data-reading-section-hidden="true"]')).toHaveCount(0);
  }
  const toggle = canvas.getByRole("button", { name: "Collapse section: Parent section", exact: true });
  const heading = canvas.getByRole("heading", { name: "Parent section", exact: true });
  await toggle.scrollIntoViewIfNeeded();
  const buttonBox = (await toggle.boundingBox())!; const headingBox = (await heading.boundingBox())!;
  expect(buttonBox.y).toBeGreaterThanOrEqual(headingBox.y);
  expect(buttonBox.y + buttonBox.height).toBeLessThanOrEqual(headingBox.y + headingBox.height + 1);
  await toggle.click(); await expect(canvas.getByText("Parent list remains visible.", { exact: true })).not.toBeVisible();
  await expect(canvas.getByText("Ordinary list text remains visible.", { exact: true })).toBeVisible();
  await canvas.getByRole("button", { name: "Expand section: Parent section", exact: true }).click();
  await expect(canvas.getByText("Parent list remains visible.", { exact: true })).toBeVisible();
});

for (const width of [1440, 390]) test(`connection management preserves its separate reference surface at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 }); await fixture(page);
  await page.goto("/vault/fixture/doc/reading.md");
  await readerNavigation(page);
  const opener = page.getByRole("button", { name: "Connection details", exact: true }); await opener.click();
  const connections = page.getByRole("dialog", { name: "Connections", exact: true });
  await expect(connections).toBeVisible(); await expect(connections.getByRole("tablist")).toHaveCount(0);
  await expect(connections.getByRole("navigation", { name: "Document outline", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape"); await expect(opener).toBeFocused();
  await expect(page.getByRole("region", { name: "Document references", exact: true })).toBeVisible();
  if (width < 1000) {
    await page.getByRole("button", { name: "Close contents", exact: true }).click();
    await expect(page.getByRole("button", { name: "Open contents", exact: true })).toBeFocused();
  }
});

test("Contents keeps keyboard navigation reachable when its available width changes", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 }); await fixture(page);
  await page.goto("/vault/fixture/doc/reading.md");
  const outline = page.getByRole("navigation", { name: "Document outline", exact: true });
  await outline.getByRole("link", { name: "Parent section", exact: true }).focus();
  await page.setViewportSize({ width: 390, height: 844 });
  const trigger = page.getByRole("button", { name: "Open contents", exact: true });
  await expect(trigger).toBeFocused(); await trigger.click();
  await outline.getByRole("link", { name: "Nested detail", exact: true }).focus();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(page.getByRole("dialog", { name: "Contents", exact: true })).toHaveCount(0);
  await expect(page.getByRole("complementary", { name: "Contents", exact: true })).toBeVisible();
  expect(await outline.evaluate(element => element.contains(document.activeElement))).toBe(true);
});

test.describe("touch reading layout", () => {
  test.use({ hasTouch: true });
  test("shares the navigation baseline and keeps folding away from ordinary list taps", async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 1000 }); await fixture(page);
    await page.goto("/vault/fixture/doc/reading.md");
    const row = page.locator('[data-slot="collection-management-row"]:visible');
    await expect(row).toBeVisible();
    const box = (await row.boundingBox())!;
    expect(box.height).toBe(48);
    for (const selector of ['[data-slot="workspace-sidebar-heading"]:visible', '.reading-studio-header']) {
      const bounds = (await page.locator(selector).boundingBox())!;
      expect(bounds.y + bounds.height).toBeCloseTo(box.y + box.height, 0);
    }
    const text = page.locator("#document-reading-canvas").getByText("Parent list remains visible.", { exact: true });
    await text.tap({ position: { x: 1, y: 10 } }); await expect(text).toBeVisible();
    await expect(page.locator('[data-reading-section-hidden="true"]')).toHaveCount(0);
    const toggle = page.getByRole("button", { name: "Collapse section: Parent section", exact: true });
    expect((await toggle.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  });
});
