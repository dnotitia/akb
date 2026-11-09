import { expect, test } from "@playwright/test";
import { readingFixture } from "./fixtures/reading-workspace";

for (const viewport of [{ width: 1440, height: 900 }, { width: 375, height: 667 }, { width: 1024, height: 320 }]) {
  test(`an inaccessible document has padded, reachable recovery at ${viewport.width}×${viewport.height}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await readingFixture(page, viewport.width === 375);
    await page.route("**/api/v1/documents/fixture/**", route => route.fulfill({ status: 403, json: { detail: "Private server error: secret title" } }));
    await page.goto("/vault/fixture/doc/private.md");
    const state = page.getByRole("alert", { name: "You don’t have access to this document" });
    await expect(state).toBeVisible();
    await expect(state.getByText(/Ask the Vault owner/)).toBeVisible();
    await expect(page.getByText("Private server error: secret title")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
    const browse = state.getByRole("link", { name: "Browse Vaults" });
    await expect(browse).toHaveAttribute("href", "/vault");
    await browse.scrollIntoViewIfNeeded();
    await expect(browse).toBeInViewport();
    const bounds = await state.boundingBox();
    const canvas = await page.getByRole("region", { name: "Document workspace" }).boundingBox();
    expect(bounds!.x - canvas!.x).toBeGreaterThanOrEqual(16);
    expect(canvas!.x + canvas!.width - bounds!.x - bounds!.width).toBeGreaterThanOrEqual(16);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("unavailable-document.png") });
    await browse.click();
    await expect(page).toHaveURL(/\/vault$/);
  });
}

test("access can be checked again without leaving the document link", async ({ page }) => {
  await readingFixture(page, false, { "reading.md": "# Reading workflow\n\nRestored content." });
  let allowed = false;
  await page.route("**/api/v1/documents/fixture/reading.md", route => allowed ? route.fallback() : route.fulfill({ status: 403, json: { detail: "Forbidden" } }));
  await page.goto("/vault/fixture/doc/reading.md");
  await expect(page.getByRole("heading", { name: "You don’t have access to this document" })).toBeVisible();
  allowed = true;
  await page.getByRole("button", { name: "Check access again" }).click();
  await expect(page.getByRole("heading", { name: "Reading workflow", level: 1 })).toBeVisible();
  await expect(page.getByText("Restored content.", { exact: true })).toBeVisible();
  await expect(page.getByRole("alert", { name: "You don’t have access to this document" })).toHaveCount(0);
});

test("a missing document avoids claiming the document exists", async ({ page }) => {
  await readingFixture(page);
  await page.route("**/api/v1/documents/fixture/**", route => route.fulfill({ status: 404, json: { detail: "Not Found" } }));
  await page.goto("/vault/fixture/doc/missing.md");
  const state = page.getByRole("alert", { name: "Document unavailable" });
  await expect(state).toBeVisible();
  await expect(state.getByText(/moved, been removed, or be outside your access/)).toBeVisible();
  await expect(state.getByRole("link", { name: "Browse Vaults" })).toHaveAttribute("href", "/vault");
});
