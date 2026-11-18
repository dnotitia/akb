import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";

test.use({ trace: "off" });

test("monitoring signs in separately, shows PAT aliases, and clears the report after revocation", async ({ page, request }) => {
  const appOrigin = process.env.AKB_MONITORING_E2E_APP_ORIGIN;
  const adminUsername = process.env.AKB_E2E_USERNAME;
  const adminPassword = process.env.AKB_E2E_PASSWORD;
  test.skip(!appOrigin || !adminUsername || !adminPassword, "Requires the isolated AKB runtime.");
  test.setTimeout(60_000);

  const login = await request.post("/api/v1/admin/auth/local/login", {
    data: { username: adminUsername, password: adminPassword },
  });
  expect(login.status()).toBe(200);
  const admin = { Authorization: `Bearer ${(await login.json()).token}` };

  const username = `monitoring-web-${randomUUID().slice(0, 12)}`;
  const password = randomUUID();
  const account = await request.post("/api/v1/auth/register", {
    data: { username, password, email: `${username}@example.invalid` },
  });
  expect(account.status()).toBe(200);
  const monitorId = (await account.json()).user_id;
  const access = `/api/v1/admin/mcp-monitoring/access/${monitorId}`;
  const names = [`private-label-${randomUUID()}`, `private-label-${randomUUID()}`];
  const tokenIds: string[] = [];
  const actorName = `monitoring-actor-${randomUUID().slice(0, 12)}`;
  const actorAccount = await request.post("/api/v1/auth/register", {
    data: { username: actorName, password: randomUUID(), email: `${actorName}@example.invalid` },
  });
  expect(actorAccount.status()).toBe(200);
  const target = (await actorAccount.json()).user_id;
  const keys = `/api/v1/admin/users/${target}/tokens`;
  try {
    expect((await request.put(access, { headers: admin, data: { enabled: true } })).status()).toBe(204);
    for (const name of names) {
      const created = await request.post(keys, { headers: admin, data: { name } });
      expect(created.status()).toBe(200);
      const { token, token_id } = await created.json();
      tokenIds.push(token_id);
      const call = await request.post(`${appOrigin}/mcp/`, {
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/json, text/event-stream",
          "MCP-Protocol-Version": "2025-06-18",
        },
        data: { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "akb_whoami", arguments: {} } },
      });
      expect(call.status()).toBe(200);
    }

    await page.goto("/admin/mcp-monitoring");
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.getByRole("heading", { name: "MCP monitoring" })).toBeVisible();
    const actor = page.getByRole("table", { name: "Users", exact: true })
      .getByRole("button", { name: actorName, exact: true });
    await expect(async () => {
      await page.getByRole("button", { name: "Refresh", exact: true }).click();
      await expect(actor).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });
    await actor.click();

    const aliases = page.getByRole("table", { name: "Credentials", exact: true })
      .getByRole("button", { name: /^PAT \d+$/ });
    await expect(aliases).toHaveCount(2);
    for (const value of [...names, ...tokenIds]) {
      await expect(page.getByText(value, { exact: false })).toHaveCount(0);
    }

    expect((await request.put(access, { headers: admin, data: { enabled: false } })).status()).toBe(204);
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
    await expect(aliases).toHaveCount(0);
  } finally {
    for (const id of tokenIds) await request.delete(`${keys}/${id}`, { headers: admin });
    await request.delete(`/api/v1/admin/users/${monitorId}`, { headers: admin });
    await request.delete(`/api/v1/admin/users/${target}`, { headers: admin });
  }
});
