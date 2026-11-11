import { expect, test } from "@playwright/test";
import { readingFixture } from "./fixtures/reading-workspace";
import { readerNavigation, readingSettings } from "./fixtures/reader-navigation";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");

for (const scenario of [{ width: 1280, dark: false }, { width: 1440, dark: false }, { width: 1920, dark: true }, { width: 375, dark: false }]) {
  test(`Contents and References share navigation at ${scenario.width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width: scenario.width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" }); await readingFixture(page, scenario.dark);
    await page.goto("/vault/fixture/doc/reading.md");
    const canvas = page.locator("#document-reading-canvas");
    await expect(canvas.getByRole("heading", { name: "Reading workflow", exact: true })).toBeVisible();
    const tools = page.getByRole("group", { name: "Document reading tools", exact: true });
    await expect(tools.getByRole("button", { name: /^Show document references/ })).toHaveCount(0);
    await expect(tools.getByRole("button", { name: "Reading settings", exact: true })).toHaveCount(0);
    const panel = await readerNavigation(page);
    const references = panel.getByRole("region", { name: "Document references", exact: true });
    await expect(references.getByRole("status", { name: "2 connections" })).toHaveText("2");
    const outline = panel.getByRole("navigation", { name: "Document outline", exact: true });
    const toc = (await outline.boundingBox())!; const ref = (await references.boundingBox())!;
    expect(ref.y).toBeGreaterThanOrEqual(toc.y + toc.height);
    await expect(references.getByRole("button", { name: "Connection details", exact: true })).toBeInViewport({ ratio: 1 });
    await page.screenshot({ path: info.outputPath("contents-references.png") });
    const link = references.getByRole("link", { name: /Reference notes.*depends on/ }).first();
    const before = await canvas.evaluate(element => element.scrollTop);
    await link.click();
    const preview = scenario.width < 800 ? page.getByRole("dialog", { name: "Reference preview", exact: true }) : page.getByRole("complementary", { name: "Reference notes", exact: true });
    await expect(preview).toBeVisible();
    if (scenario.width < 800) await expect(page.getByRole("dialog", { name: "Contents", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Back to references", exact: true }).click();
    await expect(preview).toHaveCount(0);
    const returned = page.getByRole("region", { name: "Document references", exact: true });
    await expect(returned).toBeVisible();
    await expect(returned.getByRole("link", { name: /Reference notes.*depends on/ }).first()).toBeFocused();
    expect(await canvas.evaluate(element => element.scrollTop)).toBe(before);
    if (scenario.width < 800) await page.getByRole("button", { name: "Close contents", exact: true }).click();
    const more = tools.getByRole("button", { name: "Actions for Reading workflow", exact: true });
    const settings = await readingSettings(page);
    await settings.getByRole("button", { name: "Increase text size", exact: true }).click();
    await page.keyboard.press("Escape"); await expect(more).toBeFocused();
    await expect.poll(() => canvas.locator(".ProseMirror").evaluate(element => getComputedStyle(element).fontSize)).toBe("18px");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(scenario.width);
  });
}

test("long navigation lists keep References reachable without moving the article", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 800 }); await readingFixture(page, false, {
    "reading.md": "# Reading workflow\n\n" + Array.from({ length: 60 }, (_, index) => `## Section ${index + 1}\n\nBody text.`).join("\n\n"),
  });
  await page.goto("/vault/fixture/doc/reading.md");
  const panel = await readerNavigation(page);
  await expect(panel.getByRole("region", { name: "Document references" }).getByRole("button", { name: "Connection details" })).toBeInViewport({ ratio: 1 });
  await panel.locator(".document-contents-scroll").evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect(panel.getByRole("link", { name: "Section 60", exact: true })).toBeInViewport();
  expect(await page.locator("#document-reading-canvas").evaluate(element => element.scrollTop)).toBe(0);
});

for (const width of [1440, 375]) test(`outline toolbar preserves keyboard folding and filtered context at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 }); await readingFixture(page);
  await page.goto("/vault/fixture/doc/reading.md");
  const panel = await readerNavigation(page);
  const outline = panel.getByRole("navigation", { name: "Document outline", exact: true });
  const search = outline.getByRole("searchbox", { name: "Find a section" });
  const fold = outline.getByRole("button", { name: "Collapse all", exact: true });
  const inputBox = (await search.boundingBox())!, foldBox = (await fold.boundingBox())!;
  expect(Math.abs(inputBox.y + inputBox.height / 2 - foldBox.y - foldBox.height / 2)).toBeLessThan(2);
  await fold.focus(); await page.keyboard.press("Enter");
  const expand = outline.getByRole("button", { name: "Expand all", exact: true });
  await expect(expand).toBeFocused();
  await expect(outline.getByRole("link", { name: "Nested detail", exact: true })).toHaveCount(0);
  await search.fill("Nested");
  await expect(expand).toBeDisabled();
  await expect(outline.getByRole("link", { name: "Nested detail", exact: true })).toBeVisible();
  await search.fill("");
  await expect(outline.getByRole("link", { name: "Nested detail", exact: true })).toHaveCount(0);
  await expand.click();
  await expect(outline.getByRole("link", { name: "Nested detail", exact: true })).toBeVisible();
});


test("returning from preview preserves reference-map selection", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 }); await readingFixture(page);
  await page.goto("/vault/fixture/doc/reading.md");
  const panel = await readerNavigation(page);
  await panel.getByRole("button", { name: "Reference map", exact: true }).click();
  const link = panel.getByRole("link", { name: /^From: Reference notes/ });
  await link.click();
  await page.getByRole("button", { name: "Back to references", exact: true }).click();
  const returned = await readerNavigation(page);
  await expect(returned.getByRole("button", { name: "Reference map", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(returned.getByRole("link", { name: /^From: Reference notes/ })).toBeFocused();
});
