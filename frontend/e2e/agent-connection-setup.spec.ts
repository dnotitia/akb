import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");
const userId = "00000000-0000-0000-0000-000000000001";
const tokenId = "00000000-0000-0000-0000-000000000002";

async function setup(page: Page, { oauth = false, dark = false } = {}) {
  let creations = 0;
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "connection-layout-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, { dark });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: oauth } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: userId, username: "reviewer", display_name: "Connection reviewer", email: "reviewer@example.invalid", is_admin: false, auth_method: "local" } });
    if (path.endsWith("/auth/tokens/capabilities")) return route.fulfill({ json: { contract_version: 1, user_id: userId, name_max_length: 255, permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"], vault_scope_semantics: "write_restriction_sql_read_write" } });
    if (path.endsWith("/auth/tokens/issuance")) {
      creations++;
      const body = route.request().postDataJSON();
      return route.fulfill({ json: { contract_version: 1, user_id: userId, token_id: tokenId, name: body.name, token: "akb_fixture_secret_only", prefix: "akb_fixture", key_class: "pat", scopes: body.scopes, vault_scope: body.vault_scope, expires_at: null, issued_at: "2026-09-30T00:00:00Z" } });
    }
    return route.fulfill({ json: { vaults: [], tokens: [], items: [], changes: [], templates: [], unread_count: 0, supported: true } });
  });
  return () => creations;
}

for (const width of [1440, 375]) for (const oauth of [false, true]) {
  test(`connection setup fits ${width}px ${oauth ? "browser sign-in" : "token"}`, async ({ page }, testInfo) => {
    const creations = await setup(page, { oauth, dark: width === 375 });
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/");
    await page.getByRole("button", { name: "Connect an agent", exact: true }).click();
    const modal = page.getByRole("dialog", { name: "Connect an agent", exact: true });
    await expect(modal.getByRole("group", { name: "AI tool" })).toBeVisible();
    await expect(modal.getByRole("heading", { name: "1. Prepare access" })).toBeVisible();
    await expect(modal.getByRole("heading", { name: "3. Try it in your agent" })).toHaveCount(1);
    expect(creations()).toBe(0);
    expect(await modal.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await expect(modal.getByRole("button", { name: "Close", exact: true })).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath("connect-initial.png") });
    await modal.getByRole("radio", { name: "Codex", exact: true }).locator("..").click();
    await expect(modal.getByRole("radio", { name: "Browser sign-in" })).toHaveCount(0);
    await modal.getByRole("radio", { name: "Use a saved token" }).locator("..").click();
    await modal.getByLabel("Full saved token").fill("akb_fixture_saved_secret");
    await expect(modal.getByText(/codex mcp add/)).toContainText("akb_fixture_saved_secret");
    await modal.getByRole("heading", { name: "3. Try it in your agent" }).scrollIntoViewIfNeeded();
    await expect(modal.getByText(/This browser cannot verify/)).toBeVisible();
    await expect(modal.getByRole("button", { name: "Close", exact: true })).toBeInViewport();
    expect(await modal.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("connect-saved-token.png") });
    // Settings uses the same choice/step vocabulary, not another wizard.
    await page.goto("/settings?tab=tokens");
    const settings = page.getByRole("dialog", { name: "Settings", exact: true });
    await expect(settings.getByRole("group", { name: "AI tool" })).toBeVisible();
    await expect(settings.getByRole("heading", { name: "1. Prepare access" })).toBeVisible();
    await expect(settings.getByRole("heading", { name: "3. Try it in your agent" })).toHaveCount(1);
    await expect(settings.getByRole("heading", { name: "Saved tokens" })).toHaveCount(1);
    expect(await settings.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("settings-setup.png") });
    expect(creations()).toBe(0);
  });
}

test("setup preserves a draft across choices and protects the one-time token", async ({ page }) => {
  const creations = await setup(page, { oauth: true });
  await page.goto("/");
  await page.getByRole("button", { name: "Connect an agent", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Connect an agent", exact: true });
  await modal.getByRole("radio", { name: "Claude Code", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(modal.getByRole("radio", { name: "Cursor", exact: true })).toBeChecked();
  await expect(modal.getByRole("heading", { name: "2. Configure Cursor" })).toBeVisible();
  await page.keyboard.press("ArrowLeft");
  await expect(modal.getByRole("radio", { name: "Claude Code", exact: true })).toBeChecked();
  await modal.getByRole("radio", { name: "Create a new token" }).locator("..").click();
  await modal.getByLabel("Token name").fill("Work laptop");
  await modal.getByRole("radio", { name: "Browser sign-in" }).locator("..").click();
  await modal.getByRole("radio", { name: "Create a new token" }).locator("..").click();
  await expect(modal.getByLabel("Token name")).toHaveValue("Work laptop");
  await modal.getByRole("button", { name: "Create token", exact: true }).click();
  await expect(modal.getByText("Token created — save it now")).toBeVisible();
  expect(creations()).toBe(1);
  await modal.getByRole("button", { name: "Hide token and configuration" }).click();
  await expect(modal).not.toContainText("akb_fixture_secret_only");
  await modal.getByRole("button", { name: "Show token and configuration" }).click();
  await expect(modal.getByText(/claude mcp add/)).toContainText("akb_fixture_secret_only");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Have you saved your token?" })).toBeVisible();
  await page.getByRole("button", { name: "Keep setup open" }).click();
  await expect(modal.getByText("Token created — save it now")).toBeVisible();
  expect(creations()).toBe(1);
});
