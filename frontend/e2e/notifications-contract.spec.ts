import { expect, test } from "@playwright/test";

for (const width of [1440, 1024, 640, 375, 320]) for (const dark of [false, true]) for (const markFails of [false, true]) {
  test(`notification inbox ${width}px ${dark ? "dark" : "light"}${markFails ? " read failure" : ""}`, async ({ page }, testInfo) => {
    test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Own HTTP fixtures, no MSW.");
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(() => localStorage.setItem("akb_token", "notifications-browser-fixture"));
    const writes: unknown[] = [];
    let read = false;
    let failNextMark = markFails;
    let watched = false;
    let releaseRowRead!: () => void;
    const rowReadResponse = new Promise<void>(resolve => { releaseRowRead = resolve; });
    let rowReadStarted = false;
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
      if (path.endsWith("/notifications/other-5")) {
        rowReadStarted = true;
        await rowReadResponse;
        return route.fulfill({ status: 503, json: { detail: "Read state unavailable" } });
      }
      if (path.endsWith("/notifications/n1") || path.endsWith("/notifications/mark-read")) {
        writes.push(route.request().postDataJSON());
        if (failNextMark) { failNextMark = false; return route.fulfill({ status: 503, json: { detail: "Read state unavailable" } }); }
        read = true; return route.fulfill({ json: summary() });
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
          { kind: "access.ownership", title: "fixture", target: { uri: "akb://fixture", vault: "fixture" } },
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
    if (width === 1440 && !dark && !markFails) {
      // Detailed category filters stay on the full page, not in the dropdown.
      await page.getByRole("tab", { name: "Vault access", exact: true }).click();
      await expect(page.getByRole("listitem")).toHaveCount(3);
      await expect(page.locator(".lucide-user-round-key")).toHaveCount(1);
      await expect(page.locator(".lucide-crown")).toHaveCount(0);
      // This URL-backed control commits through the router transition, not
      // synchronously inside the native checkbox click.
      const unreadOnly = page.getByRole("checkbox", { name: "Unread only", exact: true });
      await unreadOnly.click();
      await expect(unreadOnly).toBeChecked();
      await expect(page.getByText("You’re all caught up", { exact: true })).toBeVisible();
      await page.getByRole("tab", { name: "Documents", exact: true }).click();
      await expect(page.getByRole("link", { name: "Release notes updated", exact: true })).toBeVisible();
      await page.goto("/notifications?state=all");
    }
    await expect(page.getByRole("heading", { name: "Inbox", exact: true })).toHaveCount(0);
    const bell = page.getByRole("button", { name: "Notifications, 1 unread" });
    const account = page.getByRole("button", {
      name: "Account menu — 테스터 (@fixture), fixture@example.invalid",
      exact: true,
    });
    await expect(account).toBeVisible();
    if (width >= 640) {
      const name = account.getByText("테스터", { exact: true });
      await expect(name).toBeVisible();
      expect(await name.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
      expect((await account.boundingBox())!.width).toBeGreaterThan(80);
    }
    const bellGlyph = bell.locator("svg");
    await expect(bellGlyph).toHaveClass(/lucide-bell/);
    await expect(bellGlyph).toHaveCSS("width", "16px");
    await expect(bellGlyph).toHaveCSS("stroke-width", "1.7px");
    const unreadDot = bell.locator("[data-notification-indicator]");
    await expect(unreadDot).toHaveCSS("width", "5px");
    await expect(unreadDot).toHaveCSS("height", "5px");
    await expect(bell).toHaveText("");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await bell.click();
    const panel = page.getByTestId("notification-panel");
    await expect(panel).toBeVisible();
    // Radix first mounts its content offscreen while Floating UI measures the
    // anchor. Production bundles can mount faster than that positioning pass.
    await expect(panel).toBeInViewport({ ratio: 1 });
    const bounds = (await panel.boundingBox())!;
    const anchor = (await bell.boundingBox())!;
    const usableWidth = await page.evaluate(() => document.documentElement.getBoundingClientRect().width);
    expect(bounds.width).toBe(Math.min(404, usableWidth - 16));
    if (anchor.x + anchor.width - bounds.width >= 8) expect(bounds.x + bounds.width).toBeCloseTo(anchor.x + anchor.width, 0);
    else expect(bounds.x).toBeGreaterThanOrEqual(8);
    expect(bounds.y).toBeGreaterThanOrEqual(anchor.y + anchor.height + 7);
    expect(bounds.height).toBeLessThanOrEqual(640);
    await expect(page.locator("body")).not.toHaveAttribute("data-scroll-locked");
    await expect(panel.getByRole("tablist", { name: "Notification state" })).toHaveAttribute("aria-orientation", "horizontal");
    await expect(panel.getByRole("tab", { name: "All", exact: true })).toBeFocused();
    await expect(panel.getByRole("tab")).toHaveCount(2);
    await expect(panel.getByRole("checkbox")).toHaveCount(0);
    // The approved compact mock uses plain 16px Lucide outlines, not tonal
    // tiles or a different glyph for a given event. Preserve its 1.7px stroke.
    const compactIcons = [
      "lucide-file-pen-line", "lucide-arrow-right-left", "lucide-archive",
      "lucide-archive-restore", "lucide-user-round-check", "lucide-key-round",
      "lucide-trash-2", "lucide-file-pen-line", "lucide-user-round-key",
    ];
    const rows = panel.getByRole("listitem");
    await expect(panel.locator(".lucide-crown")).toHaveCount(0);
    for (const [index, glyph] of compactIcons.entries()) {
      const icon = rows.nth(index).locator("svg").first();
      await expect(icon).toHaveClass(new RegExp(`\\b${glyph}\\b`));
      await expect(icon).toHaveCSS("width", "16px");
      await expect(icon).toHaveCSS("height", "16px");
      await expect(icon).toHaveCSS("stroke-width", "1.7px");
    }
    for (const [label, glyph] of [
      ["Mark all notifications read", "lucide-check-check"],
      ["Close notifications", "lucide-x"],
      ["Watched documents", "lucide-settings-2"],
    ]) {
      const icon = panel.getByRole("button", { name: label, exact: true }).locator("svg");
      await expect(icon).toHaveClass(new RegExp(`\\b${glyph}\\b`));
      await expect(icon).toHaveCSS("width", "16px");
      await expect(icon).toHaveCSS("stroke-width", "1.7px");
    }
    const readControl = panel.getByRole("button", { name: "Mark read: Release notes updated", exact: true });
    const coarsePointer = await page.evaluate(() => matchMedia("(pointer: coarse)").matches);
    const readTarget = width >= 640 && !coarsePointer ? "24px" : "44px";
    await expect(readControl).toHaveCSS("width", readTarget);
    await expect(readControl).toHaveCSS("height", readTarget);
    await expect(readControl).toHaveCSS("margin-top", "1px");
    await expect(readControl.locator("span")).toHaveCSS("width", "6px");
    const readCheck = rows.nth(1).locator("[data-notification-read] svg");
    await expect(readCheck).toHaveClass(/lucide-check\b/);
    await expect(readCheck).toHaveCSS("stroke-width", "1.7px");
    if (width >= 640) {
      // An outside action remains usable on its first click, not just a
      // transparent backdrop dismissal that consumes the user's interaction.
      await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
      await expect(page.getByTestId("global-search-dialog")).toBeVisible();
      await expect(panel).toHaveCount(0);
      await page.keyboard.press("Escape");
      await bell.click();
    } else {
      await expect(panel.getByRole("button", { name: "Close notifications" })).toHaveCSS("width", "44px");
      const closeBox = (await panel.getByRole("button", { name: "Close notifications" }).boundingBox())!;
      expect(closeBox.x + closeBox.width).toBeCloseTo(bounds.x + bounds.width - 17, 0);
    }
    if (width === 1440 && !dark && !markFails) {
      await panel.getByRole("tab", { name: "Unread", exact: true }).click();
      await page.setViewportSize({ width: 375, height: 900 });
      await expect(panel).toBeVisible();
      await expect(panel.getByRole("tab", { name: "Unread", exact: true })).toHaveAttribute("aria-selected", "true");
      await expect.poll(async () => {
        const available = await page.evaluate(() => document.documentElement.getBoundingClientRect().width - 16);
        return Math.abs((await panel.boundingBox())!.width - Math.min(404, available));
      }).toBe(0);
      await expect(page.locator("body")).not.toHaveAttribute("data-scroll-locked");
      await page.setViewportSize({ width, height: 900 });
      await expect(panel).toBeVisible();
      await expect(page.locator("body")).not.toHaveAttribute("data-scroll-locked");
      await expect(panel.getByRole("tab", { name: "Unread", exact: true })).toHaveAttribute("aria-selected", "true");
      await panel.getByRole("tab", { name: "All", exact: true }).click();
    }
    await expect(panel.getByText("Release notes updated")).toBeVisible();
    await expect(panel.getByRole("link", { name: "Release notes updated", exact: true })).toHaveAccessibleDescription(/View document/);
    const notice = panel.getByRole("listitem").filter({ hasText: "A watched document was deleted" });
    await expect(notice).toHaveAttribute("data-actionable", "false");
    await expect(notice.getByText("Notice only", { exact: true })).toBeVisible();
    await expect(notice.getByRole("button", { name: "A watched document was deleted", exact: true })).toHaveCount(0);
    await expect(notice.getByRole("link")).toHaveCount(0);
    await expect(notice.getByRole("button", { name: "Mark unread: A watched document was deleted", exact: true })).toHaveAttribute("aria-disabled", "false");
    await notice.getByText("A watched document was deleted", { exact: true }).click();
    await expect(panel).toBeVisible();
    await expect(page).toHaveURL(/\/notifications\?state=all/);
    const firstRow = panel.getByRole("listitem").filter({ has: page.getByRole("link", { name: "Release notes updated", exact: true }) });
    expect((await firstRow.boundingBox())!.height).toBeGreaterThanOrEqual(64);
    expect((await firstRow.boundingBox())!.height).toBeLessThanOrEqual(72);
    expect(writes).toEqual([]);
    await firstRow.scrollIntoViewIfNeeded();
    expect(bounds.width).toBeLessThanOrEqual(width);
    if (width > 600) expect(bounds.width).toBeGreaterThanOrEqual(400);
    await page.screenshot({ path: testInfo.outputPath("notifications-panel.png") });
    if (width === 1440 && !dark && !markFails) {
      const rowActions = notice.getByRole("button", { name: "Mark unread: A watched document was deleted", exact: true });
      await rowActions.focus();
      await rowActions.press("Enter");
      await expect.poll(() => rowReadStarted).toBe(true);
      try {
        await expect(rowActions).toBeFocused();
      } finally { releaseRowRead(); }
      await expect(panel.getByRole("alert")).toContainText("Could not update notifications");
    }
    await expect(panel.getByRole("button", { name: "Mark all notifications read" })).toHaveAttribute("aria-disabled", "false");
    await panel.getByRole("tab", { name: "All", exact: true }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(panel.getByRole("tab", { name: "Unread", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(panel.getByRole("listitem")).toHaveCount(1);
    await expect(panel.getByRole("link", { name: "Release notes updated" })).toBeVisible();
    expect(writes).toEqual([]);
    await expect(panel.getByRole("link", { name: "View all notifications", exact: true })).toHaveAttribute("href", "/notifications?category=all&state=unread");
    await page.screenshot({ path: testInfo.outputPath("notifications-unread.png") });
    await panel.getByRole("link", { name: "Release notes updated" }).click();
    const reader = page.getByTestId("document-preview-dialog");
    await expect(reader).toBeVisible();
    await expect(panel).toHaveCount(0);
    await expect.poll(() => writes).toEqual([{ read: true, version: "v1" }]);
    await reader.getByRole("button", { name: /^Actions for / }).click();
    const watch = page.getByRole("menuitemcheckbox", { name: "Watch", exact: true });
    await expect(watch).toBeEnabled();
    await watch.click();
    await expect(page.getByRole("menuitemcheckbox", { name: "Unwatch", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await page.keyboard.press("Escape");
    await expect(page).toHaveURL(/\/notifications\?state=all/);
    await expect(page.getByRole("button", { name: `Notifications, ${markFails ? 1 : 0} unread` })).toBeFocused();
    if (markFails) {
      // Resource navigation survived a failed acknowledgement. Its separate
      // retry is available after the modal closes, without reopening the doc.
      await page.getByRole("button", { name: "Retry marking read" }).click();
      await expect(page.getByRole("button", { name: "Retry marking read" })).toHaveCount(0);
      await expect.poll(() => writes).toEqual([{ read: true, version: "v1" }, { read: true, version: "v1" }]);
    }
    await page.screenshot({ path: testInfo.outputPath("notifications-inbox.png") });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByRole("button", { name: "Notifications, 0 unread" }).click();
    await expect(panel.getByRole("button", { name: "Mark all notifications read" })).toHaveAttribute("aria-disabled", "true");
    await panel.getByRole("button", { name: "Watched documents", exact: true }).click();
    const settings = page.getByRole("dialog", { name: "Settings", exact: true });
    await expect(settings).toBeVisible();
    await expect(settings.getByText("You aren’t watching any documents yet.")).toBeVisible();
    await expect(page).toHaveURL(/\/notifications\?state=all$/);
    await expect(panel).toHaveCount(0);
    await expect(page.getByRole("menu")).toHaveCount(0);
    await settings.getByRole("button", { name: "Close settings", exact: true }).click();
    await expect(settings).toHaveCount(0);
    await expect(page).toHaveURL(/\/notifications\?state=all$/);
    if (width === 375 && !dark && !markFails) {
      await page.setViewportSize({ width: 844, height: 390 });
      await page.getByRole("button", { name: "Notifications, 0 unread", exact: true }).click();
      await expect(panel).toBeVisible();
      const shortBox = (await panel.boundingBox())!;
      expect(shortBox.y + shortBox.height).toBeLessThanOrEqual(390);
      await expect(panel.getByRole("button", { name: "Close notifications", exact: true })).toBeInViewport({ ratio: 1 });
      await expect(panel.getByRole("link", { name: "View all notifications", exact: true })).toBeInViewport({ ratio: 1 });
      await expect(panel.getByRole("tab", { name: "Unread", exact: true })).toBeInViewport({ ratio: 1 });
      await page.screenshot({ path: testInfo.outputPath("notifications-landscape.png") });
    }
  });
}
