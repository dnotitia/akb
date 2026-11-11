import { readerNavigation } from "./fixtures/reader-navigation";
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

for (const viewport of [{ width: 375, height: 700 }, { width: 1440, height: 900 }]) {
  test(`keeps version history and the comparison header compact at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await fixture(page, viewport.width > 1000);
    const date = "2026-10-27T03:00:00.000Z";
    const message = "UpdatedReadingWorkspaceNavigationAndReviewInstructionsForTheDocumentationTeam";
    const author = "DocumentationTeamReviewerWithAnUnbrokenDisplayName";
    await page.route("**/api/v1/history/fixture/reading.md?*", route => route.fulfill({ json: {
      kind: "document_history",
      uri: "akb://fixture/doc/reading.md",
      history: [
        { hash: "bbbbbbbbbbbbb", message, author_name: author, date },
        { hash: "ccccccccccccc", message: "Create document", author_name: "Initial author", date },
      ],
    } }));
    await page.route("**/api/v1/diff/fixture/reading.md?*", route => route.fulfill({ json: {
      kind: "document_diff", file: "reading.md", commit: "bbbbbbbbbbbbb", type: "modified",
      diff: "--- a/reading.md\n+++ b/reading.md\n@@ -1 +1 @@\n-Original\n+Revised",
    } }));
    await page.goto("/vault/fixture/doc/reading.md");
    const more = page.getByRole("group", { name: "Document reading tools", exact: true }).getByRole("button", { name: "Actions for Reading workflow", exact: true });
    await more.click();
    await expect(page.getByRole("menuitem", { name: "Version history", exact: true })).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Details", exact: true }).click();
    const panel = page.locator('[data-slot="document-context-panel"]');
    const panelHeader = panel.locator(".document-info-heading");
    await expect(panelHeader.getByRole("heading", { name: "Details", exact: true })).toBeVisible();
    await expect(panelHeader.getByRole("button")).toHaveCount(1);
    await expect(panel.getByRole("tab", { name: "Properties", exact: true })).toBeFocused();
    const history = panel.getByRole("tab", { name: "Version history", exact: true });
    await expect(history).toBeInViewport();
    await history.click();

    const openVersion = panel.getByRole("button", { name: `View version bbbbbbb: ${message}` });
    const compare = panel.getByRole("button", { name: "View changes in version bbbbbbb" });
    await expect(openVersion.getByText(author, { exact: true })).toBeVisible();
    await expect(compare).toHaveText("");
    const versionBounds = (await openVersion.boundingBox())!;
    const compareBounds = (await compare.boundingBox())!;
    expect(compareBounds.x).toBeGreaterThanOrEqual(versionBounds.x + versionBounds.width);
    expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("version-history.png") });
    await compare.click();

    const diff = page.getByRole("region", { name: "Changes from ccccccc to bbbbbbb" });
    const header = diff.locator('[data-slot="document-diff-header"]');
    const table = diff.getByRole("table");
    await expect(table.getByRole("row", { name: "Added line 1: Revised" })).toBeVisible();
    await expect(page.getByRole("group", { name: "Document metadata" })).toHaveCount(0);
    await expect(header.getByRole("group", { name: "Revision metadata" }).getByText(author, { exact: true })).toBeVisible();
    await expect(header.locator("time")).toHaveCount(1);
    await expect(header.locator("time")).toHaveAttribute("datetime", date);
    await expect(header.locator("time .sr-only")).toHaveCSS("position", "absolute");
    await expect(header.locator('time [aria-hidden="true"]')).not.toContainText("Version saved");
    expect((await header.boundingBox())!.height).toBeLessThanOrEqual(viewport.width < 640 ? 112 : 66);
    expect((await table.boundingBox())!.height).toBeGreaterThan(viewport.height / 2);
    await page.screenshot({ path: testInfo.outputPath("document-diff.png") });
    await header.locator("time").focus();
    await expect(page.getByRole("tooltip")).toContainText("Version saved:");
    await page.getByRole("button", { name: "Back to version", exact: true }).click();
    await expect(compare).toBeFocused();
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
    const references = page.getByRole("region", { name: "Document references", exact: true });
    const more = tools.getByRole("button", { name: "Actions for Reading workflow", exact: true });
    await expect(tools.getByRole("button", { name: /^Show document references/ })).toHaveCount(0);
    if (scenario.width >= 1000) {
      await expect(references).toBeVisible();
      await expect(references.getByRole("status", { name: "2 connections" })).toHaveText("2");
    }
    if (scenario.width < 640) await expect(tools.locator(".document-copy-button")).not.toBeVisible();
    else await expect(tools.getByRole("button", { name: "Copy markdown", exact: true })).toBeVisible();
    await expect(tools.getByRole("button", { name: "Find in document", exact: true })).toBeInViewport({ ratio: 1 });
    await expect(tools.getByRole("button", { name: "Reading settings", exact: true })).toHaveCount(0);
    await expect(tools.getByRole("button", { name: "Edit", exact: true })).toHaveText("");
    await expect(tools.getByRole("button", { name: "Details", exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Explore all connections", exact: true })).toHaveCount(0);
    const order = await tools.locator('[role="radiogroup"], button.reading-reference-trigger, button.document-copy-button, button.document-reader-edit, button[aria-haspopup="menu"]').evaluateAll(elements => elements.map(element => element.getAttribute("aria-label")));
    expect(order).toEqual(["Document view mode", "Copy markdown", "Edit", "Actions for Reading workflow"]);
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
    await expect(page.getByRole("menuitem", { name: "Copy markdown", exact: true })).toBeEnabled();
    await expect(page.getByRole("menuitem", { name: "On this page", exact: true })).toHaveCount(0);
    await expect(page.getByRole("menuitem", { name: "Document details", exact: true })).toHaveCount(0);
    await page.keyboard.press("Escape");
    const details = canvas.getByRole("button", { name: "Details", exact: true });
    await details.click();
    await expect(page.getByRole("tab", { name: "Properties", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("button", { name: "Close details", exact: true }).click();
    await expect(details).toBeFocused();
    await readerNavigation(page);
    await expect(references).toBeVisible();
    await expect(references.getByRole("status", { name: "2 connections" })).toHaveText("2");
    const contentsSheet = page.getByRole("dialog", { name: "Contents", exact: true });
    if (await contentsSheet.isVisible()) await page.getByRole("button", { name: "Close contents", exact: true }).click();
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
      await expect(references).toBeVisible();
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
      await expect(tools.getByRole("button", { name: /^Show document references/ })).toHaveCount(0);
      await expect(tools.getByRole("button", { name: "Find in document", exact: true })).toBeVisible();
      const targets = await tools.locator('[data-reader-control]:visible').evaluateAll(elements => elements.map(element => ({ width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height })));
      expect(targets.length).toBeGreaterThan(3);
      expect(targets.every(target => target.width >= 44 && target.height >= 44)).toBe(true);
      expect((await page.locator(".reading-workspace-bottom").boundingBox())!.height).toBe(48);
      const close = (await page.getByRole("button", { name: "Close Reading workflow", exact: true }).boundingBox())!;
      expect(close.width).toBeGreaterThanOrEqual(44);
      expect(close.height).toBeGreaterThanOrEqual(44);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await readerNavigation(page);
      await expect(page.getByRole("dialog", { name: "Contents", exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Reference map", exact: true }).click();
      await expect(page.getByRole("group", { name: "Direct connections to Reading workflow", exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Close contents", exact: true }).click();
      await expect(page.getByRole("button", { name: "Open contents", exact: true })).toBeFocused();
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
  const details = page.locator("#document-reading-canvas").getByRole("button", { name: "Details", exact: true });
  await details.click();
  await expect(page.getByRole("tab", { name: "Properties", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close details", exact: true }).click();
  await expect(details).toBeFocused();
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
