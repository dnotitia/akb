import { expect, test, type Page } from "@playwright/test";
import axe from "axe-core";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");
const userId = "00000000-0000-0000-0000-000000000001";
const keyId = "00000000-0000-0000-0000-0000000000a1";

async function setup(page: Page, { dark = false } = {}) {
  const deleted: string[] = [];
  let connections = [
    { id: "c-oauth", client_name: "claude-code", client_version: "2.1.0", auth_kind: "oauth", token_id: null, token_prefix: null, token_name: null, first_seen_at: "2026-11-01T09:00:00Z", last_seen_at: "2026-11-15T09:00:00Z" },
    { id: "c-pat", client_name: "codex", client_version: "0.46.0", auth_kind: "pat", token_id: keyId, token_prefix: "akb_lap", token_name: "laptop", first_seen_at: "2026-11-02T09:00:00Z", last_seen_at: "2026-11-14T09:00:00Z" },
  ];
  let tokens = [{ token_id: keyId, name: "laptop", prefix: "akb_lap", key_class: "pat", scopes: ["read", "write"], vault_scope: null, expires_at: null, created_at: "2026-10-01T00:00:00Z", last_used_at: "2026-11-14T09:00:00Z" }];
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "connected-apps-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, { dark });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: true } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: userId, username: "reviewer", display_name: "Connection reviewer", email: "reviewer@example.invalid", is_admin: false, auth_method: "jwt", key_class: null } });
    if (path.endsWith("/auth/connections")) return route.fulfill({ json: { connections } });
    if (path.endsWith(`/auth/tokens/${keyId}`) && request.method() === "DELETE") {
      deleted.push(keyId);
      tokens = []; connections = connections.filter(item => item.token_id !== keyId);
      return route.fulfill({ json: { revoked: true } });
    }
    if (path.endsWith("/auth/tokens")) return route.fulfill({ json: { tokens } });
    return route.fulfill({ json: { vaults: [], tokens: [], items: [], changes: [], templates: [], unread_count: 0, supported: true } });
  });
  return () => deleted;
}

for (const dark of [false, true]) test(`connected apps list each AI app and how it connects (${dark ? "dark" : "light"})`, async ({ page }, testInfo) => {
  await setup(page, { dark });
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/settings?tab=tokens&connectionView=apps");
  await expect(page.getByRole("tab", { name: "Connected apps", exact: true })).toHaveAttribute("aria-selected", "true");
  const list = page.getByRole("list", { name: "Connected apps" });
  await expect(list.getByRole("listitem")).toHaveCount(2);
  await expect(list.getByRole("listitem").nth(0)).toContainText("claude-code");
  await expect(list.getByRole("listitem").nth(0)).toContainText("Sign-in");
  await expect(list.getByRole("listitem").nth(1)).toContainText("Key laptop");
  await expect(list.getByRole("button", { name: "Disconnect codex" })).toBeEnabled();
  await expect(list.getByRole("button", { name: "Disconnect claude-code" })).toHaveCount(0);
  await page.addScriptTag({ content: axe.source });
  const violations = await page.evaluate(async () => {
    const report = await (window as unknown as { axe: typeof axe }).axe.run(document.querySelector('[data-testid="account-settings-dialog"]')!, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"] },
    });
    return report.violations.map(issue => ({ id: issue.id, nodes: issue.nodes.map(node => node.html) }));
  });
  expect(violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath(`connected-apps-${dark ? "dark" : "light"}.png`) });
});

test("connected apps fit a phone width without horizontal scroll", async ({ page }, testInfo) => {
  await setup(page);
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/settings?tab=tokens&connectionView=apps");
  await expect(page.getByRole("list", { name: "Connected apps" }).getByRole("listitem")).toHaveCount(2);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({ path: testInfo.outputPath("connected-apps-375.png") });
});

test("disconnecting a key-based app revokes its key after confirmation", async ({ page }) => {
  const deleted = await setup(page);
  await page.goto("/settings?tab=tokens&connectionView=apps");
  await page.getByRole("button", { name: "Disconnect codex" }).click();
  expect(deleted()).toEqual([]);
  const confirm = page.getByRole("dialog", { name: 'Revoke "laptop"?' });
  await confirm.getByRole("button", { name: "Revoke key" }).click();
  await expect(page.getByRole("list", { name: "Connected apps" }).getByRole("listitem")).toHaveCount(1);
  expect(deleted()).toEqual([keyId]);
});
