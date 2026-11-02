import { expect, test } from "@playwright/test";
import { readingFixture as fixture } from "./fixtures/reading-workspace";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures against the frontend.");


for (const width of [1440, 390]) {
  test(`reader tools preserve document state at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, width === 390);
    await page.goto("/vault/fixture/doc/reading.md");
    const canvas = page.locator("#document-reading-canvas");
    await expect(canvas.getByRole("heading", { name: "Reading workflow", exact: true })).toBeVisible();
    const more = page.getByRole("group", { name: "Document reading tools", exact: true }).getByRole("button", { name: "Actions for Reading workflow", exact: true });
    await more.click();
    await page.getByRole("menuitem", { name: "Reading settings", exact: true }).click();
    const settings = page.getByRole("dialog", { name: "Reading settings", exact: true });
    await expect(settings).toBeVisible();
    await settings.getByRole("radio", { name: "Wide", exact: true }).check();
    await settings.getByRole("button", { name: "Increase text size", exact: true }).click();
    await settings.getByRole("checkbox", { name: "Start with code collapsed" }).check();
    await expect(canvas.getByRole("button", { name: "Expand code block 1" })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("reading-settings.png") });
    await page.keyboard.press("Escape");
    await expect(more).toBeFocused();
    await more.click();
    await page.getByRole("menuitem", { name: "Find in document", exact: true }).click();
    const find = page.getByRole("searchbox", { name: "Find text in document", exact: true });
    await expect(find).toBeFocused();
    await find.fill("needle");
    await expect(page.getByRole("search")).toContainText("1/3");
    await page.getByRole("button", { name: "Next match", exact: true }).click();
    await expect(canvas.locator('pre[data-reading-folded="true"]')).toHaveCount(0);
    await expect(page.getByRole("search")).toContainText("2/3");
    await page.keyboard.press("Escape");
    await expect(more).toBeFocused();
    await expect(canvas.locator(".markdown-reading-match")).toHaveCount(0);
    await more.click();
    await page.getByRole("menuitem", { name: "Document details", exact: true }).click();
    const outline = page.getByRole("navigation", { name: "Document outline", exact: true });
    await expect(outline).toBeVisible();
    await outline.getByRole("searchbox", { name: "Find a section", exact: true }).fill("Nested");
    await expect(outline.getByRole("link", { name: "Reading workflow", exact: true })).toBeVisible();
    await expect(outline.getByRole("link", { name: "Code example", exact: true })).toBeVisible();
    await expect(outline.getByRole("link", { name: "Final notes", exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Close document panel", exact: true }).click();
    const enlarge = canvas.getByRole("button", { name: "Enlarge image: Reading diagram", exact: true });
    await expect(enlarge).toBeEnabled();
    await enlarge.click();
    const zoom = page.getByRole("dialog", { name: "Reading diagram", exact: true });
    await expect(zoom).toBeVisible();
    const picture = zoom.getByRole("img", { name: "Reading diagram", exact: true });
    const fitted = (await picture.boundingBox())!;
    await zoom.getByRole("button", { name: "Zoom in", exact: true }).click();
    await expect.poll(async () => (await picture.boundingBox())!.width).toBeGreaterThan(fitted.width);
    await zoom.getByRole("button", { name: "1:1", exact: true }).click();
    await expect.poll(async () => (await picture.boundingBox())!.width).toBe(1000);
    await page.keyboard.press("Escape");
    await expect(enlarge).toBeFocused();
    await canvas.getByRole("link", { name: "Inspect reference", exact: true }).click();
    await page.getByRole("button", { name: "Keep in tabs", exact: true }).click();
    await page.getByRole("button", { name: "Close reference preview", exact: true }).click();
    await page.getByRole("button", { name: "Open document list", exact: true }).click();
    await page.getByRole("button", { name: "Tab actions for Reference notes", exact: true }).click();
    await page.getByRole("menuitem", { name: "Move Reference notes left", exact: true }).click();
    await expect(page.getByRole("tablist", { name: "Open documents", exact: true }).getByRole("tab").first()).toContainText("Reference notes");
    await page.keyboard.press("Escape");
    if (width > 1000) {
      await page.getByRole("tab", { name: /^Reference notes/ }).dragTo(page.getByRole("tab", { name: "Reading workflow", exact: true }));
      await expect(page.getByRole("tablist", { name: "Open documents", exact: true }).getByRole("tab").first()).toHaveText("Reading workflow");
    }
    await page.getByRole("tab", { name: /^Reference notes/ }).click({ button: "middle" });
    await expect(page.getByRole("tab", { name: /^Reference notes/ })).toHaveCount(0);
    await page.getByRole("button", { name: "Open document list", exact: true }).click();
    await page.getByRole("tab", { name: /^Recently closed/ }).click();
    await page.getByRole("button", { name: /Reference notes.*reference.md/ }).click();
    await expect(page).toHaveURL(/reference.md(?:\?.*)?$/);
    await page.getByRole("tab", { name: "Reading workflow", exact: true }).click();
    await expect(canvas.getByRole("heading", { name: "Reading workflow", exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.screenshot({ path: testInfo.outputPath("reader-tools.png") });
  });
}
