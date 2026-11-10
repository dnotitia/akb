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
  test(`guided connection fits ${width}px with ${oauth ? "sign-in" : "a key"}`, async ({ page }, testInfo) => {
    const creations = await setup(page, { oauth, dark: width === 375 });
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/");
    await page.getByRole("button", { name: "Connect an AI tool", exact: true }).click();
    const modal = page.getByRole("dialog", { name: "Connect an AI tool", exact: true });
    await expect(modal.getByRole("heading", { level: 3 })).toHaveCount(1);
    await expect(modal.getByLabel("Connection key name")).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath("choose-tool.png") });
    await modal.getByRole("button", { name: "Continue with Claude Code" }).click();
    await expect(modal.getByRole("heading", { name: "2. Add AKB to Claude Code" })).toBeFocused();
    if (oauth) {
      await expect(modal.getByRole("radio", { name: "Sign in with AKB" })).toBeChecked();
      await expect(modal.getByText(/--transport http/)).toBeVisible();
      await expect(modal.getByText(/Open Claude Code, enter \/mcp/)).toBeVisible();
    } else {
      await modal.getByRole("radio", { name: "Use an existing key" }).locator("..").click();
      await modal.getByLabel("Full connection key").fill("akb_complete_fixture_key");
      await expect(modal.getByText(/npx akb-mcp/)).toContainText("akb_complete_fixture_key");
    }
    expect(creations()).toBe(0);
    await page.screenshot({ path: testInfo.outputPath("add-akb.png") });
    await modal.getByRole("button", { name: "Check in Claude Code", exact: true }).click();
    await expect(modal.getByRole("heading", { name: "3. Check the connection" })).toBeFocused();
    await expect(modal.getByText(/This browser cannot verify/)).toBeVisible();
    await modal.getByRole("checkbox", { name: "I received an AKB response in my AI tool" }).check();
    await expect(modal.getByText(/Confirmed by you/)).toBeVisible();
    await expect(modal.getByRole("button", { name: "Close", exact: true })).toBeInViewport();
    expect(await modal.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("check-connection.png") });
    await modal.getByRole("button", { name: "Back to setup" }).click();
    if (!oauth) await expect(modal.getByLabel("Full connection key")).toHaveValue("akb_complete_fixture_key");
    await modal.getByRole("button", { name: "Close", exact: true }).click();
    if (!oauth) await page.getByRole("button", { name: "Leave setup", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Connect an AI tool", exact: true })).toBeFocused();
  });
}

test("a fresh key stays available across client changes and close requires acknowledgement", async ({ page }) => {
  const creations = await setup(page, { oauth: true });
  await page.goto("/");
  await page.getByRole("button", { name: "Connect an AI tool", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Connect an AI tool" });
  await modal.getByRole("radio", { name: "Codex", exact: true }).locator("..").click();
  await modal.getByRole("button", { name: "Continue with Codex" }).click();
  await modal.getByLabel("Connection key name").fill("Work laptop");
  await modal.getByRole("button", { name: "Create connection key", exact: true }).click();
  await expect(modal.getByText("Connection key created", { exact: true })).toBeVisible();
  await expect(modal.getByText(/codex mcp add/)).toContainText("akb_fixture_secret_only");
  await modal.getByRole("button", { name: "Choose a tool", exact: true }).click();
  await modal.getByRole("radio", { name: "Cursor", exact: true }).locator("..").click();
  await modal.getByRole("button", { name: "Continue with Cursor" }).click();
  await expect(modal.getByRole("radio", { name: "Use a connection key" })).toBeChecked();
  await expect(modal.getByLabel("mcp.json", { exact: true })).toContainText("akb_fixture_secret_only");
  await modal.getByRole("button", { name: "Hide key and configuration" }).click();
  await expect(modal.getByLabel("mcp.json", { exact: true })).toHaveCount(0);
  await modal.getByRole("button", { name: "Show key and configuration" }).click();
  await expect(modal.getByLabel("mcp.json", { exact: true })).toContainText("akb_fixture_secret_only");
  expect(creations()).toBe(1);
  await modal.getByRole("button", { name: "Close", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "Have you saved your connection key?" });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "Keep setup open" }).click();
  await expect(modal.getByLabel("mcp.json", { exact: true })).toContainText("akb_fixture_secret_only");
});

for (const width of [1440, 375]) {
  test(`Help is reachable and opens one setup surface at ${width}px`, async ({ page }, testInfo) => {
    await setup(page, { oauth: true, dark: width === 375 });
    await page.setViewportSize({ width, height: 667 });
    await page.goto("/");
    const account = page.getByRole("button", { name: /^Account menu/ });
    const opener = width === 375 ? account : page.getByRole("button", { name: "Help", exact: true });
    if (width === 375) { await account.click(); await page.getByRole("menuitem", { name: "Help", exact: true }).click(); }
    else await opener.click();
    const help = page.getByRole("dialog", { name: "Help", exact: true });
    await expect(help).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("help-home.png") });
    await help.getByRole("button", { name: /^Find a document/ }).click();
    await expect(page.getByRole("heading", { name: "Find a document", exact: true })).toBeFocused();
    await page.getByRole("button", { name: "All help topics" }).click();
    await expect(page.getByRole("button", { name: /^Find a document/ })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(opener).toBeFocused();
    if (width === 375) { await account.click(); await page.getByRole("menuitem", { name: "Help", exact: true }).click(); }
    else await opener.click();
    await page.getByRole("button", { name: /^Use AKB in an AI tool/ }).click();
    await page.getByRole("button", { name: "Set up an AI connection" }).click();
    await expect(page.getByRole("heading", { name: "AI connections", exact: true })).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await page.getByRole("button", { name: "Continue with Claude Code" }).click();
    await page.getByRole("radio", { name: "Use a connection key" }).locator("..").click();
    await page.getByLabel("Connection key name").fill("Unfinished setup");
    await page.getByRole("tab", { name: "Connection keys", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Connection keys", exact: true })).toBeVisible();
    await page.getByRole("tab", { name: "Connection setup", exact: true }).click();
    await expect(page.getByLabel("Connection key name")).toHaveValue("Unfinished setup");
    expect(await page.getByRole("dialog").evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("settings-setup.png") });
  });
}

test("short windows keep setup dismissal reachable", async ({ page }) => {
  await setup(page, { oauth: true }); await page.setViewportSize({ width: 800, height: 320 });
  await page.goto("/"); await page.getByRole("button", { name: "Connect an AI tool", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Connect an AI tool" });
  await modal.getByRole("button", { name: "Continue with Claude Code" }).click();
  await expect(modal.getByRole("button", { name: "Close", exact: true })).toBeInViewport();
  await modal.getByRole("button", { name: "Go to step 1: Choose a tool" }).click();
  await expect(modal.getByRole("heading", { name: "1. Choose your AI tool" })).toBeFocused();
  await page.keyboard.press("Escape"); await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("key-list recovery links open management directly", async ({ page }) => {
  const creations = await setup(page, { oauth: true });
  await page.goto("/settings?tab=tokens&connectionView=keys");
  await expect(page.getByRole("tab", { name: "Connection keys", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("No connection keys yet", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "1. Choose your AI tool" })).toHaveCount(0);
  expect(creations()).toBe(0);
});

test("short Help keeps Back and Close visible while its article scrolls", async ({ page }) => {
  await setup(page); await page.setViewportSize({ width: 800, height: 320 }); await page.goto("/");
  await page.getByRole("button", { name: /^Account menu/ }).click();
  await page.getByRole("menuitem", { name: "Help", exact: true }).click();
  await page.getByRole("button", { name: /^Solve a problem/ }).click();
  await page.getByText("I only have part of a connection key", { exact: true }).click();
  await page.getByText(/The key list shows an identifying prefix/).scrollIntoViewIfNeeded();
  await expect(page.getByRole("button", { name: "All help topics" })).toBeInViewport();
  await expect(page.getByRole("button", { name: "Close dialog" })).toBeInViewport();
  await page.getByRole("button", { name: "All help topics" }).click();
  await expect(page.getByRole("button", { name: /^Find a document/ })).toBeFocused();
});

test("keyboard tool selection and copying never claim a verified connection", async ({ page }) => {
  await page.addInitScript(() => {
    let text = "";
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      writeText: async (value: string) => { text = value; }, readText: async () => text,
    } });
  });
  await setup(page, { oauth: true }); await page.goto("/");
  await page.getByRole("button", { name: "Connect an AI tool", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Connect an AI tool" });
  await modal.getByRole("radio", { name: "Claude Code", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(modal.getByRole("radio", { name: "Cursor", exact: true })).toBeChecked();
  await modal.getByRole("button", { name: "Continue with Cursor" }).click();
  await modal.getByLabel("mcp.json", { exact: true }).locator("..").getByRole("button", { name: "Copy snippet", exact: true }).click();
  await expect(modal.getByText(/Setup copied. Apply it in Cursor/)).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain("mcpServers");
  await modal.getByRole("button", { name: "Check in Cursor", exact: true }).click();
  await expect(modal.getByRole("checkbox")).not.toBeChecked();
  await expect(modal.getByText(/This browser cannot verify/)).toBeVisible();
});
