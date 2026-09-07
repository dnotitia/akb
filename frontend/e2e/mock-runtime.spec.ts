import { test, expect } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE !== "mock", "Requires the explicit mock mode");

test("mock runtime exposes readiness, discovery, and reset", async ({ page }) => {
  await page.goto("/auth");

  const contract = await page.evaluate(async () => {
    const health = await fetch("/__akb_mock__/health").then((response) => response.json());
    const discovery = await fetch("/__akb_mock__/discover").then((response) => response.json());
    const reset = await fetch("/__akb_mock__/reset", { method: "POST" }).then((response) => response.json());
    return { health, discovery, reset };
  });

  expect(contract.health).toEqual({ status: "ready", mode: "mock" });
  expect(contract.discovery.mode).toBe("mock");
  expect(contract.discovery.worker.url).toBe("/mockServiceWorker.js");
  expect(contract.discovery.reset).toEqual({
    method: "POST",
    path: "/__akb_mock__/reset",
  });
  expect(contract.reset).toEqual({ status: "ready", mode: "mock" });
  await expect(page.getByRole("tab", { name: /^register$/i })).toBeVisible();
});
