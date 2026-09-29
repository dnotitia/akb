import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");

async function setup(page: Page, { dark = false, sso = false, admin = false } = {}) {
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "settings-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, { dark });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: sso ? "sso" : "local", local_auth: { enabled: !sso }, keycloak: { enabled: sso, browser_session_ready: sso }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "settings-reader", username: "reader", display_name: "Settings reviewer", email: "reader@example.invalid", is_admin: admin, auth_method: sso ? "browser_session" : "jwt", key_class: null } });
    if (path.endsWith("/my/vaults")) return route.fulfill({ json: { vaults: [] } });
    if (path.endsWith("/auth/tokens")) return route.fulfill({ json: { tokens: [] } });
    if (path.endsWith("/search")) return route.fulfill({ json: { results: [], total: 0, query: "retained" } });
    return route.fulfill({ json: { items: [], users: [], vaults: [], unread_count: 0 } });
  });
}

test("settings opens over the current workspace and restores it on close", async ({ page }, testInfo) => {
  await setup(page);
  await page.goto("/search?q=retained");
  const query = page.getByRole("searchbox", { name: "Search query" });
  await expect(query).toHaveValue("retained");
  const trigger = page.getByTestId("app-sidebar").getByRole("button", { name: "Settings", exact: true });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await expect(dialog).toBeVisible();
  await expect(page).toHaveURL(/\/search\?q=retained$/);
  await expect(dialog.getByLabel("Display name")).toHaveValue("Settings reviewer");
  await expect(dialog.getByRole("tab", { name: "Profile", exact: true })).toHaveAttribute("aria-selected", "true");
  await page.screenshot({ path: testInfo.outputPath("settings-profile.png") });
  await dialog.getByRole("button", { name: "Close settings", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(query).toHaveValue("retained");
  await expect(trigger).toBeFocused();
});

test("unsaved profile changes protect Escape, dismissal and section changes", async ({ page }) => {
  await setup(page);
  await page.goto("/settings");
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await dialog.getByLabel("Display name").fill("Unsaved name");
  await page.keyboard.press("Escape");
  const confirm = page.getByRole("dialog", { name: "Discard unsaved changes?" });
  await expect(confirm).toBeVisible();
  await confirm.getByRole("button", { name: "Keep editing" }).click();
  await expect(dialog.getByLabel("Display name")).toHaveValue("Unsaved name");
  await dialog.getByRole("tab", { name: "Appearance", exact: true }).click();
  await expect(confirm).toBeVisible();
  await confirm.getByRole("button", { name: "Discard changes" }).click();
  await expect(dialog.getByRole("radio", { name: "Light", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page).toHaveURL(/\/$/);
});

for (const width of [375, 1440]) for (const dark of [false, true]) test(`settings deep link and theme controls fit ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await setup(page, { dark });
  await page.setViewportSize({ width, height: 800 });
  await page.goto("/settings?tab=preferences");
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("radio", { name: dark ? "Dark" : "Light", exact: true })).toBeChecked();
  await expect(dialog.getByRole("button", { name: "Close settings" })).toBeInViewport({ ratio: 1 });
  expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  const bounds = (await dialog.boundingBox())!;
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(800);
  await page.screenshot({ path: testInfo.outputPath("settings-appearance.png") });
});

test("managed profiles stay read-only and cannot reveal administrator settings", async ({ page }) => {
  await setup(page, { sso: true });
  await page.goto("/settings?tab=admin");
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await expect(dialog.getByLabel("Display name")).toBeDisabled();
  await expect(dialog.getByRole("tab", { name: "Administration" })).toHaveCount(0);
  await expect(dialog.getByLabel("Current password", { exact: true })).toHaveCount(0);
});

test("agent connection deep links preserve all three setup phases", async ({ page }) => {
  await setup(page);
  await page.goto("/settings?tab=tokens");
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await expect(dialog.getByRole("heading", { name: "1. Prepare access" })).toBeVisible();
  await expect(dialog.getByRole("heading", { name: /2. Configure/ })).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "3. Try it in your agent" })).toBeVisible();
  await expect(dialog.getByText("No tokens yet", { exact: true })).toBeVisible();
});

test("account menu opens settings in place and outside dismissal restores focus without layout shift", async ({ page }) => {
  await setup(page);
  await page.goto("/search?q=retained");
  const account = page.getByRole("button", { name: "Account menu — Settings reviewer" });
  const before = (await account.boundingBox())!;
  await account.click();
  await page.getByRole("menuitem", { name: "Settings", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await expect(dialog.getByLabel("Display name")).toBeVisible();
  // The underlying account trigger is inert but must not shift when scroll locks.
  const during = (await page.locator("#account-menu-trigger").boundingBox())!;
  expect(during.x).toBeCloseTo(before.x, 0);
  await page.mouse.click(10, 10);
  await expect(dialog).toHaveCount(0);
  await expect(account).toBeFocused();
  await expect(page).toHaveURL(/\/search\?q=retained$/);
});

test("pending profile saves block dismissal and a failed save retains the draft", async ({ page }) => {
  await setup(page);
  let release!: () => void;
  const response = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/v1/auth/me", async route => {
    if (route.request().method() !== "PATCH") return route.fallback();
    await response;
    await route.fulfill({ status: 500, json: { detail: "Profile update failed" } });
  });
  await page.goto("/settings");
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await dialog.getByLabel("Display name").fill("Keep this draft");
  await dialog.getByRole("button", { name: "Save profile" }).click();
  try {
    await expect(dialog.getByRole("button", { name: "Close settings" })).toBeDisabled();
    await expect(dialog.getByRole("tab", { name: "Appearance" })).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    await expect(page.getByRole("dialog", { name: "Discard unsaved changes?" })).toHaveCount(0);
  } finally { release(); }
  await expect(dialog.getByRole("button", { name: "Close settings" })).toBeEnabled();
  await expect(dialog.getByLabel("Display name")).toHaveValue("Keep this draft");
  await page.mouse.click(10, 10);
  await expect(page.getByRole("dialog", { name: "Discard unsaved changes?" })).toBeVisible();
});

test("pending unwatch keeps settings open through failure, retry and list refresh", async ({ page }) => {
  await setup(page);
  const watched = { resource_id: "doc-a", uri: "akb://demo/coll/guides/doc/start.md", title: "Getting started", vault: "demo" };
  let releaseFailure!: () => void;
  let releaseRefresh!: () => void;
  const failureResponse = new Promise<void>(resolve => { releaseFailure = resolve; });
  const refreshResponse = new Promise<void>(resolve => { releaseRefresh = resolve; });
  let attempts = 0;
  let removed = false;
  await page.route("**/api/v1/notification-subscriptions**", async route => {
    if (route.request().method() === "DELETE") {
      if (new URL(route.request().url()).searchParams.get("uri") !== watched.uri) {
        return route.fulfill({ status: 400, json: { detail: "Unknown document" } });
      }
      if (++attempts === 1) {
        await failureResponse;
        return route.fulfill({ status: 500, json: { detail: "Unwatch failed" } });
      }
      removed = true;
      return route.fulfill({ json: { subscribed: false, resource_id: watched.resource_id } });
    }
    if (removed) await refreshResponse;
    return route.fulfill({ json: { items: removed ? [] : [watched] } });
  });
  await page.goto("/settings?tab=notifications");
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  const close = dialog.getByRole("button", { name: "Close settings" });
  const appearance = dialog.getByRole("tab", { name: "Appearance" });
  const unwatch = dialog.getByRole("button", { name: "Unwatch Getting started" });
  try {
    await unwatch.click();
    await expect(close).toBeDisabled();
    await expect(appearance).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    await page.mouse.click(10, 10);
    await expect(dialog).toBeVisible();

    releaseFailure();
    await expect(dialog.getByRole("alert")).toContainText("Could not stop watching");
    await expect(dialog.getByRole("link", { name: watched.title })).toBeVisible();
    await expect(close).toBeEnabled();
    await expect(appearance).toBeEnabled();
    await expect(unwatch).toBeEnabled();

    const refreshStarted = page.waitForRequest(request => request.method() === "GET"
      && new URL(request.url()).pathname.endsWith("/notification-subscriptions"));
    await unwatch.click();
    await refreshStarted;
    await expect(close).toBeDisabled();
    await expect(appearance).toBeDisabled();
    await expect(unwatch).toBeDisabled();
    await expect(dialog.getByRole("alert")).toHaveCount(0);
    releaseRefresh();
    await expect(dialog.getByText("You aren’t watching any documents yet.")).toBeVisible();
    await expect(close).toBeEnabled();
    await appearance.click();
    await expect(dialog.getByRole("radio", { name: "Light", exact: true })).toBeVisible();
    await close.click();
    await expect(dialog).toHaveCount(0);
  } finally {
    releaseFailure();
    releaseRefresh();
  }
});

test("mobile section navigation and nested dismissal stay inside settings", async ({ page }) => {
  await setup(page);
  await page.setViewportSize({ width: 375, height: 600 });
  await page.goto("/settings");
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await dialog.getByLabel("Display name").fill("Mobile draft");
  await dialog.getByRole("button", { name: "Settings section" }).click();
  await page.getByRole("menuitemradio", { name: "Appearance", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "Discard unsaved changes?" });
  await expect(confirmation).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(confirmation).toHaveCount(0);
  await expect(dialog.getByLabel("Display name")).toHaveValue("Mobile draft");
  await dialog.getByRole("button", { name: "Settings section" }).click();
  await page.getByRole("menuitemradio", { name: "Appearance", exact: true }).click();
  await confirmation.getByRole("button", { name: "Discard changes" }).click();
  await expect(dialog.getByRole("radio", { name: "Light", exact: true })).toBeChecked();
  await expect(dialog.getByRole("button", { name: "Settings section" })).toHaveText("Appearance");
});

test("leaving a new one-time token requires acknowledgement", async ({ page }) => {
  await setup(page);
  await page.route("**/api/v1/auth/tokens/capabilities", route => route.fulfill({ status: 404, json: { detail: "Not found" } }));
  await page.route("**/api/v1/auth/tokens", route => route.request().method() === "POST"
    ? route.fulfill({ json: { token_id: "00000000-0000-4000-8000-000000000001", token: "akb_fixture_one_time_secret", prefix: "akb_fixture" } })
    : route.fulfill({ json: { tokens: [] } }));
  await page.goto("/settings?tab=tokens");
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await dialog.getByLabel("Token name", { exact: true }).fill("Fixture agent");
  await dialog.getByRole("button", { name: "Create with server defaults" }).click();
  await expect(dialog.getByText("Token created — save it now")).toBeVisible();
  await dialog.getByRole("button", { name: "Close settings" }).click();
  const confirmation = page.getByRole("dialog", { name: "Leave agent connections?" });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "Stay here" }).click();
  await expect(dialog.getByText("Token created — save it now")).toBeVisible();
  await dialog.getByRole("tab", { name: "Profile", exact: true }).click();
  await confirmation.getByRole("button", { name: "Leave section" }).click();
  await expect(dialog.getByLabel("Display name")).toBeVisible();
  await expect(dialog).not.toContainText("akb_fixture_one_time_secret");
});

test("saving a profile refreshes the global identity without leaving the workspace", async ({ page }) => {
  await setup(page);
  let displayName = "Settings reviewer";
  await page.route("**/api/v1/auth/me", route => {
    if (route.request().method() === "PATCH") displayName = route.request().postDataJSON().display_name;
    return route.fulfill({ json: { updated: true, user_id: "settings-reader", username: "reader", display_name: displayName, email: "reader@example.invalid", is_admin: false, auth_method: "jwt", key_class: null } });
  });
  await page.goto("/search?q=retained");
  await page.getByTestId("app-sidebar").getByRole("button", { name: "Settings", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
  await dialog.getByLabel("Display name").fill("Updated reviewer");
  await dialog.getByRole("button", { name: "Save profile" }).click();
  await expect(dialog.getByRole("status").filter({ hasText: /^Saved$/ })).toBeVisible();
  await dialog.getByRole("button", { name: "Close settings" }).click();
  await expect(page.getByRole("button", { name: "Account menu — Updated reviewer" })).toBeVisible();
  await expect(page).toHaveURL(/\/search\?q=retained$/);
});
