import { expect, test, type Page } from "@playwright/test";

function health(pending = 0, abandoned = 0) {
  return { search_update_status: { version: 1, observed_at: new Date().toISOString(), stages: {
    search_index: { mode: "enabled", unit: "chunks", scope: "stored_chunks", pending, retrying: 0, abandoned },
    file_projection: { mode: "enabled", unit: "file_updates", scope: "latest_file_intents", pending: 0, retrying: 0, exhausted: 0, abandoned: 0 },
    content_preparation: { mode: "enabled", unit: "revision_updates", scope: "current_heads", pending: 0, retrying: 0, exhausted: 0, abandoned: 0 },
    metadata: { mode: "not_applicable", unit: "documents", scope: "external_git_documents" },
  } } };
}

async function fixtures(page: Page, dark: boolean) {
  const state = { pending: 0, abandoned: 0 };
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "search-status-isolated-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, { dark });
  await page.route("**/health/vault/**", route => route.fulfill({ json: health(route.request().url().includes("Research") ? state.pending * 2 : state.pending, state.abandoned) }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "status-fixture", username: "reviewer", display_name: "임근우", email: "review@example.invalid", is_admin: false, auth_method: "local" } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "one", name: "Team knowledge", role: "reader", description: "Project notes and working documents." }, { id: "two", name: "Research", role: "owner" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: { document_count: 21, table_count: 2, file_count: 3, role: "reader" } });
    if (path.endsWith("/notifications/unread-count")) return route.fulfill({ json: { supported: true, unread_count: 0, snapshot: "fixture", retention_days: 90 } });
    if (path.endsWith("/vaults/templates")) return route.fulfill({ json: [] });
    return route.fulfill({ json: { tokens: [{ name: "Configured" }], items: [], changes: [], templates: [], vaults: [] } });
  });
  return state;
}

for (const width of [375, 768, 1024, 1440, 2560]) for (const dark of [false, true]) {
  test(`Compact indexing ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Isolated authenticated HTTP fixtures.");
    await page.setViewportSize({ width, height: width === 768 ? 375 : 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const state = await fixtures(page, dark);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Your vaults", exact: true })).toBeVisible();
    const profile = page.getByRole("button", { name: "Account menu — 임근우" });
    const before = await profile.boundingBox();
    const badge = page.getByTestId("header-indexing-status");
    await expect(badge).toBeEmpty();
    await expect(page.getByRole("button", { name: /Search status/i })).toHaveCount(0);
    state.pending = 3;
    state.abandoned = 2;
    await page.reload();
    await expect(page.getByRole("heading", { name: "Your vaults", exact: true })).toBeVisible();
    if (width >= 1024) {
      await expect(badge.getByText("9 indexing", { exact: true })).toBeVisible();
      await expect(badge).toContainText("9 chunks waiting for search indexing across accessible vaults.");
      const box = (await badge.boundingBox())!;
      expect(box.width).toBeLessThan(145);
      expect(box.height).toBeLessThan(30);
      expect(await badge.locator("svg").evaluate(node => getComputedStyle(node).animationName)).toBe("none");
    } else {
      await expect(badge).toBeHidden();
      await profile.click();
      await expect(page.getByRole("menuitem", { name: /Search status/i })).toHaveCount(0);
      await page.keyboard.press("Escape");
    }
    await expect(page.getByTestId("home-search-notice")).toHaveCount(0);
    await expect(page.getByRole("dialog", { name: "Search updates" })).toHaveCount(0);
    const after = await profile.boundingBox();
    expect(after?.x).toBe(before?.x);
    expect(after?.width).toBe(before?.width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("compact-indexing.png"), fullPage: true });
    state.pending = 0;
    await page.reload();
    await expect(page.getByRole("heading", { name: "Your vaults", exact: true })).toBeVisible();
    await expect(badge).toBeEmpty();
    await expect(page.getByText(/Search status|Needs attention|caught up/i)).toHaveCount(0);
  });
}

test("Overview shows only its own pending count", async ({ page }, testInfo) => {
  test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Isolated authenticated HTTP fixtures.");
  await page.setViewportSize({ width: 1440, height: 900 });
  const state = await fixtures(page, false);
  state.pending = 3;
  await page.goto("/vault/Research");
  await expect(page.getByRole("heading", { name: "Research", exact: true })).toBeVisible();
  await expect(page.getByTestId("header-indexing-status").getByText("9 indexing", { exact: true })).toBeVisible();
  await expect(page.locator("#main").getByText("6 indexing", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Search status/i })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("vault-indexing.png"), fullPage: true });
});
