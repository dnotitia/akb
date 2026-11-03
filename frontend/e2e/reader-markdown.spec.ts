import { expect, test } from "@playwright/test";
import { readingFixture } from "./fixtures/reading-workspace";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");
const body = '# Reading workflow\n\n[Jump to nested detail](#nested-detail)\n\n## Folding area\n\nParent body.\n\n### Nested detail\n\nA hidden needle and a fact[^evidence].\n\n## Diagram\n\n```mermaid\nflowchart LR\naccTitle: Flow of documents\nA[Document] --> B[Reference]\n```\n\n## Final notes\n\nAnother fact[^evidence].\n\n[^evidence]: Supporting **evidence** with a [source](https://example.org).';

for (const scenario of [{ width: 1440, height: 1000, dark: false }, { width: 1440, height: 1000, dark: true }, { width: 390, height: 844, dark: false }, { width: 375, height: 812, dark: false }]) {
  test(`section navigation and block tools at ${scenario.width}px ${scenario.dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize(scenario); await readingFixture(page, scenario.dark, { "reading.md": body });
    await page.goto("/vault/fixture/doc/reading.md");
    const header = page.locator(".reading-studio-header");
    const find = header.getByRole("button", { name: "Find in document", exact: true });
    const contents = header.getByRole("button", { name: "Table of contents", exact: true });
    await expect(find).toBeInViewport({ ratio: 1 }); await expect(contents).toBeInViewport({ ratio: 1 });
    const actions = header.getByRole("button", { name: "Actions for Reading workflow", exact: true });
    await expect(actions).toBeInViewport({ ratio: 1 });
    expect((await actions.boundingBox())!.y).toBeCloseTo((await find.boundingBox())!.y, 0);
    await page.getByRole("button", { name: "Collapse section: Folding area", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Nested detail", exact: true })).not.toBeVisible();
    await find.click(); await page.getByRole("searchbox", { name: "Find text in document", exact: true }).fill("needle");
    await expect(page.getByRole("heading", { name: "Nested detail", exact: true })).toBeVisible();
    await page.keyboard.press("Escape"); await expect(find).toBeFocused();
    await page.getByRole("button", { name: "Collapse section: Folding area", exact: true }).click();
    await contents.click();
    const panel = page.locator('[data-slot="document-context-panel"]');
    await panel.getByRole("link", { name: "Nested detail", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Nested detail", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Close document panel", exact: true }).click(); await expect(contents).toBeFocused();
    const diagram = page.getByRole("img", { name: "Flow of documents", exact: true });
    await diagram.scrollIntoViewIfNeeded();
    await expect(diagram).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => diagram.evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
    await page.getByRole("button", { name: "Show Mermaid source", exact: true }).click();
    await expect(page.locator('pre[data-markdown-code-language="mermaid"]')).toBeVisible();
    await page.getByRole("button", { name: "Show Mermaid diagram", exact: true }).click();
    await page.getByRole("button", { name: "Enlarge Mermaid diagram", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Flow of documents", exact: true })).toBeVisible(); await page.keyboard.press("Escape");
    const reference = page.getByRole("link", { name: "Footnote 1", exact: true }).last();
    await expect(reference).toHaveAttribute("role", "doc-noteref");
    await reference.click();
    const note = page.getByRole("dialog", { name: "Footnote 1", exact: true });
    await expect(note.getByRole("link", { name: "source", exact: true })).toHaveAttribute("href", "https://example.org");
    await page.keyboard.press("Escape"); await expect(reference).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.screenshot({ path: testInfo.outputPath("reader-markdown.png") });
  });
}

test("invalid Mermaid keeps readable, copyable source", async ({ page }) => {
  await readingFixture(page, false, { "reading.md": '# Reading workflow\n\n```mermaid\nnot a valid diagram\n```' });
  await page.goto("/vault/fixture/doc/reading.md");
  await expect(page.getByText("Diagram could not be rendered.", { exact: false })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('pre[data-markdown-code-language="mermaid"]')).toContainText("not a valid diagram");
  await expect(page.getByRole("button", { name: "Copy code block 1" })).toBeEnabled();
});

test("reading settings carry into search previews and public documents", async ({ page }) => {
  await readingFixture(page, false, { "reading.md": body });
  await page.route("**/api/v1/search?**", route => route.fulfill({ json: { query: "reading", total: 1, results: [{ title: "Reading workflow", uri: "akb://fixture/doc/reading.md", vault: "fixture", path: "reading.md", source_type: "document", score: 1 }] } }));
  await page.route("**/api/v1/public/reader-demo*", route => route.fulfill({ json: { resource_type: "document", title: "Published reading", content: body, tags: [], created_by_name: "Reading reviewer", status: "active" } }));
  await page.goto("/vault/fixture/doc/reading.md");
  await page.getByRole("group", { name: "Document reading tools", exact: true }).getByRole("button", { name: "Actions for Reading workflow", exact: true }).click();
  await page.getByRole("menuitem", { name: "Reading settings", exact: true }).click();
  await page.getByRole("radio", { name: "Wide", exact: true }).check();
  await page.getByRole("button", { name: "Increase text size", exact: true }).click();
  await page.keyboard.press("Escape");
  await page.goto("/search?q=reading&source=document");
  await page.getByRole("link", { name: /Reading workflow/ }).first().click();
  const preview = page.getByTestId("document-preview-dialog");
  await expect(preview.getByRole("button", { name: "Find in document", exact: true })).toBeVisible();
  await expect.poll(() => preview.locator('[data-markdown-surface="viewer"]').first().evaluate(element => getComputedStyle(element).fontSize)).toBe("18px");
  await preview.getByRole("button", { name: "Find in document", exact: true }).click();
  await preview.getByRole("searchbox", { name: "Find text in document", exact: true }).fill("needle");
  await expect(preview.locator('[data-reading-match="current"]')).toContainText("needle");
  await page.goto("/p/reader-demo");
  await expect(page.getByRole("heading", { name: "Published reading", exact: true })).toBeVisible();
  const publicBody = page.locator('[data-markdown-surface="viewer"]').first();
  await expect.poll(() => publicBody.evaluate(element => getComputedStyle(element).fontSize)).toBe("18px");
  await page.getByRole("button", { name: "Reading settings", exact: true }).click();
  await expect(page.getByRole("radio", { name: "Wide", exact: true })).toBeChecked();
  await page.getByRole("button", { name: "Reset reading settings", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect.poll(() => publicBody.evaluate(element => getComputedStyle(element).fontSize)).toBe("16px");
});

for (const width of [1440, 390]) {
  test(`Markdown blocks share the reading column at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const markdown = [
      "# Reading column", "A paragraph sets the reading column.",
      "```text\n" + "long_code_token".repeat(120) + "\n```",
      "| Heading | Value |\n| --- | --- |\n| Column | Value |",
      "> An inset quotation.\n>\n> ```text\n> nested code\n> ```", "---",
      "![Reading image](https://example.org/reading-image.svg)",
      "A note[^note].\n\n[^note]: Note text.",
    ].join("\n\n");
    await readingFixture(page, false, { "reading.md": markdown });
    await page.route("https://example.org/reading-image.svg", route => route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="80"/>',
    }));
    await page.goto("/vault/fixture/doc/reading.md");
    const prose = page.locator(".document-reading-flow .ProseMirror");
    const paragraph = await prose.getByText("A paragraph sets the reading column.").boundingBox();
    expect(paragraph).not.toBeNull();
    for (const selector of [
      "[data-markdown-code-tools]", "pre", ".akb-md-table",
      "blockquote", "hr", "[data-markdown-footnote-definition]",
    ]) {
      const block = prose.locator(selector).first();
      await expect(block).toBeAttached();
      const box = await block.boundingBox();
      expect(box?.x, selector).toBeCloseTo(paragraph!.x, 0);
      expect(box?.width, selector).toBeCloseTo(paragraph!.width, 0);
    }
    const image = prose.getByRole("img", { name: "Reading image" });
    await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBe(1200);
    const imageBox = (await image.boundingBox())!;
    expect(imageBox.x).toBeCloseTo(paragraph!.x, 0);
    expect(imageBox.width).toBeLessThanOrEqual(paragraph!.width);
    const nestedTools = (await prose.locator("blockquote [data-markdown-code-tools]").boundingBox())!;
    const nestedCode = (await prose.locator("blockquote pre").boundingBox())!;
    expect(nestedCode.x).toBeCloseTo(nestedTools.x, 0);
    expect(nestedCode.width).toBeCloseTo(nestedTools.width, 0);
    expect(nestedCode.y).toBeCloseTo(nestedTools.y + nestedTools.height, 0);
    expect(await prose.locator("pre").first().evaluate(block => block.scrollWidth > block.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
  });
}
