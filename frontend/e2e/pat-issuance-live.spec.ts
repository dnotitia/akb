import { expect, test } from "@playwright/test";

// Explicit opt-in against the repository-owned local runtime. Creates only
// disposable resources; the ordinary developer deployment is never a target.
test("PAT options preserve expiry and Vault scope through creation and replacement", async ({ page, request }) => {
  test.skip(process.env.AKB_FE_E2E_MODE !== "real" || process.env.AKB_PAT_ISSUANCE_E2E !== "1", "Requires isolated runtime with PAT issuance v1 and account cleanup enabled.");
  test.setTimeout(90_000);
  page.setDefaultTimeout(10_000);
  const username = `pat-options-${Date.now()}`;
  const password = "Pat-Options-local-123!"; // pragma: allowlist secret
  const endpoint = "/api/v1";
  const bearer = (token: string) => ({ Authorization: `Bearer ${token}` });
  const ownedVaults: string[] = [];
  const registered = await request.post(`${endpoint}/auth/register`, { data: { username, email: `${username}@example.invalid`, password } });
  expect(registered.ok()).toBeTruthy();
  const login = await request.post(`${endpoint}/auth/login`, { data: { username, password } });
  expect(login.ok()).toBeTruthy();
  const jwt = (await login.json()).token as string;
  const identity = await request.get(`${endpoint}/auth/me`, { headers: bearer(jwt) });
  const userId = (await identity.json()).user_id;
  try {
    const inside = `${username}-inside`;
    const outside = `${username}-outside`;
    for (const vault of [inside, outside]) {
      const response = await request.post(`${endpoint}/vaults`, { headers: bearer(jwt), params: { name: vault } });
      expect(response.ok()).toBeTruthy();
      ownedVaults.push(vault);
    }
    await page.goto("/auth");
    await page.evaluate(token => localStorage.setItem("akb_token", token), jwt);
    await page.goto("/settings?tab=tokens");
    await page.getByLabel("Token name", { exact: true }).fill("restricted browser token");
    await page.getByRole("button", { name: "Advanced options", exact: true }).click();
    // Accessible labels below are shared by Home and Settings through one form.
    await page.getByLabel("Expiration", { exact: true }).click();
    await page.getByRole("menuitemradio", { name: "30 days", exact: true }).click();
    await page.getByLabel("Vault write restriction", { exact: true }).click();
    await page.getByRole("menuitemradio", { name: "Selected Vaults or name prefixes", exact: true }).click();
    await page.getByLabel("Exact Vault name", { exact: true }).fill(inside);
    await page.getByRole("button", { name: "Add Vault", exact: true }).click();
    // Capture only the reviewed form, before any token secret exists.
    await page.setViewportSize({ width: 1536, height: 1000 });
    await page.screenshot({ path: test.info().outputPath("pat-options-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await page.screenshot({ path: test.info().outputPath("pat-options-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1280, height: 900 });
    const createdResponse = page.waitForResponse(response => response.url().endsWith("/auth/tokens/issuance") && response.request().method() === "POST");
    await page.getByRole("button", { name: "Create token", exact: true }).click();
    const created = await createdResponse;
    expect(created.ok()).toBeTruthy();
    const issued = await created.json();
    expect(issued.scopes).toEqual(["read", "write"]);
    expect(issued.vault_scope).toEqual({ prefixes: [], extra_vaults: [inside] });
    expect(Date.parse(issued.expires_at) - Date.parse(issued.issued_at)).toBe(30 * 86_400_000);
    await expect(page.getByText("Token created — save it now", { exact: true })).toBeVisible();
    expect((await request.patch(`${endpoint}/vaults/${inside}`, { headers: bearer(issued.token), data: { description: "Allowed scoped write" } })).status()).toBe(200);
    expect((await request.patch(`${endpoint}/vaults/${outside}`, { headers: bearer(issued.token), data: { description: "Must be denied" } })).status()).toBe(403);
    expect((await request.get(`${endpoint}/vaults/${outside}/info`, { headers: bearer(issued.token) })).status()).toBe(200);
    expect((await request.post(`${endpoint}/auth/tokens`, { headers: bearer(issued.token), data: { name: "Must not mint child" } })).status()).toBe(403);
    const listed = await request.get(`${endpoint}/auth/tokens`, { headers: bearer(jwt) });
    const metadata = (await listed.json()).tokens.find((token: { token_id: string }) => token.token_id === issued.token_id);
    expect(metadata.vault_scope).toEqual(issued.vault_scope);
    expect(metadata.expires_at).toBe(issued.expires_at);
    expect(metadata.token).toBeUndefined();
    await page.getByRole("button", { name: "Replace token restricted browser token", exact: true }).click();
    const replacement = page.getByRole("dialog", { name: "Replace token restricted browser token", exact: true });
    await expect(replacement.getByLabel("Token name", { exact: true })).toHaveValue("restricted browser token");
    const replacementResponse = page.waitForResponse(response => response.url().endsWith("/auth/tokens/issuance") && response.request().method() === "POST");
    await replacement.getByRole("button", { name: "Create replacement token", exact: true }).click();
    const replacementCreated = await replacementResponse;
    expect(replacementCreated.ok()).toBeTruthy();
    const next = await replacementCreated.json();
    expect(next.token_id).not.toBe(issued.token_id);
    expect(next.scopes).toEqual(issued.scopes);
    expect(next.vault_scope).toEqual(issued.vault_scope);
    expect(next.expires_at).toBe(issued.expires_at);
    expect((await request.get(`${endpoint}/auth/me`, { headers: bearer(issued.token) })).status()).toBe(200);
    await replacement.getByRole("button", { name: "Revoke original token", exact: true }).click();
    await page.getByRole("dialog", { name: 'Revoke "restricted browser token"?', exact: true }).getByRole("button", { name: "Revoke token", exact: true }).click();
    await expect(replacement.getByText("The original token was revoked. Keep the replacement secret somewhere private.")).toBeVisible();
    expect((await request.get(`${endpoint}/auth/me`, { headers: bearer(issued.token) })).status()).toBe(401);
    expect((await request.get(`${endpoint}/auth/me`, { headers: bearer(next.token) })).status()).toBe(200);
  } finally {
    for (const vault of ownedVaults.reverse()) {
      expect((await request.delete(`${endpoint}/vaults/${vault}`, { headers: bearer(jwt) })).ok()).toBeTruthy();
    }
    const cleanup = await request.post(`${endpoint}/my/account/deletion`, { headers: bearer(jwt), data: { expected_user_id: userId, confirm_username: username, current_password: password } });
    expect(cleanup.ok(), "Ephemeral account cleanup").toBeTruthy();
  }
});
