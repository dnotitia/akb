import { expect, test } from "@playwright/test";

for (const width of [1920, 1440, 375]) for (const dark of [false, true]) {
  test(`Home watching ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Own HTTP fixtures, no MSW.");
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(() => localStorage.setItem("akb_token", "home-watch-fixture"));
    const writes: string[] = [];
    let watched = true;
    const row = (id: string, title: string) => ({ doc_id: id, title, vault: "fixture", path: `guides/${id}.md`, changed_at: "2026-09-09T00:00:00Z" });
    await page.route("**/health/**", route => route.fulfill({ json: {} }));
    await page.route("**/api/v1/**", async route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      const method = route.request().method();
      if (method !== "GET") writes.push(`${method} ${path}`);
      if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
      if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "home-watch-user", username: "fixture", display_name: "테스터", email: "fixture@example.invalid", is_admin: false, auth_method: "local" } });
      if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ name: "fixture", role: "writer" }] } });
      if (path.endsWith("/vaults/fixture/info")) return route.fulfill({ json: { name: "fixture", role: "writer", is_archived: false, is_external_git: false } });
      if (path.endsWith("/recent")) {
        const scope = url.searchParams.get("scope") || "all";
        return route.fulfill({ json: { scope, next_cursor: null, changes: scope === "watching" ? (watched ? [row("watched", "Watched operating guide")] : []) : [row("other", "Another recent document")] } });
      }
      if (path.endsWith("/notifications/unread-count")) return route.fulfill({ json: { supported: true, unread_count: 1, snapshot: "snapshot", retention_days: 90 } });
      if (path.endsWith("/notification-subscriptions")) {
        if (method === "DELETE") watched = false;
        return route.fulfill({ json: { subscribed: watched, resource_id: "watched", items: [] } });
      }
      if (path.includes("/documents/fixture/")) return route.fulfill({ json: { path: "guides/watched.md", title: "Watched operating guide", content: "# Operating guide\n\nThis document is watched.", status: "active", current_commit: "aaaaaaaaaaaaa", tags: [] } });
      return route.fulfill({ json: { items: [], tokens: [], history: [], relations: [], vaults: [] } });
    });
    await page.goto("/");
    await page.evaluate(value => document.documentElement.classList.toggle("dark", value), dark);
    const recent = page.locator("#recent");
    await expect(recent.getByText("Another recent document")).toBeVisible();
    await recent.getByRole("tab", { name: "All", exact: true }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(recent.getByRole("tab", { name: "Watching", exact: true })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(recent.getByRole("tab", { name: "Watching", exact: true })).toHaveAttribute("aria-selected", "true");
    const link = recent.getByRole("link", { name: /Watched operating guide/ });
    await expect(link).toBeVisible();
    await expect(recent.getByText("Another recent document")).toHaveCount(0);
    await expect(recent.getByRole("link", { name: "Manage watches" })).toHaveAttribute("href", "/settings?tab=notifications");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await recent.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("home-watching.png") });
    await link.click();
    const preview = page.getByTestId("document-preview-dialog");
    await expect(preview).toBeVisible();
    expect(writes).toEqual([]);
    await preview.getByRole("button", { name: "Unwatch", exact: true }).click();
    await expect(preview.getByRole("button", { name: "Watch", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(preview).toHaveCount(0);
    await expect(page).toHaveURL(/\/$/);
    await expect(recent.getByText("Watched operating guide")).toHaveCount(0);
    await expect(recent.getByRole("tab", { name: "Watching", exact: true })).toBeFocused();
    expect(writes).toEqual(["DELETE /api/v1/notification-subscriptions"]);
    await page.reload();
    await expect(recent.getByRole("tab", { name: "Watching", exact: true })).toHaveAttribute("aria-selected", "true");
  });
}
