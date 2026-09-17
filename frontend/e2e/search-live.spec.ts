// Opt-in isolated local runtime: no intercepted API responses or injected sessions.
import { randomBytes } from "node:crypto";
import { test, expect } from "@playwright/test";

test("real login, search filters, reload and exact-search options", async ({
  page,
  request,
  baseURL,
}) => {
  test.setTimeout(90000);
  test.skip(
    process.env.AKB_SEARCH_LIVE !== "1",
    "Requires an isolated local backend and frontend proxy",
  );
  expect(["127.0.0.1", "localhost"]).toContain(new URL(baseURL!).hostname);
  const suffix = randomBytes(6).toString("hex");
  const username = `browser_${suffix}`;
  const password = randomBytes(24).toString("base64url");
  const vault = `browser-search-${suffix}`;
  const registration = await request.post("/api/v1/auth/register", {
    data: { username, password, email: `${username}@example.invalid` },
  });
  expect(registration.ok()).toBeTruthy();
  await page.goto("/auth");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).not.toHaveURL(/\/auth/);
  const token = await page.evaluate(() => localStorage.getItem("akb_token"));
  expect(token).toBeTruthy();
  const headers = { Authorization: `Bearer ${token}` };
  expect(
    (await request.post(`/api/v1/vaults?name=${vault}`, { headers })).ok(),
  ).toBeTruthy();
  try {
    expect(
      (
        await request.post(`/api/v1/collections/${vault}`, {
          headers,
          data: { path: "guide" },
        })
      ).ok(),
    ).toBeTruthy();
    for (const [title, type, status] of [
      ["Live active report", "report", "active"],
      ["Live archived report", "report", "archived"],
      ["Live note", "note", "active"],
    ]) {
      expect(
        (
          await request.post("/api/v1/documents", {
            headers,
            data: {
              vault,
              collection: "guide",
              title,
              type,
              status,
              tags: ["ops"],
              content: "DeploymentNeedle API-223",
            },
          })
        ).ok(),
      ).toBeTruthy();
    }
    await page.goto(`/search?q=DeploymentNeedle&v=${vault}&mode=literal`);
    await expect(page.getByText("Live note", { exact: true })).toBeVisible();
    await expect(
      page.getByText("Live archived report", { exact: true }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Filter by document type" }).click();
    await page.getByRole("button", { name: "Toggle report" }).click();
    await expect(
      page.getByText("Live active report", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Live note", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "Document state", exact: true }).click();
    await page.getByRole("menuitemradio", { name: "All documents", exact: true }).click();
    await expect(
      page.getByText("Live archived report", { exact: true }),
    ).toBeVisible();
    await page.reload();
    await expect(
      page.getByText("Live archived report", { exact: true }),
    ).toBeVisible();
    await page.getByLabel("Search query", { exact: true }).fill("API-[0-9]+");
    await page.getByLabel("Search query", { exact: true }).press("Enter");
    await expect(
      page.getByText("Live active report", { exact: true }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Filter by document type" }).click();
    await page.getByLabel("Regular expression").click();
    await expect(
      page.getByText("Live active report", { exact: true }),
    ).toBeVisible();
    await page
      .getByLabel("Search query", { exact: true })
      .fill("DeploymentNeedle");
    await page.getByLabel("Search query", { exact: true }).press("Enter");
    await expect
      .poll(
        async () => {
          const response = await request.get(
            `/api/v1/search?q=DeploymentNeedle&vault=${vault}&doc_types=report`,
            { headers },
          );
          return (await response.json()).results?.length;
        },
        { timeout: 30000 },
      )
      .toBe(1);
    const semanticResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === "/api/v1/search" && url.searchParams.get("vault") === vault;
    });
    await page.getByRole("button", { name: "Semantic", exact: true }).click();
    const semantic = await semanticResponse;
    expect(semantic.ok()).toBeTruthy();
    expect((await semantic.json()).results).toHaveLength(2);
    await expect(page.getByRole("button", { name: "Semantic", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("region", { name: "Search results", exact: true })).toHaveAttribute("aria-busy", "false");
    await expect(
      page.getByText("Live active report", { exact: true }),
    ).toBeVisible({ timeout: 30000 });
    await expect(page.getByText("Live note", { exact: true })).toHaveCount(0);
    await page.screenshot({
      path: test.info().outputPath("live-search.png"),
      fullPage: true,
    });
  } finally {
    expect(
      (await request.delete(`/api/v1/vaults/${vault}`, { headers })).ok(),
    ).toBeTruthy();
  }
});
