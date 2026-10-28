import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures, never a user's data.");

async function fixture(page: Page, dark: boolean, role: "owner" | "reader" = "owner") {
  await page.addInitScript((dark) => {
    localStorage.setItem("akb_token", "composer-settings-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, dark);
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "writing-fixture", username: "reviewer", display_name: "Workspace reviewer", email: "reviewer@example.invalid", auth_method: "local", is_admin: false } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "writing-vault", name: "fixture", role }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: {
      name: "fixture", description: "A shared space for decisions, product notes, and operating guides.",
      role, public_access: "none", owner_display_name: "Workspace reviewer",
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
    if (path.includes("/help/vault-skill-preview/")) return route.fulfill({ contentType: "text/plain", body: "# Agent context\n\nUse this vault for team decisions." });
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
    // The lazy editor replaces a taller fallback. Measure after it mounts so
    // the two rectangles cannot straddle that layout change on a cold runner.
    const body = composer.locator('[contenteditable="true"]').first();
    await expect(body).toBeVisible();
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

  test(`settings presents a compact connected sheet without nested rails at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, dark);
    await page.goto("/vault/fixture/settings");
    const general = page.getByRole("region", { name: "Vault details", exact: true });
    const access = page.getByRole("region", { name: "Access permissions", exact: true });
    const guide = page.getByRole("region", { name: "Vault guide", exact: true });
    const danger = page.getByRole("region", { name: "Danger zone", exact: true });
    for (const [region, id] of [[general, "general"], [access, "access"], [guide, "skill"], [danger, "danger"]] as const) {
      await expect(region).toBeVisible();
      await expect(region).toHaveAttribute("id", id);
      const group = region.locator('[data-slot="settings-group"]');
      await expect(group).toHaveCount(1);
      await expect(group).toHaveCSS("border-top-width", "0px");
      expect(await region.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    }
    const sections = [general, access, guide, danger];
    for (let index = 1; index < sections.length; index++) {
      const previousBorder = await sections[index - 1].evaluate(node => parseFloat(getComputedStyle(node).borderBottomWidth));
      const currentBorder = await sections[index].evaluate(node => parseFloat(getComputedStyle(node).borderTopWidth));
      expect(previousBorder + currentBorder).toBeGreaterThanOrEqual(1);
    }
    const wideForm = await page.getByTestId("settings-workspace-frame").evaluate(node =>
      node.clientWidth >= 64 * parseFloat(getComputedStyle(document.documentElement).fontSize));
    for (const region of [general, access]) {
      const headingBox = (await region.getByRole("heading").boundingBox())!;
      const groupBox = (await region.locator('[data-slot="settings-group"]').boundingBox())!;
      if (wideForm) expect(groupBox.x).toBeGreaterThan(headingBox.x + headingBox.width);
      else expect(groupBox.y).toBeGreaterThanOrEqual(headingBox.y + headingBox.height);
      await expect(region.locator('[data-slot="settings-row"]')).toHaveCount(0);
    }
    await expect(page.getByRole("tablist")).toHaveCount(0);
    await expect(page.getByRole("tabpanel")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Save changes", exact: true })).toHaveCount(0);
    const details = page.getByRole("complementary", { name: "Vault details" });
    await expect(details).toHaveCount(0);
    await expect(general.getByRole("button", { name: "Copy vault address", exact: true })).toBeVisible();
    await expect(page.getByLabel("Vault ownership and dates", { exact: true })).toHaveCount(0);
    const explorer = page.getByRole("navigation", { name: "fixture explorer", exact: true });
    if (width >= 1024) {
      await expect(explorer).toBeVisible();
      await expect(page.getByRole("button", { name: "Switch vault: fixture", exact: true })).toBeVisible();
    } else {
      await expect(page.locator("#vault-workspace-navigation")).toBeHidden();
      await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
      await expect(explorer).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toBeFocused();
    }
    await expect(page.getByRole("navigation", { name: "Vault sections" })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("settings-general.png"), animations: "disabled" });
    const description = page.getByLabel("Description", { exact: true });
    const descriptionBox = (await description.boundingBox())!;
    expect(descriptionBox.height).toBeLessThanOrEqual(150);
    await description.fill("A draft description that must survive working across the settings sheet.");
    const policy = page.getByRole("radiogroup", { name: "Who can access", exact: true });
    const firstChoice = policy.getByRole("radio", { name: "Invited members only", exact: true });
    const lastChoice = policy.getByRole("radio", { name: "Everyone can edit", exact: true });
    await expect(firstChoice).toBeChecked();
    await expect(policy).toHaveCSS("border-top-width", "1px");
    const choices = await policy.locator("label").all();
    for (let index = 1; index < choices.length; index++) {
      const previous = choices[index - 1];
      const current = choices[index];
      const horizontal = (await current.boundingBox())!.x > (await previous.boundingBox())!.x;
      const previousBorder = await previous.evaluate((node, horizontal) => parseFloat(getComputedStyle(node)[horizontal ? "borderRightWidth" : "borderBottomWidth"]), horizontal);
      const currentBorder = await current.evaluate((node, horizontal) => parseFloat(getComputedStyle(node)[horizontal ? "borderLeftWidth" : "borderTopWidth"]), horizontal);
      expect(previousBorder + currentBorder).toBeGreaterThanOrEqual(1);
    }
    await expect(policy).toHaveAccessibleDescription("Only people invited to this vault.");
    await expect(lastChoice).toBeVisible();
    await firstChoice.focus();
    await page.keyboard.press("ArrowDown");
    const readingChoice = policy.getByRole("radio", { name: "Everyone can view", exact: true });
    await expect(readingChoice).toBeFocused();
    await expect(readingChoice).toBeChecked();
    await expect(policy).toHaveAccessibleDescription("Signed-in AKB users can read without an invitation.");
    await expect(general.getByRole("button", { name: "Save changes", exact: true })).toBeEnabled();
    await expect(access.getByRole("button", { name: "Save changes", exact: true })).toBeEnabled();
    await page.screenshot({ path: testInfo.outputPath("settings-access.png"), animations: "disabled" });
    await expect(guide.getByRole("region", { name: "Vault guide preview", exact: true })).toContainText("Keep decisions with their context.");
    await expect(guide.getByRole("textbox", { name: "Vault guide body", exact: true })).toHaveCount(0);
    await expect(guide.getByRole("link", { name: "Open guide", exact: true })).toHaveAttribute("href", "/vault/fixture/doc/overview%2Fvault-skill.md");
    await guide.getByRole("button", { name: "Edit guide", exact: true }).click();
    const guideBody = guide.getByRole("textbox", { name: "Vault guide body", exact: true });
    await guideBody.fill("# Draft guide\n\nKeep this draft while viewing a preview.");
    await guide.getByRole("button", { name: "Guide options", exact: true }).click();
    const options = page.getByRole("menu", { name: "Guide options", exact: true });
    await expect(options.getByRole("menuitem", { name: "History", exact: true })).toHaveAttribute("href", "/vault/fixture/doc/overview%2Fvault-skill.md?commit=fixture-guide-commit");
    await expect(options.getByRole("menuitem", { name: "Reset to template", exact: true })).toBeVisible();
    await options.getByRole("menuitem", { name: "Agent view", exact: true }).click();
    await expect(guide.getByRole("region", { name: "Agent view", exact: true })).toContainText("Use this vault for team decisions.");
    await guide.getByRole("button", { name: "Close agent view", exact: true }).click();
    await expect(guide.getByRole("region", { name: "Vault guide preview", exact: true })).toContainText("Keep decisions with their context.");
    await guide.getByRole("button", { name: "Edit guide", exact: true }).click();
    await expect(guideBody).toHaveValue("# Draft guide\n\nKeep this draft while viewing a preview.");
    await guide.getByRole("link", { name: "Open guide", exact: true }).click();
    const reader = page.getByRole("dialog", { name: "Document preview", exact: true });
    await expect(reader).toBeVisible();
    await expect(page).toHaveURL(/\/vault\/fixture\/doc\/overview%2Fvault-skill\.md$/);
    await expect(reader.getByRole("heading", { name: "Working together", exact: true })).toBeVisible();
    await reader.getByRole("button", { name: "Close dialog", exact: true }).click();
    await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
    await expect(guideBody).toHaveValue("# Draft guide\n\nKeep this draft while viewing a preview.");
    await guide.getByRole("button", { name: "Cancel", exact: true }).click();
    const discardGuide = page.getByRole("dialog", { name: "Discard unsaved guide changes?", exact: true });
    await discardGuide.getByRole("button", { name: "Keep editing", exact: true }).click();
    await expect(guideBody).toHaveValue("# Draft guide\n\nKeep this draft while viewing a preview.");
    await page.getByRole("navigation", { name: "Current page", exact: true }).getByRole("link", { name: "Back to fixture", exact: true }).click();
    const discardSettings = page.getByRole("dialog", { name: "Discard unsaved settings changes?", exact: true });
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await discardSettings.getByRole("button", { name: "Keep editing", exact: true }).click();
    await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
    await expect(guideBody).toHaveValue("# Draft guide\n\nKeep this draft while viewing a preview.");
    await expect(description).toHaveValue("A draft description that must survive working across the settings sheet.");
    await expect(readingChoice).toBeChecked();
    await page.screenshot({ path: testInfo.outputPath("settings-guide.png"), animations: "disabled" });
    await expect(danger.getByRole("button", { name: "Archive", exact: true })).toBeVisible();
    await expect(danger.getByRole("button", { name: "Delete vault", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Operations", exact: true })).toHaveCount(0);
    await expect(details).toHaveCount(0);
    await danger.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("settings-danger.png"), animations: "disabled" });
    await expect(page.getByRole("complementary", { name: "Vault settings context" })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

for (const width of [375, 1920]) test(`readers can read the Guide without settings mutation controls at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 1000 });
  await fixture(page, false, "reader");
  await page.goto("/vault/fixture/settings");
  await expect(page.getByText("Read only — only the vault owner can change settings.", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Description", { exact: true })).toHaveJSProperty("readOnly", true);
  const policy = page.getByRole("radiogroup", { name: "Who can access", exact: true });
  await expect(policy.getByRole("radio")).toHaveCount(3);
  for (const choice of await policy.getByRole("radio").all()) await expect(choice).toBeDisabled();
  const guide = page.getByRole("region", { name: "Vault guide", exact: true });
  await expect(guide.getByRole("region", { name: "Vault guide preview", exact: true })).toContainText("Keep decisions with their context.");
  await expect(guide.getByRole("link", { name: "Open guide", exact: true })).toHaveAttribute("href", "/vault/fixture/doc/overview%2Fvault-skill.md");
  await expect(guide.getByRole("button", { name: "Edit guide", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save changes", exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Danger zone", exact: true })).toHaveCount(0);
  await guide.getByRole("button", { name: "Guide options", exact: true }).click();
  const options = page.getByRole("menu", { name: "Guide options", exact: true });
  await expect(options.getByRole("menuitem", { name: "Agent view", exact: true })).toBeVisible();
  await expect(options.getByRole("menuitem", { name: "History", exact: true })).toBeVisible();
  await expect(options.getByRole("menuitem", { name: "Reset to template", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(guide.getByRole("button", { name: "Guide options", exact: true })).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("wide Settings keeps one scroll surface and a readable expanded Guide on short screens", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 });
  await fixture(page, false);
  const content = "# Working together\n\n" + Array.from({ length: 60 }, (_, index) => `## Decision ${index + 1}\n\n${"Keep decisions with their context. ".repeat(3)}`).join("\n\n");
  await page.route("**/api/v1/documents/fixture/**", route => route.fulfill({ json: {
    path: "overview/vault-skill.md", title: "Workspace guide", content,
    current_commit: "fixture-guide-commit", status: "active",
  } }));
  await page.goto("/vault/fixture/settings#skill");
  const frame = page.locator('[data-slot="vault-route-viewport"]');
  const sheet = page.getByTestId("settings-workspace-frame");
  const guide = page.getByRole("region", { name: "Vault guide", exact: true });
  const previewHeading = guide.getByRole("heading", { name: "Working together", exact: true });
  await expect(previewHeading).toBeVisible();
  const preview = guide.getByRole("region", { name: "Vault guide preview", exact: true });
  expect((await preview.boundingBox())!.width).toBeLessThanOrEqual(1024);
  await expect(guide.getByRole("heading", { name: "Decision 60", exact: true })).toBeHidden();
  const showMore = guide.getByRole("button", { name: "Show more", exact: true });
  await expect(showMore).toHaveAttribute("aria-expanded", "false");
  await showMore.click();
  await expect(guide.getByRole("button", { name: "Show less", exact: true })).toHaveAttribute("aria-expanded", "true");
  await expect(guide.getByRole("heading", { name: "Decision 60", exact: true })).toBeVisible();
  expect(await sheet.evaluate(node => node.scrollHeight <= node.clientHeight)).toBe(true);
  await expect.poll(() => frame.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  await frame.evaluate(node => { node.scrollTop = node.scrollHeight; });
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  await frame.evaluate(node => { node.scrollTop = 0; });
  await expect(page.getByLabel("Description", { exact: true })).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

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
