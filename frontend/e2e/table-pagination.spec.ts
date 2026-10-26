import { expect, test } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");

for (const scenario of [
  { width: 375, height: 812, dark: false },
  { width: 1440, height: 900, dark: false },
  { width: 1440, height: 900, dark: true },
  { width: 812, height: 375, dark: false },
]) test(`table browsing and explicit count at ${scenario.width}x${scenario.height} ${scenario.dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize(scenario);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "table-pagination-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    localStorage.setItem("akb.vaultRailCollapsed", "1");
    localStorage.setItem("akb.treeVisible", "0");
  }, scenario);
  const reads: Array<{ limit: number; offset: number; count: boolean }> = [];
  let countAttempts = 0;
  let liveRows = 123;
  let schemaOnly = false;
  await page.route("**/api/v1/**", route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const respond = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path.endsWith("/auth/config")) return respond({ schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } });
    if (path.endsWith("/auth/me")) return respond({ user_id: "table-pagination-fixture", username: "fixture", email: "reader@example.invalid", is_admin: false });
    if (path.endsWith("/vaults")) return respond({ vaults: [{ name: "fixture", role: "reader" }] });
    if (path.endsWith("/info")) return respond({ name: "fixture", role: "reader", is_archived: false, is_external_git: false });
    if (path === "/api/v1/tables/fixture") {
      schemaOnly = url.searchParams.get("include_row_counts") === "false";
      return respond({ items: [{ name: "records", columns: [{ name: "title", type: "text" }] }] });
    }
    if (path.endsWith("/tables/fixture/records/rows")) {
      const limit = Number(url.searchParams.get("limit"));
      const offset = Number(url.searchParams.get("offset"));
      const count = request.headers().prefer === "count=exact";
      reads.push({ limit, offset, count });
      if (count && ++countAttempts === 1) return respond({ error: "Count temporarily unavailable" }, 503);
      const items = Array.from({ length: Math.min(limit, Math.max(0, liveRows - offset)) }, (_, i) => ({
        id: `00000000-0000-0000-0000-${String(liveRows - offset - i).padStart(12, "0")}`,
        title: `Record ${liveRows - offset - i}`,
      }));
      return respond({ kind: "table_query", columns: ["id", "title"], items, total: count ? 123 : items.length });
    }
    return respond({ items: [], vaults: [], unread_count: 0, subscribed: false });
  });
  await page.goto("/vault/fixture/table/records");
  await expect(page.getByText("Record 123", { exact: true })).toBeVisible();
  await expect(page.getByText("Record 73", { exact: true })).toHaveCount(0);
  await expect(page.getByText("1–50 · total not counted")).toBeVisible();
  expect(schemaOnly).toBe(true);
  expect(reads.every(read => !read.count)).toBe(true);
  await page.getByRole("button", { name: "Next page", exact: true }).click();
  await expect(page.getByText("51–100 · total not counted")).toBeVisible();
  expect(reads.at(-1)).toEqual({ limit: 51, offset: 50, count: false });
  await page.getByRole("button", { name: "Count rows", exact: true }).click();
  await expect(page.getByText(/Could not count rows/)).toBeVisible();
  await expect(page.getByText("Record 73", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Count rows", exact: true }).click();
  await expect(page.getByText("51–100 · 123 rows counted")).toBeVisible();
  expect(reads.at(-1)).toEqual({ limit: 0, offset: 0, count: true });
  await page.getByRole("button", { name: "Next page", exact: true }).click();
  await expect(page.getByText("101–123 · 123 rows counted")).toBeVisible();
  await expect(page.getByRole("button", { name: "Next page", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Previous page", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("51–100 · 123 rows counted")).toBeVisible();
  // The recorded count is a snapshot, not a ceiling after another writer inserts.
  liveRows = 201;
  await page.getByRole("spinbutton", { name: /Page number/ }).fill("4");
  await page.getByRole("spinbutton", { name: /Page number/ }).press("Enter");
  await expect(page.getByText("151–200 · 123 rows counted")).toBeVisible();
  expect(reads.at(-1)).toEqual({ limit: 51, offset: 150, count: false });
  expect(reads.filter(read => read.count)).toHaveLength(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("table-pagination.png"), fullPage: true });
});
