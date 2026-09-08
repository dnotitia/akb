// Real browser, isolated HTTP fixtures: never changes a user's Vault.
import { test, expect } from "@playwright/test";

for (const width of [1440, 390]) {
for (const dark of [false, true]) {
test(`archived search result restores inside the reader at ${width}px (${dark ? "dark" : "light"})`, async ({ page }, testInfo) => {
  test.skip(process.env.AKB_FE_E2E_MODE === "mock", "This contract owns its HTTP fixtures, not MSW.");
  let status = "archived";
  await page.setViewportSize({ width, height: 900 });
  const writes: unknown[] = [];
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.addInitScript(() => localStorage.setItem("akb_token", "archive-browser-fixture"));
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: {
      schema_version: 2, auth_mode: "local", local_auth: { enabled: true },
      keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false },
    } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: {
      user_id: "archive-fixture", username: "fixture", display_name: "Fixture writer", email: "fixture@example.invalid", is_admin: false, auth_method: "local",
    } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ name: "fixture", role: "writer" }] } });
    if (path.endsWith("/vaults/fixture/info")) return route.fulfill({ json: { name: "fixture", role: "writer", is_archived: false, is_external_git: false } });
    if (path.endsWith("/search")) {
      const scope = url.searchParams.get("archive_scope") || "unarchived";
      const results = scope === "archived" && status !== "archived" ? [] : [{
        title: "Archive recovery example", uri: "akb://fixture/doc/example.md", vault: "fixture", path: "example.md", source_type: "document", status, score: 1,
      }];
      return route.fulfill({ json: { query: "example", archive_scope: scope, total: results.length, returned: results.length, total_matches: results.length, results } });
    }
    if (path.includes("/documents/fixture/") && !path.endsWith("/history")) {
      if (route.request().method() === "PATCH") {
        const body = route.request().postDataJSON();
        writes.push(body);
        status = body.status;
      }
      return route.fulfill({ json: { path: "example.md", title: "Archive recovery example", content: "# Recovery\n\nOriginal document body.", status, current_commit: "aaaaaaaaaaaaa", tags: [] } });
    }
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [] } });
  });
  await page.goto("/search?q=example&archive_scope=archived&source=document");
  await page.evaluate(value => document.documentElement.classList.toggle("dark", value), dark);
  await page.getByRole("link", { name: /Archive recovery example/ }).click();
  const reader = page.getByRole("dialog").filter({ has: page.getByRole("heading", { name: "Archive recovery example" }) });
  await expect(reader).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("archived-reader.png") });
  await reader.getByRole("button", { name: "Restore document" }).click();
  const confirm = page.getByRole("dialog", { name: "Restore this document?" });
  await confirm.getByRole("button", { name: "Restore document" }).click();
  await expect(page.getByText("Restored to Vault root.")).toBeVisible();
  expect(writes).toEqual([{ status: "active", expected_commit: "aaaaaaaaaaaaa" }]);
  await page.keyboard.press("Escape");
  await expect(page).toHaveURL(/\/search\?q=example&archive_scope=archived/);
  await expect(page.getByRole("link", { name: /Archive recovery example/ })).toHaveCount(0);
});
}
}
