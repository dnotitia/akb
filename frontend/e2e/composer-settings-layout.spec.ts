import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures, never a user's data.");

async function fixture(page: Page, dark: boolean) {
  await page.addInitScript((dark) => {
    localStorage.setItem("akb_token", "composer-settings-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, dark);
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "writing-fixture", username: "reviewer", display_name: "Workspace reviewer", email: "reviewer@example.invalid", auth_method: "local", is_admin: false } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "writing-vault", name: "fixture", role: "owner" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: {
      name: "fixture", description: "A shared space for decisions, product notes, and operating guides.",
      role: "owner", public_access: "none", owner_display_name: "Workspace reviewer",
      created_at: "2026-09-01T09:00:00Z", last_activity: "2026-09-24T08:30:00Z",
      is_archived: false, is_external_git: false, member_count: 4,
      collection_count: 2, document_count: 12, table_count: 1, file_count: 3,
    } });
    if (path.includes("/browse/")) return route.fulfill({ json: { items: [
      { type: "collection", path: "notes", name: "notes" },
      { type: "collection", path: "guides", name: "guides" },
      { type: "document", path: "notes/decisions.md", name: "Product decisions" },
    ] } });
    if (path.includes("/documents/fixture/")) return route.fulfill({ json: {
      path: "overview/vault-skill.md", title: "Workspace guide", content: "# Working together\n\nKeep decisions with their context.",
      current_commit: "fixture-guide-commit", status: "active",
    } });
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
}

for (const width of [375, 1440, 2560]) for (const dark of [false, true]) {
  test(`composer separates writing from its details rail at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, dark);
    await page.goto("/vault/fixture/doc/new?collection=notes");
    const composer = page.getByRole("dialog", { name: "New document", exact: true });
    await expect(composer).toBeVisible();
    await expect(composer.getByText("fixture", { exact: true })).toBeVisible();
    const writing = composer.getByRole("main", { name: "Document composition", exact: true });
    const details = composer.getByRole("complementary", { name: "Document details", exact: true });
    await expect(details).toBeVisible();
    const collection = composer.getByLabel(/^Collection/);
    await expect(collection).toHaveValue("notes");
    const writingBox = (await writing.boundingBox())!;
    const detailsBox = (await details.boundingBox())!;
    if (width >= 1440) {
      expect(detailsBox.x).toBeGreaterThanOrEqual(writingBox.x + writingBox.width - 1);
      expect(Math.abs(detailsBox.y - writingBox.y)).toBeLessThanOrEqual(1);
      expect(detailsBox.width).toBeGreaterThanOrEqual(280);
      expect(detailsBox.width).toBeLessThanOrEqual(321);
      await expect(collection).toBeInViewport({ ratio: 1 });
    } else {
      expect(detailsBox.y).toBeGreaterThanOrEqual(writingBox.y + writingBox.height - 1);
      await expect(composer.getByRole("button", { name: "Edit collection: notes", exact: true })).toBeInViewport({ ratio: 1 });
    }
    await composer.getByLabel(/^Title/).fill("Working together: product decisions");
    const body = composer.locator('[contenteditable="true"]').first();
    await expect(body).toBeVisible();
    await body.fill("Write decisions with their context so the next person can pick up where you left off.");
    await expect(composer.getByRole("button", { name: "Create document" })).toBeEnabled();
    const titleBox = (await composer.getByLabel(/^Title/).boundingBox())!;
    expect(titleBox.x - writingBox.x).toBeGreaterThanOrEqual(12);
    expect(titleBox.x - writingBox.x).toBeLessThanOrEqual(24);
    const bodyBox = (await body.boundingBox())!;
    const bodyPadding = await body.evaluate(node => parseFloat(getComputedStyle(node).paddingLeft));
    expect(Math.abs(titleBox.x - bodyBox.x - bodyPadding)).toBeLessThanOrEqual(2);
    await page.screenshot({ path: testInfo.outputPath("composer.png"), animations: "disabled" });
    await composer.getByRole("button", { name: /^Editor mode:/ }).click();
    await page.getByRole("menuitemradio", { name: "Markdown", exact: true }).click();
    const source = composer.getByRole("textbox", { name: "Content (required)" });
    await expect(source).toHaveValue(/Write decisions/);
    const sourceBox = (await source.boundingBox())!;
    const sourcePadding = await source.evaluate(node => parseFloat(getComputedStyle(node).paddingLeft));
    expect(Math.abs(titleBox.x - sourceBox.x - sourcePadding)).toBeLessThanOrEqual(2);
    await composer.getByRole("button", { name: /^Editor mode:/ }).click();
    await page.getByRole("menuitemradio", { name: "Visual", exact: true }).click();
    if (width < 1440) {
      await composer.getByRole("button", { name: "Edit collection: notes", exact: true }).click();
      await expect(collection).toBeFocused();
      await expect(collection).toBeInViewport({ ratio: 1 });
    }
    await composer.getByLabel("Summary", { exact: true }).fill("Decisions and context");
    await composer.getByLabel("Domain", { exact: true }).fill("engineering");
    await expect(body).toContainText("Write decisions");
    await expect(composer.getByLabel("Summary", { exact: true })).toHaveValue("Decisions and context");
    await expect(composer.getByRole("button", { name: "Create document" })).toBeInViewport({ ratio: 1 });
    expect(await composer.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  });

  test(`settings shows one task without nested rails at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, dark);
    await page.goto("/vault/fixture/settings");
    const tabs = page.getByRole("tablist", { name: "Vault settings sections" });
    await expect(tabs).toBeVisible();
    const workspace = (await page.getByTestId("settings-workspace-shell").boundingBox())!;
    const generalTab = (await page.getByRole("tab", { name: "General", exact: true }).boundingBox())!;
    expect(generalTab.x - workspace.x).toBeLessThanOrEqual(16);
    const panel = (await page.getByRole("tabpanel").boundingBox())!;
    expect(panel.x - workspace.x).toBeLessThanOrEqual(24);
    const details = page.getByRole("complementary", { name: "Vault details" });
    await expect(details).toBeVisible();
    await expect(details.getByText("Workspace reviewer", { exact: true })).toBeVisible();
    const detailsBounds = (await details.boundingBox())!;
    if (width >= 1440) {
      expect(detailsBounds.x).toBeGreaterThanOrEqual(panel.x + panel.width + 20);
      expect(Math.abs(detailsBounds.y - panel.y)).toBeLessThanOrEqual(2);
    } else {
      expect(detailsBounds.y).toBeGreaterThanOrEqual(panel.y + panel.height + 20);
    }
    await expect(details.getByRole("link", { name: "Open Vault Overview" })).toHaveAttribute("href", "/vault/fixture");
    await expect(page.getByRole("tabpanel")).toHaveCount(1);
    await expect(page.getByRole("tab", { name: "General", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.screenshot({ path: testInfo.outputPath("settings-general.png"), animations: "disabled" });
    const description = page.getByLabel("Description", { exact: true });
    await description.fill("A draft description that must survive switching sections.");
    await page.getByRole("tab", { name: "Access", exact: true }).click();
    await expect(page).toHaveURL(/#access$/);
    await expect(page.getByRole("tabpanel")).toHaveCount(1);
    await page.screenshot({ path: testInfo.outputPath("settings-access.png"), animations: "disabled" });
    await page.goBack();
    await expect(page.getByRole("tab", { name: "General", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(description).toHaveValue("A draft description that must survive switching sections.");
    await page.getByRole("tab", { name: "Advanced", exact: true }).click();
    await expect(page.getByRole("tab", { name: "Advanced", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { name: "Operations", exact: true })).toBeVisible();
    await expect(details).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("settings-advanced.png"), animations: "disabled" });
    const bounds = (await tabs.boundingBox())!;
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    await expect(page.getByRole("complementary", { name: "Vault settings context" })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("composer rail scrolls independently and keeps required destination reachable on short screens", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 });
  await fixture(page, false);
  await page.goto("/vault/fixture/doc/new");
  const composer = page.getByRole("dialog", { name: "New document", exact: true });
  const writing = composer.getByRole("main", { name: "Document composition" });
  const details = composer.getByRole("complementary", { name: "Document details" });
  const collection = details.getByLabel(/^Collection/);
  await expect(collection).toBeInViewport({ ratio: 1 });
  await expect(collection).toHaveValue("");
  await details.getByLabel("Tags", { exact: true }).fill("review");
  await details.getByLabel("Tags", { exact: true }).press("Enter");
  expect(await details.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
  expect(await writing.evaluate(node => node.scrollTop)).toBe(0);
  await expect(composer.getByRole("button", { name: "Create document" })).toBeInViewport({ ratio: 1 });
  await page.setViewportSize({ width: 375, height: 600 });
  const destination = composer.getByRole("button", { name: "Edit collection: Choose a collection" });
  await expect(destination).toBeInViewport({ ratio: 1 });
  await destination.click();
  await expect(collection).toBeFocused();
  await expect(collection).toBeInViewport({ ratio: 1 });
  await collection.fill("notes");
  await expect(composer.getByRole("button", { name: "Edit collection: notes" })).toBeInViewport({ ratio: 1 });
  await expect(details.getByRole("button", { name: "Remove tag review" })).toBeVisible();
});
