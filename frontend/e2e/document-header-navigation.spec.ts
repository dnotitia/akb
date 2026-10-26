import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP fixtures; no user Vault requests.");
test.beforeEach(async ({ page }) => { await page.emulateMedia({ reducedMotion: "reduce" }); });

const documentPath = "guides/reading.md";
const documentUrl = `/vault/fixture/doc/${encodeURIComponent(documentPath)}`;
const title = "Reading decisions for a shared workspace";
const destinations = [
  ["Overview", "/vault/fixture"],
  ["Graph", "/vault/fixture/graph"],
  ["Public links", "/vault/fixture/publications"],
  ["Members", "/vault/fixture/members"],
  ["Settings", "/vault/fixture/settings"],
] as const;

async function fixture(page: Page, dark = false, treeVisible = "1") {
  await page.addInitScript(({ dark, treeVisible }) => {
    localStorage.setItem("akb_token", "document-header-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    if (!localStorage.getItem("document-header-fixture-ready")) {
      localStorage.setItem("akb.treeVisible", treeVisible);
      localStorage.setItem("akb.focusedNavigationWidth.v1", "286");
      localStorage.setItem("document-header-fixture-ready", "1");
    }
  }, { dark, treeVisible });
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    let json: object = { items: [], history: [], changes: [], relations: [], subscribed: false, unread_count: 0 };
    if (path.endsWith("/auth/config")) json = { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
    else if (path.endsWith("/auth/me")) json = { user_id: "document-header-owner", username: "fixture", display_name: "Reading reviewer", email: "reviewer@example.invalid", auth_method: "local", is_admin: false };
    else if (path.endsWith("/vaults")) json = { vaults: [{ id: "document-header-vault", name: "fixture", role: "owner" }] };
    else if (path.endsWith("/info")) json = { name: "fixture", role: "owner", description: "Shared reading decisions.", public_access: "none", is_archived: false, is_external_git: false, document_count: 1, collection_count: 1 };
    else if (path.endsWith("/members")) json = { members: [{ user_id: "document-header-owner", username: "fixture", display_name: "Reading reviewer", email: "reviewer@example.invalid", role: "owner", since: "2026-09-01T00:00:00Z" }] };
    else if (path.includes("/browse/")) json = { items: [{ type: "collection", path: "guides", name: "Guides", doc_count: 1 }, { type: "document", path: documentPath, name: title }] };
    else if (path.includes("/publications/")) json = { publications: [] };
    else if (path.endsWith("/graph/overview")) json = { nodes: [], edges: [], nodes_total: 0, edges_total: 0, returned: 0, truncated: false };
    else if (decodeURIComponent(path).endsWith("/documents/fixture/overview/vault-skill.md")) json = { path: "overview/vault-skill.md", title: "Fixture vault guide", content: "# Fixture guide\n\nKeep shared decisions with their context.", status: "active", current_commit: "aaaaaaaaaaaaa", updated_at: "2026-09-01T00:00:00Z" };
    else if (path.includes("/documents/fixture/") && !path.endsWith("/history")) json = { uri: `akb://fixture/coll/guides/doc/reading.md`, path: documentPath, title, content: "# Reading context\n\nKeep decisions close to their source.", status: "active", current_commit: "aaaaaaaaaaaaa", updated_at: "2026-09-01T00:00:00Z" };
    return route.fulfill({ json });
  });
}

async function revealVaultNavigation(page: Page) {
  const trigger = page.getByRole("button", { name: "Open vault navigation", exact: true });
  await expect(page.locator("#vault-workspace-navigation:visible").or(trigger)).toBeVisible();
  const drawer = await trigger.isVisible();
  if (drawer) await trigger.click();
  return drawer;
}

function vaultBreadcrumb(page: Page) {
  return page.getByRole("navigation", { name: /^(Current page|Resource location)$/ })
    .getByRole("link", { name: /^(Back to )?fixture$/ });
}

async function expectOverviewDestinations(page: Page) {
  const navigation = page.getByRole("navigation", { name: "Vault pages", exact: true });
  await expect(navigation.getByRole("link")).toHaveCount(4);
  for (const [name, href] of destinations.filter(([name]) => name !== "Overview")) {
    await expect(navigation.getByRole("link", { name, exact: true })).toHaveAttribute("href", href);
  }
  await expect(page.getByRole("button", { name: "Navigate in fixture", exact: true })).toHaveCount(0);
}

for (const width of [375, 1440]) test(`title back follows the Vault hierarchy on direct visits at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await fixture(page, false, "0");
  for (const path of [documentUrl, ...destinations.slice(1).map(([, href]) => href), "/vault/fixture/search", "/vault/fixture/activity"]) {
    await page.goto(path);
    const parent = page.getByRole("link", { name: "Back to vault fixture", exact: true });
    await expect(parent).toHaveAttribute("href", "/vault/fixture");
    await parent.focus();
    await parent.press("Enter");
    await expect(page).toHaveURL(url => url.pathname === "/vault/fixture" && !url.search);
    await expectOverviewDestinations(page);
    await expect(page.getByRole("main")).toBeFocused();
    const directory = page.getByRole("link", { name: "Back to all vaults", exact: true });
    await expect(directory).toHaveAttribute("href", "/vault");
    await directory.click();
    await expect(page).toHaveURL(url => url.pathname === "/vault");
    await expect(page.locator("[data-vault-parent-back]")).toHaveCount(0);
    await expect(page.getByRole("main")).toBeFocused();
  }
});

for (const width of [375, 1440]) for (const source of ["title", "sidebar"] as const) test(`${source} back confirms a dirty document before leaving at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await fixture(page, false, "0");
  await page.goto(`${documentUrl}?view=edit`);
  const editor = page.getByRole("textbox", { name: "Document body (markdown)", exact: true });
  const draft = "Retain this draft until I confirm leaving.";
  await editor.fill(draft);
  const back = page.getByRole("link", { name: source === "sidebar" ? "Show vault list" : "Back to vault fixture", exact: true });
  if (source === "sidebar") await revealVaultNavigation(page);
  await back.click();
  const confirmation = page.getByRole("dialog", { name: "Leave this document?", exact: true });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "Keep editing", exact: true }).click();
  await expect(editor).toContainText(draft);
  await expect(page.getByRole("textbox", { name: "Document title", exact: true })).toBeFocused();
  if (source === "sidebar") await revealVaultNavigation(page);
  await back.click();
  await confirmation.getByRole("button", { name: "Leave document", exact: true }).click();
  await expect(page).toHaveURL(source === "sidebar" ? /\/vault$/ : /\/vault\/fixture$/);
  if (source === "sidebar") {
    await expect(page.getByRole("navigation", { name: "Vaults", exact: true })).toBeVisible();
    await expect(page.getByRole("searchbox", { name: "Find a vault in sidebar", exact: true })).toBeFocused();
  } else {
    await expect(page.getByRole("main")).toBeFocused();
    await expect(page.getByRole("link", { name: "Back to all vaults", exact: true })).toHaveAttribute("href", "/vault");
  }
});

for (const width of [375, 1440]) for (const [suffix, label] of [
  ["", "Overview"],
  ["/", "Overview"],
  ["/search", "Search"],
  ["/activity", "Activity"],
] as const) test(`Vault breadcrumb returns to Overview discovery on ${suffix || "Overview"} at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await fixture(page);
  await page.goto(`/vault/fixture${suffix}`);
  const location = page.getByRole("navigation", { name: "Current page", exact: true });
  await expect(location.locator('[aria-current="page"]')).toHaveText(label === "Overview" ? "fixture" : label);
  await expect(location.getByRole("button")).toHaveCount(0);
  await expect(vaultBreadcrumb(page)).toHaveAttribute("href", "/vault/fixture");
  await expect(vaultBreadcrumb(page)).toBeInViewport({ ratio: 1 });
  await expect(page.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Navigate in fixture", exact: true })).toHaveCount(0);
  if (label === "Overview") await expectOverviewDestinations(page);
  else await expect(page.getByRole("navigation", { name: "Vault pages", exact: true })).toHaveCount(0);
  const drawer = await revealVaultNavigation(page);
  await expect(page.locator('[data-slot="vault-collections-sidebar"]').getByRole("navigation", { name: "fixture explorer", exact: true })).toBeVisible();
  if (drawer) await page.keyboard.press("Escape");
  await vaultBreadcrumb(page).click();
  await expect(page).toHaveURL(url => url.pathname === "/vault/fixture");
  await expect(location.locator('[aria-current="page"]')).toHaveText("fixture");
  await expectOverviewDestinations(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const width of [375, 1440]) for (const dark of [false, true]) {
  test(`document header keeps reading tools and returns to Vault discovery at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await fixture(page, dark);
    await page.goto(documentUrl);
    const location = page.getByRole("navigation", { name: "Resource location", exact: true });
    const tools = page.getByRole("group", { name: "Document reading tools", exact: true });
    await expect(page.getByRole("heading", { name: "Reading context", exact: true })).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
    await expect(page.getByRole("navigation", { name: "Vault pages", exact: true })).toHaveCount(0);
    await expect(location).toContainText(title);
    await expect(location.getByRole("button", { name: title, exact: true })).toHaveCount(0);
    await expect(vaultBreadcrumb(page)).toBeInViewport({ ratio: 1 });
    const controls = [
      tools.getByRole("radio", { name: "Preview", exact: true }),
      tools.getByRole("radio", { name: "Raw markdown", exact: true }),
      tools.getByRole("button", { name: "Copy markdown", exact: true }),
      tools.getByRole("button", { name: "Edit", exact: true }),
      tools.getByRole("button", { name: "Document details", exact: true }),
      tools.getByRole("button", { name: `Actions for ${title}`, exact: true }),
    ];
    let previousRight = 0;
    for (const control of controls) {
      await expect(control).toBeInViewport({ ratio: 1 });
      const box = (await control.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(previousRight);
      previousRight = box.x + box.width;
    }
    await page.screenshot({ path: testInfo.outputPath("document-header.png"), animations: "disabled" });
    for (const [name, href] of destinations) {
      await vaultBreadcrumb(page).click();
      await expectOverviewDestinations(page);
      if (name !== "Overview") {
        const link = page.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link", { name, exact: true });
        await link.focus();
        await link.press("Enter");
        await expect(page.getByRole("navigation", { name: "Vault pages", exact: true })).toHaveCount(0);
      }
      await expect(page).toHaveURL(url => url.pathname === href);
      const currentPage = page.getByRole("navigation", { name: "Current page", exact: true });
      await expect(currentPage.locator('[aria-current="page"]')).toHaveText(name === "Overview" ? "fixture" : name);
      await expect(currentPage.getByRole("button")).toHaveCount(0);
      await page.goBack();
      if (name !== "Overview") await page.goBack();
      await expect(location).toContainText(title);
    }
    const actions = controls.at(-1)!;
    await actions.click();
    await page.getByRole("menuitem", { name: "Publish", exact: true }).click();
    const review = page.getByRole("dialog", { name: "Publish document", exact: true });
    await expect(review).toBeVisible();
    await expect(review.getByRole("button", { name: "Publish", exact: true })).toBeInViewport();
    await page.keyboard.press("Escape");
    await expect(actions).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

for (const treeVisible of ["0", "1"]) test(`document header navigation preserves content-panel preference ${treeVisible}`, async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await fixture(page, false, treeVisible);
  let finishIdentity = () => {};
  const identityPending = new Promise<void>(resolve => { finishIdentity = resolve; });
  await page.route("**/api/v1/auth/me", async route => {
    await identityPending;
    await route.fallback();
  });
  await page.goto(documentUrl);
  const panel = page.locator("#vault-workspace-navigation");
  const explorer = panel.getByRole("navigation", { name: "fixture explorer", exact: true });
  const expectSavedPanel = async () => {
    await expect(panel).toHaveCount(1);
    if (treeVisible === "1") {
      await expect(explorer).toBeVisible();
      await expect(page.getByRole("button", { name: "Close vault navigation", exact: true })).toHaveAttribute("aria-expanded", "true");
      expect((await panel.boundingBox())!.width).toBe(286);
    } else {
      await expect(panel).toBeHidden();
      await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toHaveAttribute("aria-expanded", "false");
    }
  };
  await expect(page.getByRole("status", { name: "Verifying session", exact: true })).toBeVisible();
  await expect(panel).toHaveCount(0);
  finishIdentity();
  await expectSavedPanel();
  await vaultBreadcrumb(page).click();
  await page.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link", { name: "Settings", exact: true }).click();
  // Focused tasks retain the same context panel and saved visibility.
  await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
  await expectSavedPanel();
  await page.goBack();
  await page.goBack();
  await expect(vaultBreadcrumb(page)).toBeVisible();
  await expectSavedPanel();
  expect(await page.evaluate(() => [localStorage.getItem("akb.treeVisible"), localStorage.getItem("akb.focusedNavigationWidth.v1")])).toEqual([treeVisible, "286"]);
});

for (const width of [375, 1440]) test(`modified pointer header links preserve the dirty editor at ${width}px`, async ({ page, context }) => {
  await context.route("**/api/v1/**", route => route.fulfill({ json: {} }));
  await page.setViewportSize({ width, height: 900 });
  await fixture(page);
  await page.goto(`${documentUrl}?view=edit`);
  const editor = page.getByRole("textbox", { name: "Document body (markdown)", exact: true });
  await editor.fill("Keep this draft in the original tab.");
  await expect(page.getByRole("group", { name: "Document publishing", exact: true })).toHaveCount(0);
  const popupPromise = context.waitForEvent("page");
  await page.getByRole("link", { name: "Back to vault fixture", exact: true }).click({ modifiers: ["ControlOrMeta"] });
  const popup = await popupPromise;
  await expect(popup).toHaveURL(/\/vault\/fixture$/);
  await popup.close();
  await expect(page).toHaveURL(/\?view=edit$/);
  await expect(page.getByRole("dialog", { name: "Leave this document?", exact: true })).toHaveCount(0);
  await expect(editor).toContainText("Keep this draft in the original tab.");
});

for (const width of [375, 1440]) test(`header navigation waits for an active image upload at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  await fixture(page);
  const assetId = "123e4567-e89b-42d3-a456-426614174000";
  const bytes = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64");
  let finishUpload = () => {};
  const pending = new Promise<void>(resolve => { finishUpload = resolve; });
  await page.route("**/api/v1/assets/fixture?*", async route => {
    await pending;
    await route.fulfill({ json: { id: assetId, name: "diagram.png", mime_type: "image/png", size_bytes: bytes.length, target: `/api/assets/${assetId}`, unclaimed_expires_at: "2099-01-01T00:00:00Z" } });
  });
  await page.route(`**/api/assets/${assetId}?*`, route => route.fulfill({ contentType: "image/png", body: bytes }));
  try {
    await page.goto(`${documentUrl}?view=edit`);
    await expect(page.getByRole("textbox", { name: "Document body (markdown)", exact: true })).toBeVisible();
    const chooserPromise = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Insert image", exact: true }).click();
    await (await chooserPromise).setFiles({ name: "diagram.png", mimeType: "image/png", buffer: bytes });
    await expect(page.getByText("Uploading image…", { exact: true })).toBeVisible();
    await page.getByRole("link", { name: "Back to vault fixture", exact: true }).click();
    const confirmation = page.getByRole("dialog", { name: "Leave this document?", exact: true });
    await expect(confirmation).toContainText("An image is still uploading");
    await expect(confirmation.getByRole("button", { name: "Leave document", exact: true })).toBeDisabled();
    await confirmation.getByRole("button", { name: "Keep editing", exact: true }).click();
    await expect(page).toHaveURL(/\?view=edit$/);
    await expect(page.getByRole("button", { name: "Save changes", exact: true })).toBeDisabled();
    finishUpload();
    await expect(page.getByRole("button", { name: "Remove image: diagram", exact: true })).toBeVisible();
    await page.getByRole("link", { name: "Back to vault fixture", exact: true }).click();
    await confirmation.getByRole("button", { name: "Leave document", exact: true }).click();
    await expect(page.getByRole("navigation", { name: "Current page", exact: true }).locator('[aria-current="page"]')).toHaveText("fixture");
  } finally {
    finishUpload();
  }
});

for (const width of [375, 1440]) for (const aggregate of [false, true]) {
  test(`Vault breadcrumb protects ${aggregate ? "all settings drafts" : "the description draft"} while local history remains usable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await fixture(page);
    const mutations: string[] = [];
    page.on("request", request => {
      const pathname = new URL(request.url()).pathname;
      if (!["GET", "HEAD", "OPTIONS"].includes(request.method())
        && (pathname.startsWith("/api/v1/vaults/fixture") || pathname.startsWith("/api/v1/documents/fixture/"))) mutations.push(pathname);
    });
    await page.goto("/vault/fixture/members");
    await vaultBreadcrumb(page).click();
    await page.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link", { name: "Settings", exact: true }).click();
    const trigger = aggregate ? page.getByRole("link", { name: "Back to vault fixture", exact: true }) : vaultBreadcrumb(page);
    const description = page.getByRole("textbox", { name: "Description", exact: true });
    const draft = "An unsaved description stays here until I choose to leave.";
    const guideDraft = "# Unsaved guide\n\nKeep this guide with the other settings drafts.";
    await expect(description).toHaveValue("Shared reading decisions.");
    await description.fill(draft);
    await page.evaluate(() => { location.hash = "access"; });
    await expect(page).toHaveURL(/\/settings#access$/);
    if (aggregate) {
      await page.getByRole("radio", { name: "Everyone can view", exact: true }).click();
      await page.evaluate(() => { location.hash = "skill"; });
      await page.getByRole("button", { name: "Edit guide", exact: true }).click();
      await page.getByRole("textbox", { name: "Vault guide body", exact: true }).fill(guideDraft);
      await page.goBack();
      await expect(page).toHaveURL(/\/settings#access$/);
    }
    await page.goBack();
    await expect(page).toHaveURL(/\/vault\/fixture\/settings$/);
    await expect(description).toHaveValue(draft);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    if (aggregate) {
      await page.goForward();
      await expect(page.getByRole("radio", { name: "Everyone can view", exact: true })).toBeChecked();
      await page.goForward();
      await expect(page.getByRole("textbox", { name: "Vault guide body", exact: true })).toHaveValue(guideDraft);
      await expect(page.getByRole("dialog")).toHaveCount(0);
    }
    await trigger.click();
    const confirmation = page.getByRole("dialog", { name: "Discard unsaved settings changes?", exact: true });
    await expect(confirmation).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await expect(page.getByRole("navigation", { name: "Vault pages", exact: true })).toHaveCount(0);
    await expect(page).toHaveURL(url => url.pathname === "/vault/fixture/settings");
    await confirmation.getByRole("button", { name: "Keep editing", exact: true }).click();
    await expect(trigger).toBeFocused();
    if (aggregate) {
      await expect(page.getByRole("textbox", { name: "Vault guide body", exact: true })).toHaveValue(guideDraft);
    }
    await expect(description).toHaveValue(draft);
    if (aggregate) {
      await expect(page.getByRole("radio", { name: "Everyone can view", exact: true })).toBeChecked();
      await expect(page.getByRole("textbox", { name: "Vault guide body", exact: true })).toHaveValue(guideDraft);
    }
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await trigger.click();
    await confirmation.getByRole("button", { name: "Discard changes and leave", exact: true }).click();
    await expectOverviewDestinations(page);
    await page.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link", { name: "Members", exact: true }).click();
    await expect(page).toHaveURL(/\/vault\/fixture\/members$/);
    await expect(page.getByRole("navigation", { name: "Current page", exact: true }).locator('[aria-current="page"]')).toHaveText("Members");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(mutations).toEqual([]);
    await page.goBack();
    await page.goBack();
    await expect(description).toHaveValue("Shared reading decisions.");
    expect(mutations).toEqual([]);
  });
}
