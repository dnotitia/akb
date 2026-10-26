import { expect, test } from "@playwright/test";

for (const scenario of ["transient", "initial-conflict", "retry-conflict"] as const) {
  test(`F26 full Inbox route unmount: ${scenario}`, async ({ page }) => {
    test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Own HTTP fixtures, no MSW.");
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(() => localStorage.setItem("akb_token", "notification-recovery-fixture"));
    const writes: { read: boolean; version: string }[] = [];
    let finishInitial!: () => void;
    const initialResponse = new Promise<void>(resolve => { finishInitial = resolve; });
    let version = "v1", read = false, identityProofs = 0;
    const summary = () => ({ supported: true, unread_count: read ? 0 : 1, snapshot: "current-snapshot", retention_days: 90 });
    await page.route("**/health/**", route => route.fulfill({ json: {} }));
    await page.route("**/api/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
      if (path.endsWith("/auth/me")) { identityProofs++; return route.fulfill({ json: { user_id: "recovery-reader", username: "reader", display_name: "Reader", email: "reader@example.invalid", is_admin: false, auth_method: "local" } }); }
      if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "demo", name: "demo", role: "reader" }] } });
      if (path.endsWith("/vaults/demo/info")) return route.fulfill({ json: { name: "demo", role: "reader", document_count: 0, collection_count: 0, table_count: 0, file_count: 0, is_archived: false, is_external_git: false } });
      if (path.endsWith("/notifications/unread-count")) return route.fulfill({ json: summary() });
      if (path.endsWith("/notifications/n1")) {
        const body = route.request().postDataJSON(); writes.push(body);
        if (writes.length === 1) {
          await initialResponse;
          if (scenario !== "transient") version = "v2";
          return route.fulfill({ status: scenario === "initial-conflict" ? 409 : 503, json: { detail: "Read state changed or unavailable" } });
        }
        if (body.version !== version) return route.fulfill({ status: 409, json: {} });
        read = true; return route.fulfill({ json: summary() });
      }
      if (path.endsWith("/notifications")) return route.fulfill({ json: { ...summary(), category: "all", next_cursor: null, items: [{ id: "n1", kind: "access.changed", title: version === "v1" ? "Vault access changed" : "New access change to review", message: "A personal access notice", created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z", version, read, target: version === "v1" ? { uri: "akb://demo", vault: "demo" } : null }] } });
      return route.fulfill({ json: { items: [], changes: [], history: [], relations: [], docs: [], collections: [], tables: [], files: [], total: 0 } });
    });
    await page.goto("/notifications?state=all");
    await expect(page.getByRole("link", { name: "Vault access changed", exact: true })).toBeVisible();
    const initialProofs = identityProofs;
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect.poll(() => identityProofs).toBeGreaterThan(initialProofs);
    // Full Inbox and the header failure owner must share the new access epoch.
    await expect(page.getByRole("link", { name: "Vault access changed", exact: true })).toBeVisible();
    await page.getByRole("link", { name: "Vault access changed", exact: true }).click();
    await expect(page).toHaveURL(/\/vault\/demo$/);
    await expect(page.getByRole("heading", { name: "Notifications", exact: true })).toHaveCount(0);
    // Deliberately settle only after the full Inbox has disappeared.
    finishInitial();
    if (scenario !== "initial-conflict") await page.getByRole("button", { name: "Retry marking read" }).click();
    if (scenario === "transient") {
      await expect(page.getByRole("button", { name: "Retry marking read" })).toHaveCount(0);
      await expect.poll(() => writes).toEqual([{ read: true, version: "v1" }, { read: true, version: "v1" }]);
    } else {
      await expect(page.getByRole("button", { name: "Retry marking read" })).toHaveCount(0);
      const attempts = scenario === "retry-conflict" ? 2 : 1;
      await expect.poll(() => writes.length).toBe(attempts);
      await page.getByRole("button", { name: "Review current notification" }).click();
      const panel = page.getByTestId("notification-panel");
      await expect(panel.getByText("New access change to review", { exact: true })).toBeVisible();
      await expect(panel.getByRole("link", { name: "New access change to review" })).toHaveCount(0);
      expect(writes).toEqual(Array.from({ length: attempts }, () => ({ read: true, version: "v1" })));
      // No automatic acknowledgement while refreshing/reviewing v2.
      await panel.getByRole("button", { name: "Mark read: New access change to review", exact: true }).click();
      await expect.poll(() => writes.at(-1)).toEqual({ read: true, version: "v2" });
      await expect.poll(() => writes.length).toBe(attempts + 1);
    }
  });
}
