import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");
const path = "guides/reading.md";
const title = "Team operating guide";
const uri = "akb://fixture/coll/guides/doc/reading.md";

async function fixture(page: Page, dark = false, content = "# Reading\n\nKeep this document in context.") {
  const patches: Record<string, unknown>[] = [];
  let document = { uri, path, title, content, summary: "Original summary", type: "guide", domain: "engineering", tags: ["team"], status: "active", current_commit: "aaaaaaaaaaaaa", is_public: false };
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "authoring-continuity-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, { dark });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (pathname.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "continuity-user", username: "fixture", display_name: "Author", auth_method: "local", is_admin: false } });
    if (pathname.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "fixture-vault", name: "fixture", role: "writer" }] } });
    if (pathname.endsWith("/info")) return route.fulfill({ json: { name: "fixture", role: "writer", is_archived: false, is_external_git: false, document_count: 1 } });
    if (pathname.includes("/browse/")) return route.fulfill({ json: { items: [{ type: "collection", name: "Guides", path: "guides" }, { type: "document", name: document.title, path }] } });
    if (pathname.endsWith("/search")) return route.fulfill({ json: { query: "reading", total: 1, results: [{ title: document.title, uri, vault: "fixture", path, source_type: "document", score: 1 }] } });
    if (pathname.includes("/documents/fixture/") && !pathname.endsWith("/history")) {
      if (route.request().method() === "PATCH") {
        const patch = route.request().postDataJSON();
        patches.push(patch);
        document = { ...document, ...patch, current_commit: "bbbbbbbbbbbbb" };
      }
      return route.fulfill({ json: document });
    }
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
  return patches;
}

async function openPreview(page: Page) {
  await page.goto("/search?q=reading&source=document");
  await page.getByRole("link", { name: /Team operating guide/ }).first().click();
  const preview = page.getByTestId("document-preview-dialog");
  await expect(preview).toBeVisible();
  return preview;
}

test("preview stays open during edit, protects outside/X/Escape, and saves details in place", async ({ page }) => {
  const patches = await fixture(page);
  const preview = await openPreview(page);
  await preview.getByRole("button", { name: "Edit", exact: true }).click();
  await preview.getByRole("textbox", { name: "Summary", exact: true }).fill("Updated in the preview");
  await page.mouse.click(2, 2);
  await expect(preview).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Leave this document?" })).toHaveCount(0);
  await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Leave this document?" })).toBeVisible();
  await page.getByRole("button", { name: "Keep editing", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Leave this document?" })).toBeVisible();
  await page.getByRole("button", { name: "Keep editing", exact: true }).click();
  await preview.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(preview.getByRole("button", { name: "Edit", exact: true })).toBeVisible();
  expect(patches).toEqual([{ summary: "Updated in the preview", expected_commit: "aaaaaaaaaaaaa" }]);
  await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
  await expect(page).toHaveURL(/\/search\?q=reading&source=document$/);
});

test("browser Back does not discard an edit and can be cancelled or confirmed", async ({ page }) => {
  await fixture(page);
  const preview = await openPreview(page);
  await preview.getByRole("button", { name: "Edit", exact: true }).click();
  await preview.getByRole("textbox", { name: "Summary", exact: true }).fill("Keep this local draft");
  await page.evaluate(() => history.back());
  await expect(page.getByRole("dialog", { name: "Leave this document?" })).toBeVisible();
  await page.getByRole("button", { name: "Keep editing", exact: true }).click();
  await expect(preview.getByRole("textbox", { name: "Summary", exact: true })).toHaveValue("Keep this local draft");
  await page.evaluate(() => history.back());
  await page.getByRole("button", { name: "Leave document", exact: true }).click();
  await expect(preview).toHaveCount(0);
  await expect(page).toHaveURL(/\/search\?q=reading&source=document$/);
  await page.getByRole("link", { name: /Team operating guide/ }).first().click();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Summary", exact: true })).toHaveValue("Keep this local draft");
});

test("returning from a confirmed search preview re-arms the background editor's Back protection", async ({ page }) => {
  await fixture(page);
  const initialPreview = await openPreview(page);
  await initialPreview.getByRole("button", { name: "Open document in vault", exact: true }).click();
  await expect(initialPreview).toHaveCount(0);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByRole("textbox", { name: "Summary", exact: true }).fill("Keep the background draft");
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  const search = page.getByTestId("global-search-dialog");
  await search.getByRole("combobox").fill("reading");
  await search.getByRole("option").filter({ hasText: title }).first().click();
  await page.getByRole("button", { name: "Leave document", exact: true }).click();
  const preview = page.getByTestId("document-preview-dialog");
  await expect(preview).toBeVisible();
  await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
  await expect(search).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("textbox", { name: "Summary", exact: true })).toHaveValue("Keep the background draft");
  await page.evaluate(() => history.back());
  await expect(page.getByRole("dialog", { name: "Leave this document?" })).toBeVisible();
  await page.getByRole("button", { name: "Keep editing", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Summary", exact: true })).toHaveValue("Keep the background draft");
});

test("image file selection stays in the preview and uploading blocks every exit", async ({ page }) => {
  const patches = await fixture(page);
  const assetId = "123e4567-e89b-42d3-a456-426614174000";
  const bytes = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64");
  let finishUpload: () => void = () => {};
  const upload = new Promise<void>(resolve => { finishUpload = resolve; });
  await page.route("**/api/v1/assets/fixture?*", async route => {
    await upload;
    await route.fulfill({ json: { id: assetId, name: "diagram.png", mime_type: "image/png", size_bytes: bytes.length, target: `/api/assets/${assetId}`, unclaimed_expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route(`**/api/assets/${assetId}?*`, route => route.fulfill({ contentType: "image/png", body: bytes }));
  const preview = await openPreview(page);
  await preview.getByRole("button", { name: "Edit", exact: true }).click();
  await expect(preview.locator('input[type="file"]')).toHaveCount(1);
  // Trigger the real file chooser, including blur/focus outside the web page.
  const chooserPromise = page.waitForEvent("filechooser");
  await preview.getByRole("button", { name: "Insert image", exact: true }).click();
  const chooser = await chooserPromise;
  await chooser.setFiles({ name: "diagram.png", mimeType: "image/png", buffer: bytes });
  await expect(preview.getByText("Uploading image…", { exact: true })).toBeVisible();
  await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
  await expect(page.getByRole("button", { name: "Leave document", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Keep editing", exact: true }).click();
  await expect(preview.getByRole("button", { name: "Save changes", exact: true })).toBeDisabled();
  finishUpload();
  await expect(preview.getByRole("button", { name: "Remove image: diagram" })).toBeVisible();
  await preview.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(preview.getByRole("button", { name: "Edit", exact: true })).toBeVisible();
  expect(patches.at(-1)?.content).toContain(`/api/assets/${assetId}`);
  expect(patches.at(-1)?.content).not.toContain("blob:");
});

test("failed save keeps title/body/details, and a pending save cannot be dismissed", async ({ page }) => {
  await fixture(page);
  let finishSave: () => void = () => {};
  const save = new Promise<void>(resolve => { finishSave = resolve; });
  await page.route("**/api/v1/documents/fixture/**", async route => {
    if (route.request().method() !== "PATCH") return route.fallback();
    await save;
    await route.fulfill({ status: 500, json: { detail: "Fixture unavailable" } });
  });
  const preview = await openPreview(page);
  await preview.getByRole("button", { name: "Edit", exact: true }).click();
  await preview.getByRole("textbox", { name: "Summary", exact: true }).fill("Keep this summary");
  await preview.getByRole("button", { name: "Save changes", exact: true }).click();
  await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
  await expect(page.getByRole("button", { name: "Leave document", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Keep editing", exact: true }).click();
  finishSave();
  await expect(preview.getByText("The server hit an error while saving. Please retry.")).toBeVisible();
  await expect(preview.getByRole("textbox", { name: "Summary", exact: true })).toHaveValue("Keep this summary");
  await expect(preview.getByRole("textbox", { name: "Document title", exact: true })).toHaveValue(title);
  await expect(preview.getByRole("textbox", { name: "Document body (markdown)", exact: true })).toContainText("Keep this document in context.");
  await preview.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Discard changes", exact: true }).click();
  await expect(preview.getByRole("button", { name: "Edit", exact: true })).toBeVisible();
});

for (const presentation of ["page", "preview"]) test(`${presentation} returns to its reading position and Raw mode after editing`, async ({ page }) => {
  await fixture(page, false, Array.from({ length: 40 }, (_, index) => `## Section ${index}\n\nKeep reading this long guide.`).join("\n\n"));
  if (presentation === "preview") await openPreview(page);
  else await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  const canvas = page.locator("#document-reading-canvas");
  await expect(canvas.getByRole("heading", { name: "Section 39", exact: true })).toBeAttached();
  await canvas.evaluate(element => { element.scrollTop = 500; });
  const position = await canvas.evaluate(element => element.scrollTop);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Document body (markdown)", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toBeFocused();
  await expect.poll(() => canvas.evaluate(element => element.scrollTop)).toBe(position);
  await page.getByRole("tab", { name: "Raw", exact: true }).click();
  await expect(page.getByRole("tab", { name: "Raw", exact: true })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByRole("textbox", { name: "Summary", exact: true }).fill("Updated summary");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("tab", { name: "Raw", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("document-preview-dialog")).toHaveCount(presentation === "preview" ? 1 : 0);
});

for (const scenario of [{ width: 1440, height: 900, dark: false }, { width: 2560, height: 1440, dark: true }, { width: 375, height: 812, dark: false }]) {
  test(`shared authoring rail fits ${scenario.width}px ${scenario.dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize(scenario);
    await fixture(page, scenario.dark);
    const preview = await openPreview(page);
    await preview.getByRole("button", { name: "Edit", exact: true }).click();
    const writing = preview.locator(".document-authoring-writing");
    const details = preview.getByRole("complementary", { name: "Document details", exact: true });
    await expect(preview.getByRole("textbox", { name: "Document body (markdown)", exact: true })).toBeVisible();
    const a = (await writing.boundingBox())!;
    const b = (await details.boundingBox())!;
    if (scenario.width >= 1440) {
      expect(Math.abs(a.x + a.width - b.x)).toBeLessThanOrEqual(1);
      expect(b.width).toBeGreaterThanOrEqual(288);
      expect(b.width).toBeLessThanOrEqual(320);
    } else {
      expect(b.y).toBeGreaterThanOrEqual(a.y + a.height - 1);
      await preview.getByRole("button", { name: "Document details", exact: true }).click();
      await expect(preview.getByRole("textbox", { name: "Summary", exact: true })).toBeFocused();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("authoring-rail.png") });
  });
}
