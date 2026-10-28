import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures against the built frontend.");

async function fixture(page: Page, dark = false) {
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "reading-studio-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, { dark });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (pathname.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "studio-reader", username: "fixture", display_name: "Reading reviewer", email: "reader@example.invalid", auth_method: "local", is_admin: false } });
    if (pathname.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "studio-vault", name: "fixture", role: "writer" }] } });
    if (pathname.endsWith("/info")) return route.fulfill({ json: { name: "fixture", role: "writer", is_archived: false, is_external_git: false, document_count: 2 } });
    if (pathname.includes("/browse/")) return route.fulfill({ json: { items: [
      { type: "document", path: "reading.md", name: "Reading workflow" },
      { type: "document", path: "reference.md", name: "Reference notes" },
    ] } });
    if (pathname.endsWith("/relations")) return route.fulfill({ json: { uri: "akb://fixture/doc/reading.md", relations: [
      { direction: "outgoing", relation: "references", uri: "akb://fixture/doc/reference.md", name: "Reference notes", kind: "explicit", resource_type: "document" },
      { direction: "incoming", relation: "depends_on", uri: "akb://fixture/doc/reference.md", name: "Reference notes", kind: "explicit", resource_type: "document" },
    ] } });
    if (pathname.includes("/documents/fixture/") && !pathname.endsWith("/history")) {
      const path = decodeURIComponent(pathname.split("/documents/fixture/")[1]);
      const main = path === "reading.md";
      return route.fulfill({ json: { path, title: main ? "Reading workflow" : "Reference notes",
        content: main ? '# Reading workflow\n\nRead the primary document while keeping evidence close. ConnectX-7 is ordinary prose.\n\n[Inspect reference](akb://fixture/doc/reference.md)\n\n' + Array.from({ length: 18 }, (_, index) => `## Section ${index + 1}\n\n${"A readable workspace keeps context and makes references available without replacing the primary document. ".repeat(6)}`).join("\n\n")
          : '# Reference notes\n\nThis reference stays independent from the main document.\n\n## Shared heading\n\nReference details remain readable.\n\n[Local section](#shared-heading)',
        uri: `akb://fixture/doc/${path}`, current_commit: "aaaaaaaaaaaaa", status: main ? "draft" : "active", created_by_name: "Reading reviewer", updated_at: "2026-10-27T03:00:00Z", created_at: "2026-10-26T03:00:00Z", tags: [] } });
    }
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
}

for (const scenario of [{ width: 1920, dark: false }, { width: 1440, dark: true }, { width: 390, dark: false }]) {
  test(`reading workspace keeps context and references at ${scenario.width}px ${scenario.dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: scenario.width, height: 1000 });
    await fixture(page, scenario.dark);
    await page.goto("/vault/fixture/doc/reading.md");
    const canvas = page.locator("#document-reading-canvas");
    await expect(canvas.getByRole("heading", { name: "Reading workflow", exact: true })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Reading workflow", exact: true })).toHaveAttribute("aria-selected", "true");
    const tools = page.getByRole("group", { name: "Document reading tools", exact: true });
    const references = tools.getByRole("button", { name: /^Show document references/ });
    const more = tools.getByRole("button", { name: "Actions for Reading workflow", exact: true });
    await expect(references).toBeVisible();
    await expect(references.locator(".reading-reference-count")).toContainText("2");
    await expect(references).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("complementary", { name: "Document references", exact: true })).toHaveCount(0);
    await expect(tools.getByRole("button", { name: "Copy markdown", exact: true })).toBeVisible();
    await expect(tools.getByRole("button", { name: "Edit", exact: true })).toHaveText("");
    await expect(tools.getByRole("button", { name: "Document details", exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Explore all connections", exact: true })).toHaveCount(0);
    const order = await tools.locator('[role="radiogroup"], button.reading-reference-trigger, button.document-copy-button, button.document-reader-edit, button[aria-haspopup="menu"]').evaluateAll(elements => elements.map(element => element.getAttribute("aria-label")));
    expect(order).toEqual(["Document view mode", "Show document references (2)", "Copy markdown", "Edit", "Actions for Reading workflow"]);
    const tabbar = page.locator(".reading-workspace-bottom");
    expect((await tabbar.boundingBox())!.height).toBe(40);
    for (const bar of [page.locator(".reading-studio-header"), tabbar]) {
      const insets = await bar.evaluate(element => {
        const divider = getComputedStyle(element, element.classList.contains("reading-studio-header") ? "::after" : "::before");
        return [parseFloat(divider.left), parseFloat(divider.right)];
      });
      expect(insets).toEqual([12, 12]);
    }
    await more.click();
    await expect(page.getByRole("menuitem", { name: "On this page", exact: true })).toHaveCount(0);
    await page.getByRole("menuitem", { name: "Document details", exact: true }).click();
    await expect(page.getByRole("navigation", { name: "Document outline", exact: true })).toBeVisible();
    await page.locator('[data-context-section="info"] > summary').click();
    await expect(page.locator('[data-context-section="info"]')).toHaveAttribute("open", "");
    await page.getByRole("button", { name: "Close document panel", exact: true }).click();
    await expect(more).toBeFocused();
    await references.click();
    if (scenario.width < 1000) {
      await expect(page.getByRole("dialog", { name: "Document references", exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Close references", exact: true }).click();
      await expect(references).toBeFocused();
      await expect(references).toHaveAttribute("aria-expanded", "false");
    } else await expect(references).toHaveAttribute("aria-expanded", "true");
    const byline = canvas.getByRole("group", { name: "Document metadata", exact: true });
    await expect(byline.getByText("Reading reviewer", { exact: true })).toBeVisible();
    await expect(byline.getByText("Draft", { exact: true })).toBeVisible();
    await expect(byline.locator("time")).toHaveAttribute("datetime", "2026-10-27T03:00:00.000Z");
    const headingBounds = (await canvas.getByRole("heading", { name: "Reading workflow", exact: true }).boundingBox())!;
    expect((await byline.boundingBox())!.y).toBeGreaterThanOrEqual(headingBounds.y + headingBounds.height);
    expect(await page.locator('.reading-studio-header [data-slot="document-byline"]').count()).toBe(0);
    await page.getByRole("radio", { name: "Raw markdown", exact: true }).click();
    await expect(canvas.getByRole("group", { name: "Document metadata", exact: true }).getByText("Draft", { exact: true })).toBeVisible();
    await expect(canvas.getByTestId("doc-raw")).toContainText("# Reading workflow");
    await page.getByRole("radio", { name: "Preview", exact: true }).click();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(overflow).toBe(false);
    const text = canvas.getByText(/ConnectX-7 is ordinary prose/);
    await expect(text).toBeVisible();
    const source = canvas.getByRole("link", { name: "Inspect reference", exact: true });
    await expect(source).toBeVisible();
    await source.click();
    const preview = page.locator(".reading-reference-preview, .reading-reference-dialog");
    await expect(preview.locator(".reading-preview-identity").getByRole("heading", { name: "Reference notes", exact: true })).toBeVisible();
    await expect(page).toHaveURL(/\/doc\/reading.md$/);
    await expect(preview.getByRole("heading", { name: "Reference notes", exact: true })).toHaveCount(1);
    await expect(preview.getByRole("group", { name: "Document metadata", exact: true }).getByText("Reading reviewer", { exact: true })).toBeVisible();
    if (scenario.width > 1000) {
      const command = (await page.locator(".reading-studio-header").boundingBox())!;
      const header = (await page.locator(".reading-context-header-host").boundingBox())!;
      const panel = (await preview.boundingBox())!;
      expect(Math.abs(command.y + command.height - panel.y)).toBeLessThanOrEqual(1);
      expect(Math.abs(header.x - panel.x)).toBeLessThanOrEqual(1);
    }
    await preview.getByRole("button", { name: "Keep in tabs", exact: true }).click();
    await expect(preview.getByRole("button", { name: "Kept in tabs", exact: true })).toBeVisible();
    await preview.getByRole("link", { name: "Local section", exact: true }).click();
    await expect(page).toHaveURL(/\/doc\/reading.md$/);
    const duplicateIds = await page.evaluate(() => {
      const ids = Array.from(document.querySelectorAll("[id]")).map(node => node.id);
      return ids.filter((id, index) => ids.indexOf(id) !== index);
    });
    expect(duplicateIds).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("reading-reference.png") });
    await page.getByRole("button", { name: "Close reference preview", exact: true }).click();
    await expect(preview).toHaveCount(0);
    await expect(source).toBeFocused();
    const saved = await canvas.evaluate(element => { element.scrollTop = 600; element.dispatchEvent(new Event("scroll")); return element.scrollTop; });
    await page.getByRole("tab", { name: /Reference notes/ }).click();
    await expect(page).toHaveURL(/\/doc\/reference.md/);
    await page.getByRole("tab", { name: "Reading workflow", exact: true }).click();
    await expect(page).toHaveURL(/\/doc\/reading.md$/);
    await expect.poll(() => canvas.evaluate(element => element.scrollTop)).toBe(saved);
    if (scenario.width === 1920) {
      await expect(references).toHaveAttribute("aria-expanded", "false");
      await references.click();
      await expect(page.getByRole("complementary", { name: "Document references" })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("reading-relations.png") });
    }
  });
}

test.describe("Touch reading workspace", () => {
  test.use({ hasTouch: true });
  for (const viewport of [{ width: 375, height: 812 }, { width: 812, height: 375 }]) {
    test(`reader commands and compact tabs remain reachable at ${viewport.width}x${viewport.height}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.emulateMedia({ reducedMotion: "reduce" });
      await fixture(page, viewport.width > viewport.height);
      await page.goto("/vault/fixture/doc/reading.md");
      const tools = page.getByRole("group", { name: "Document reading tools", exact: true });
      await expect(tools.getByRole("button", { name: /^Show document references/ })).toBeInViewport({ ratio: 1 });
      const targets = await tools.locator('[data-reader-control]:visible').evaluateAll(elements => elements.map(element => ({ width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height })));
      expect(targets.length).toBeGreaterThan(3);
      expect(targets.every(target => target.width >= 44 && target.height >= 44)).toBe(true);
      expect((await page.locator(".reading-workspace-bottom").boundingBox())!.height).toBe(48);
      const close = (await page.getByRole("button", { name: "Close Reading workflow", exact: true }).boundingBox())!;
      expect(close.width).toBeGreaterThanOrEqual(44);
      expect(close.height).toBeGreaterThanOrEqual(44);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await tools.getByRole("button", { name: /^Show document references/ }).click();
      await expect(page.getByRole("dialog", { name: "Document references", exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Reference map", exact: true }).click();
      await expect(page.getByRole("group", { name: "Direct connections to Reading workflow", exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Close references", exact: true }).click();
      await expect(tools.getByRole("button", { name: /^Show document references/ })).toBeFocused();
    });
  }
});

test("reader tools reflow with enlarged text and reduced motion", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await fixture(page, true);
  await page.goto("/vault/fixture/doc/reading.md");
  await expect(page.getByRole("tab", { name: "Reading workflow", exact: true })).toBeVisible();
  await page.addStyleTag({ content: "html { font-size: 200%; }" });
  const tools = page.getByRole("group", { name: "Document reading tools", exact: true });
  await expect(tools.getByRole("button", { name: "Actions for Reading workflow", exact: true })).toBeInViewport({ ratio: 1 });
  await expect(page.locator("#document-reading-canvas").getByRole("group", { name: "Document metadata", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await tools.getByRole("button", { name: "Actions for Reading workflow", exact: true }).click();
  await page.getByRole("menuitem", { name: "Document details", exact: true }).click();
  await expect(page.getByRole("navigation", { name: "Document outline", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close document panel", exact: true }).click();
  await expect(tools.getByRole("button", { name: "Actions for Reading workflow", exact: true })).toBeFocused();
});

test("reader copy controls preserve source and restore their visible trigger", async ({ page }) => {
  await fixture(page);
  await page.addInitScript(() => Object.defineProperty(navigator, "clipboard", {
    configurable: true, value: { writeText: async () => { throw new Error("Clipboard denied for fixture"); } },
  }));
  await page.goto("/vault/fixture/doc/reading.md");
  const tools = page.getByRole("group", { name: "Document reading tools", exact: true });
  const more = tools.getByRole("button", { name: "Actions for Reading workflow", exact: true });
  for (const mode of ["Preview", "Raw markdown"]) {
    await tools.getByRole("radio", { name: mode, exact: true }).click();
    const copy = tools.getByRole("button", { name: "Copy markdown", exact: true });
    await copy.click();
    await expect(page.getByRole("dialog", { name: "Copy markdown", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(copy).toBeFocused();
    await more.click();
    await page.getByRole("menuitem", { name: "Copy markdown", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Copy markdown", exact: true });
    await expect(dialog).toBeVisible();
    const source = await dialog.getByRole("textbox", { name: "Markdown to copy", exact: true }).inputValue();
    expect(source).toMatch(/^# Reading workflow/);
    expect(source).not.toContain("Reading reviewer");
    await page.keyboard.press("Escape");
    await expect(more).toBeFocused();
  }
});

test("tab close has no hover background and closes the intended document", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await fixture(page, true);
  await page.goto("/vault/fixture/doc/reading.md");
  await page.getByRole("link", { name: "Inspect reference", exact: true }).click();
  await page.getByRole("button", { name: "Keep in tabs", exact: true }).click();
  await page.getByRole("button", { name: "Close reference preview", exact: true }).click();
  const close = page.getByRole("button", { name: "Close Reference notes", exact: true });
  const before = (await close.boundingBox())!;
  await close.hover();
  await expect(close).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  const hovered = (await close.boundingBox())!;
  expect(hovered).toEqual(before);
  const tab = (await close.locator("..").boundingBox())!;
  expect(hovered.y - tab.y).toBeGreaterThanOrEqual(4);
  expect(tab.y + tab.height - hovered.y - hovered.height).toBeGreaterThanOrEqual(4);
  await page.screenshot({ path: testInfo.outputPath("inset-tab-close-hover.png") });
  await close.click();
  await expect(page.getByRole("tab", { name: "Reference notes", exact: true })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Reading workflow", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page).toHaveURL(/\/doc\/reading.md$/);
  await page.getByRole("button", { name: "Close Reading workflow", exact: true }).click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
});
