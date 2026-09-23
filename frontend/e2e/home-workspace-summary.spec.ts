import { expect, test, type Page } from "@playwright/test";

// The real-mode CI runtime runs these browser-owned HTTP fixtures. MSW owns
// responses in mock mode and would bypass page.route's authenticated fixtures.
test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated authenticated HTTP fixtures.");

async function fixture(page: Page, dark = false) {
  const userId = "00000000-0000-4000-8000-000000000001";
  const tokenId = "00000000-0000-4000-8000-000000000002";
  const state = { legacy: false, vaults: 20, tokens: 0, summaries: 0, details: 0, authGate: null as Promise<void> | null };
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "home-summary-isolated-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, { dark });
  await page.route("**/health/vault/**", route => route.fulfill({ json: { vector_store: { backfill: { upsert: { pending: 0 } } } } }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return Promise.resolve(state.authGate).then(() => route.fulfill({ json: { user_id: userId, username: "reviewer", display_name: "임근우", email: "review@example.invalid", is_admin: false, auth_method: "local" } }));
    if (path.endsWith("/auth/tokens/capabilities")) return route.fulfill({ json: {
      contract_version: 1, user_id: userId, name_max_length: 255,
      permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"],
      vault_scope_semantics: "write_restriction_sql_read_write",
    } });
    if (path.endsWith("/auth/tokens/issuance") && route.request().method() === "POST") {
      const request = route.request().postDataJSON();
      expect(request).toEqual({ contract_version: 1, expected_user_id: userId,
        name: "Example work laptop", scopes: ["read", "write"], vault_scope: null });
      state.tokens++;
      return route.fulfill({ json: {
        contract_version: 1, user_id: userId, token_id: tokenId, name: "Example work laptop",
        token: "akb_isolated_secret_save_before_closing", prefix: "akb_isolated", key_class: "pat",
        scopes: ["read", "write"], vault_scope: null, expires_at: null, issued_at: new Date().toISOString(),
      } });
    }
    if (path.endsWith("/auth/tokens")) {
      expect(route.request().method()).toBe("GET");
      return route.fulfill({ json: { tokens: state.tokens ? [{ token_id: tokenId, name: "Existing" }] : [] } });
    }
    if (path.endsWith("/my/workspace-summary")) {
      state.summaries++;
      return state.legacy ? route.fulfill({ status: 404, json: { detail: "Not found" } })
        : route.fulfill({ json: { version: 1, scope: "accessible", observed_at: new Date().toISOString(), vault_count: state.vaults, document_count: state.vaults ? 1284 : 0, table_count: state.vaults ? 12 : 0, file_count: state.vaults ? 86 : 0 } });
    }
    if (path.endsWith("/my/vaults")) return route.fulfill({ json: { vaults: Array.from({ length: state.vaults }, (_, i) => ({ id: `vault-${i}`, name: ["Product knowledge", "Research", "Engineering", "Team handbook"][i] ?? `Workspace ${i}`, role: i % 2 ? "reader" : "owner", description: ["Decisions, project notes, and release plans.", "Evidence and recommendations for upcoming work.", "Architecture and operational guides.", "People, practices, and shared context."][i] })) } });
    if (path.endsWith("/info")) {
      state.details++;
      return route.fulfill({ json: { document_count: 21, table_count: 2, file_count: 3, role: "reader" } });
    }
    const summary = { supported: true, unread_count: 0, snapshot: "fixture", retention_days: 90 };
    if (path.endsWith("/notifications/unread-count")) return route.fulfill({ json: summary });
    if (path.endsWith("/notifications")) return route.fulfill({ json: { ...summary, category: "all", items: [] } });
    if (path.endsWith("/recent")) return route.fulfill({ json: { scope: url.searchParams.get("scope") ?? "all", changes: Array.from({ length: state.vaults ? 4 : 0 }, (_, i) => ({
      doc_id: `doc-${i}`, vault: "Product knowledge", path: `notes/update-${i}.md`, title: ["Release readiness checklist", "How we organize shared knowledge", "API integration notes", "Weekly team decisions"][i], updated_by_name: "Mina Park", changed_at: new Date().toISOString(), excerpt: "Project context and practical next steps, kept together for the team.",
    })) } });
    if (path.endsWith("/vaults/templates")) return route.fulfill({ json: [] });
    return route.fulfill({ json: { items: [], changes: [], vaults: [], results: [] } });
  });
  return state;
}

async function expectPaperHome(page: Page) {
  const surfaces = await page.evaluate(() => {
    const header = document.querySelector("header")!;
    const sidebar = document.querySelector("aside")!;
    let canvas: Element | null = document.querySelector("main");
    while (canvas && ["transparent", "rgba(0, 0, 0, 0)"].includes(getComputedStyle(canvas).backgroundColor)) {
      canvas = canvas.parentElement;
    }
    return {
      header: getComputedStyle(header).backgroundColor,
      sidebar: getComputedStyle(sidebar).backgroundColor,
      canvas: canvas && getComputedStyle(canvas).backgroundColor,
      divider: getComputedStyle(header).borderBottomWidth,
      height: header.getBoundingClientRect().height,
    };
  });
  expect(surfaces.header, "Home header should connect to the paper surface").toBe(surfaces.sidebar);
  expect(surfaces.canvas, "Home canvas should retain the same paper surface").toBe(surfaces.sidebar);
  expect(surfaces.divider).toBe("1px");
  expect(surfaces.height).toBe(56);
}

for (const width of [375, 2560]) for (const dark of [false, true]) {
  test(`Home paper surface stays stable through session loading at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const state = await fixture(page, dark);
    let release!: () => void;
    state.authGate = new Promise<void>(resolve => { release = resolve; });
    try {
      await page.goto("/");
      await expect(page.getByRole("status", { name: "Verifying session", exact: true })).toBeVisible();
      await expectPaperHome(page);
      await page.screenshot({ path: testInfo.outputPath("home-loading.png"), fullPage: true });
    } finally {
      state.authGate = null;
      release();
    }
    await expect(page.getByRole("region", { name: "Workspace summary" })).toContainText("1,284");
    await expectPaperHome(page);
    if (width >= 1024) {
      await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
      await expect(page.getByTestId("app-sidebar")).toHaveAttribute("data-compact", "true");
      await expectPaperHome(page);
      await page.getByRole("button", { name: "Expand sidebar", exact: true }).click();
      await expectPaperHome(page);
    }
  });
}

for (const width of [375, 768, 1440, 2560]) for (const dark of [false, true]) {
  test(`Home summary and connection ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width === 768 ? 375 : 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const state = await fixture(page, dark);
    await page.goto("/");
    const totals = page.getByRole("region", { name: "Workspace summary" });
    await expect(totals).toContainText("1,284");
    await expect(totals).toContainText("20");
    await expectPaperHome(page);
    await expect(totals.getByText("Available to you", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "View all vaults", exact: true })).toBeVisible();
    const invitation = page.getByTestId("home-connection-invitation");
    await expect(invitation).toBeVisible();
    await expect(invitation).toBeInViewport({ ratio: 1 });
    if (width >= 1024) {
      await expect(invitation.getByRole("heading", { name: "Use AKB in your AI tools" })).toBeVisible();
      expect((await invitation.boundingBox())!.width).toBeLessThanOrEqual(305);
    } else {
      await expect(invitation.getByRole("heading")).toHaveCount(0);
      expect((await invitation.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    }
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(state.summaries).toBe(1);
    expect(state.details).toBeLessThanOrEqual(4);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("home-workspace.png"), fullPage: true });
  });
}

for (const reducedMotion of ["no-preference", "reduce"] as const) {
  test(`Connection launcher stays viewport-fixed on a long Home (${reducedMotion})`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 700 });
    await page.emulateMedia({ reducedMotion });
    const state = await fixture(page);
    state.tokens = 1;
    await page.goto("/");
    const invitation = page.getByTestId("home-connection-invitation");
    await expect(invitation).toHaveAttribute("data-expanded", "false");
    await expect(invitation).toBeInViewport({ ratio: 1 });
    const initial = (await invitation.boundingBox())!;
    expect(700 - initial.y - initial.height).toBeCloseTo(24, 0);
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
    await expect(invitation).toBeInViewport({ ratio: 1 });
    const scrolled = (await invitation.boundingBox())!;
    expect(scrolled.y).toBeCloseTo(initial.y, 0);
    await page.getByRole("button", { name: "Connect an agent", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Connect an agent", exact: true })).toBeVisible();
  });
}

test("Connection invitation minimizes, survives reload, and preserves a new secret", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Minimize connection guide" }).click();
  const launcher = page.getByRole("button", { name: "Connect an agent", exact: true });
  await expect(launcher).toBeFocused();
  await page.reload();
  await expect(launcher).toBeVisible();
  await expect(page.getByRole("button", { name: "Minimize connection guide" })).toHaveCount(0);
  await launcher.click();
  const dialog = page.getByRole("dialog", { name: "Connect an agent", exact: true });
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId("home-connection-invitation")).toBeHidden();
  await dialog.getByLabel("Token name", { exact: true }).fill("Example work laptop");
  await dialog.getByRole("button", { name: "Create token", exact: true }).click();
  await expect(dialog.getByText("Token created — save it now")).toBeVisible();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Have you saved your token?" })).toBeVisible();
  await page.getByRole("button", { name: "Keep setup open" }).click();
  await expect(dialog.getByText("Token created — save it now")).toBeVisible();
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("button", { name: "I've saved it — close" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(launcher).toBeVisible();
  await expect(launcher).toBeFocused();
});

test("Transparent notifications and global search suspend floating chrome", async ({ page }) => {
  await fixture(page);
  await page.goto("/");
  const invitation = page.getByTestId("home-connection-invitation");
  await expect(invitation).toBeVisible();
  await page.getByRole("button", { name: /^Notifications,/ }).click();
  await expect(page.getByTestId("notification-panel")).toBeVisible();
  await expect(invitation).toBeHidden();
  await page.keyboard.press("Escape");
  await expect(invitation).toBeVisible();
  await page.getByRole("button", { name: /Search all vaults/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(invitation).toBeHidden();
  await page.keyboard.press("Escape");
  await expect(invitation).toBeVisible();
});

test("Legacy totals stay unknown; an empty workspace keeps creation primary", async ({ page }) => {
  const state = await fixture(page);
  state.legacy = true;
  await page.goto("/");
  const totals = page.getByRole("region", { name: "Workspace summary" });
  await expect(totals).toContainText("Totals unavailable");
  await expect(totals).toContainText("20");
  await expect(totals.locator("dd", { hasText: "—" })).toHaveCount(3);
  expect(state.details).toBeLessThanOrEqual(4);
  state.vaults = 0;
  await page.reload();
  await expect(totals).toContainText("0");
  await expect(totals.locator("dd")).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Create a vault", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Minimize connection guide" })).toHaveCount(0);
});

test("Local account foreground proof hides and refreshes totals after access changes", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/");
  const totals = page.getByRole("region", { name: "Workspace summary" });
  await expect(totals).toContainText("1,284");
  const before = state.summaries;
  let release!: () => void;
  state.authGate = new Promise<void>(resolve => { release = resolve; });
  state.vaults = 2;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(totals.getByRole("status", { name: "Loading workspace totals" })).toBeVisible();
  await expect(totals).not.toContainText("1,284");
  expect(state.summaries).toBe(before);
  state.authGate = null;
  release();
  await expect(totals).toContainText("1,284");
  await expect(totals.locator("dd").first()).toHaveText("2");
  expect(state.summaries).toBe(before + 1);
});
