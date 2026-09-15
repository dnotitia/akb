import { expect, test } from "@playwright/test";

// Opt in only against the isolated runtime after enabling account_self_service_enabled.
// This test creates its own account and never operates on an existing user's data.
test("Security: revoke two local sessions, retain PAT, reauthenticate and delete account", async ({ page, request }) => {
  test.skip(process.env.AKB_FE_E2E_MODE !== "real" || process.env.AKB_ACCOUNT_SELF_SERVICE_E2E !== "1", "Requires isolated local runtime with account self-service enabled.");
  test.setTimeout(60_000);
  page.setDefaultTimeout(10_000);
  const username = `security-e2e-${Date.now()}`;
  const password = "Security-E2E-local-123!"; // pragma: allowlist secret
  const endpoint = "/api/v1";
  const bearer = (token: string) => ({ Authorization: `Bearer ${token}` });
  const login = async () => {
    const response = await request.post(`${endpoint}/auth/login`, { data: { username, password } });
    expect(response.ok()).toBeTruthy();
    return (await response.json()).token as string;
  };
  const registered = await request.post(`${endpoint}/auth/register`, { data: { username, email: `${username}@example.invalid`, password } });
  expect(registered.ok()).toBeTruthy();
  let deleted = false;
  let userId = "";
  try {
    const first = await login();
    const second = await login();
    const identity = await request.get(`${endpoint}/auth/me`, { headers: bearer(first) });
    userId = (await identity.json()).user_id;
    const patResponse = await request.post(`${endpoint}/auth/tokens`, { headers: bearer(first), data: { name: "security-lifecycle-e2e" } });
    expect(patResponse.ok()).toBeTruthy();
    const pat = (await patResponse.json()).token;
    await page.goto("/auth");
    await page.evaluate(token => localStorage.setItem("akb_token", token), first);
    await page.goto("/settings?tab=security");
    const revoke = page.getByRole("button", { name: "Sign out all sessions", exact: true });
    await expect(revoke).toBeEnabled();
    await revoke.click();
    const confirmation = page.getByRole("dialog", { name: "Sign out all sessions?" });
    await expect(confirmation.getByRole("button", { name: "Cancel" })).toBeFocused();
    await confirmation.getByRole("button", { name: "Sign out all sessions", exact: true }).click();
    await expect(page).toHaveURL(/\/auth\?reason=sessions-revoked$/);
    expect((await request.get(`${endpoint}/auth/me`, { headers: bearer(first) })).status()).toBe(401);
    expect((await request.get(`${endpoint}/auth/me`, { headers: bearer(second) })).status()).toBe(401);
    expect((await request.get(`${endpoint}/auth/me`, { headers: bearer(pat) })).status()).toBe(200);
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page).not.toHaveURL(/\/auth/);
    const fresh = await page.evaluate(() => localStorage.getItem("akb_token"));
    expect(fresh).toBeTruthy();
    await page.goto("/settings?tab=security");
    const review = page.getByRole("button", { name: "Review account deletion", exact: true });
    await expect(review).toBeEnabled();
    await review.click();
    await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
    await expect(review).toBeFocused();
    await review.click();
    await page.getByLabel("Current password").fill(password);
    await page.getByRole("button", { name: "Continue to confirmation" }).click();
    const destroy = page.getByRole("button", { name: "Permanently delete account", exact: true });
    await expect(destroy).toBeDisabled();
    await page.getByLabel("Type your username to confirm").fill(username);
    await destroy.click();
    await expect(page).toHaveURL(/\/auth\?reason=account-deleted$/);
    deleted = true;
    expect((await request.get(`${endpoint}/auth/me`, { headers: bearer(fresh!) })).status()).toBe(401);
    expect((await request.get(`${endpoint}/auth/me`, { headers: bearer(pat) })).status()).toBe(401);
    expect(await page.evaluate(() => localStorage.getItem("akb_token"))).toBeNull();
  } finally {
    if (!deleted && userId) {
      const fresh = await login();
      const cleanup = await request.post(`${endpoint}/my/account/deletion`, { headers: bearer(fresh), data: { expected_user_id: userId, confirm_username: username, current_password: password } });
      expect(cleanup.ok(), "Ephemeral account cleanup").toBeTruthy();
    }
  }
});
