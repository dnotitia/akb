import { expect, test } from "@playwright/test";

for (const width of [1440, 1024, 640, 375]) for (const dark of [false, true]) {
  test(`notification inbox ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Own HTTP fixtures, no MSW.");
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(() => localStorage.setItem("akb_token", "notifications-browser-fixture"));
    const writes: unknown[] = [];
    let read = false;
    let watched = false;
    const summary = () => ({ supported: true, unread_count: read ? 0 : 1, snapshot: "observed-snapshot", retention_days: 90 });
    await page.route("**/health/**", route => route.fulfill({ json: {} }));
    await page.route("**/api/v1/**", async route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
      if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "notification-fixture", username: "fixture", display_name: "테스터", email: "fixture@example.invalid", is_admin: false, auth_method: "local" } });
      if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ name: "fixture", role: "writer" }] } });
      if (path.endsWith("/vaults/fixture/info")) return route.fulfill({ json: { name: "fixture", role: "writer", is_archived: false, is_external_git: false } });
      if (path.endsWith("/notifications/unread-count")) return route.fulfill({ json: summary() });
      if (path.endsWith("/notifications/n1") || path.endsWith("/notifications/mark-read")) {
        writes.push(route.request().postDataJSON()); read = true; return route.fulfill({ json: summary() });
      }
      if (path.endsWith("/notifications")) {
        const first = { id: "n1", kind: "document.update", title: "Release notes updated", message: "The release notes you watch have changed.", created_at: "2026-09-08T00:00:00Z", updated_at: "2026-09-08T00:00:00Z", version: "v1", read, target: { uri: "akb://fixture/doc/notes.md", vault: "fixture" } };
        const examples = [
          { kind: "document.move", title: "운영 가이드 — 배포와 복구 절차" },
          { kind: "document.archive", title: "Earlier onboarding guide" },
          { kind: "document.restore", title: "Release checklist" },
          { kind: "access.granted", title: "fixture", target: { uri: "akb://fixture", vault: "fixture" } },
          { kind: "access.changed", title: "fixture", target: { uri: "akb://fixture", vault: "fixture" } },
          { kind: "document.delete", title: "A watched document was deleted", target: null },
          { kind: "document.update", title: "A deliberately long document title covering several important operational changes without hiding its meaning" },
        ].map((entry, index) => ({ ...first, ...entry, id: `other-${index}`, read: true, message: entry.title }));
        const category = url.searchParams.get("category") || "all";
        const items = [first, ...examples].filter(item =>
          (category === "all" || item.kind.startsWith(category === "documents" ? "document." : "access.")) &&
          (url.searchParams.get("state") !== "unread" || !item.read));
        return route.fulfill({ json: { ...summary(), category, next_cursor: null, items } });
      }
      if (path.endsWith("/notification-subscriptions")) {
        if (route.request().method() === "PUT") watched = true;
        if (route.request().method() === "DELETE") watched = false;
        return route.fulfill({ json: { subscribed: watched, resource_id: "fixture-doc", items: [] } });
      }
      if (path.includes("/documents/fixture/") && !path.endsWith("/history")) return route.fulfill({ json: { path: "notes.md", title: "Release notes", content: "# Release notes\n\nA watched document in your personal inbox.", status: "active", current_commit: "aaaaaaaaaaaaa", tags: [] } });
      return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [] } });
    });
    await page.goto("/notifications?state=all");
    await page.evaluate(value => document.documentElement.classList.toggle("dark", value), dark);
    const bell = page.getByRole("button", { name: "Notifications, 1 unread" });
    const account = page.getByRole("button", { name: "Account menu — 테스터" });
    await expect(account).toBeVisible();
    if (width >= 640) {
      const name = account.getByText("테스터", { exact: true });
      await expect(name).toBeVisible();
      expect(await name.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
      expect((await account.boundingBox())!.width).toBeGreaterThan(80);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await bell.click();
    const panel = page.getByTestId("notification-panel");
    await expect(panel).toBeVisible();
    await expect(panel.getByText("Release notes updated")).toBeVisible();
    await expect(panel.getByRole("button", { name: "Release notes updated", exact: true })).toHaveAccessibleDescription(/View document/);
    const notice = panel.getByRole("listitem").filter({ hasText: "A watched document was deleted" });
    await expect(notice).toHaveAttribute("data-actionable", "false");
    await expect(notice.getByText("Notice only", { exact: true })).toBeVisible();
    await expect(notice.getByRole("button", { name: "A watched document was deleted" })).toHaveCount(0);
    await expect(notice.getByRole("button", { name: "Mark unread" })).toBeEnabled();
    await notice.getByText("A watched document was deleted", { exact: true }).click();
    await expect(panel).toBeVisible();
    await expect(page).toHaveURL(/\/notifications\?state=all/);
    const firstRow = panel.getByRole("listitem").filter({ has: page.getByRole("button", { name: "Release notes updated", exact: true }) });
    expect((await firstRow.boundingBox())!.height).toBeLessThanOrEqual(56);
    expect(writes).toEqual([]);
    const bounds = await panel.boundingBox();
    expect(bounds!.width).toBeLessThanOrEqual(width);
    if (width > 600) expect(bounds!.width).toBeGreaterThanOrEqual(400);
    await page.screenshot({ path: testInfo.outputPath("notifications-panel.png") });
    await panel.getByRole("tab", { name: "Access" }).click();
    await expect(panel.getByText("Access granted", { exact: true })).toBeVisible();
    await expect(panel.getByRole("button", { name: "fixture", exact: true }).first()).toHaveAccessibleDescription(/Open vault/);
    await expect(panel.getByRole("button", { name: "Release notes updated" })).toHaveCount(0);
    await panel.getByRole("button", { name: "Notification actions" }).click();
    await expect(page.getByRole("menuitem", { name: "Mark all notifications read" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(panel.getByRole("button", { name: "Notification actions" })).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath("notifications-access.png") });
    await panel.getByRole("checkbox", { name: "Unread only" }).check();
    await expect(panel.getByText("You’re all caught up")).toBeVisible();
    // Arrow keys switch the category while retaining the independent read filter.
    await panel.getByRole("tab", { name: "Access" }).focus();
    await page.keyboard.press("ArrowLeft");
    await expect(panel.getByRole("tab", { name: "Documents" })).toHaveAttribute("aria-selected", "true");
    await expect(panel.getByRole("checkbox", { name: "Unread only" })).toBeChecked();
    await expect(panel.getByRole("button", { name: "Release notes updated" })).toBeVisible();
    expect(writes).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("notifications-documents.png") });
    await panel.getByRole("button", { name: "Release notes updated" }).click();
    const reader = page.getByTestId("document-preview-dialog");
    await expect(reader).toBeVisible();
    await expect(panel).toHaveCount(0);
    expect(writes).toEqual([{ read: true, version: "v1" }]);
    const watch = reader.getByRole("button", { name: "Watch", exact: true });
    await expect(watch).toBeEnabled();
    await watch.click();
    await expect(reader.getByRole("button", { name: "Unwatch", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page).toHaveURL(/\/notifications\?state=all/);
    await expect(page.getByRole("button", { name: "Notifications, 0 unread" })).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath("notifications-inbox.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByRole("button", { name: "Notifications, 0 unread" }).click();
    await panel.getByRole("button", { name: "Notification actions" }).click();
    await expect(page.getByRole("menuitem", { name: "Mark all notifications read" })).toBeDisabled();
    await page.getByRole("menuitem", { name: "Notification settings", exact: true }).click();
    await expect(page).toHaveURL(/\/settings\?tab=notifications/);
    await expect(panel).toHaveCount(0);
    await expect(page.getByRole("menu")).toHaveCount(0);
  });
}
