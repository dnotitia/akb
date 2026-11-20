import { expect, type Page } from "@playwright/test";

export async function readerNavigation(page: Page) {
  const sheet = page.getByRole("dialog", { name: "Contents", exact: true });
  const trigger = page.getByRole("button", { name: "Open contents", exact: true });
  const panel = page.locator(".document-contents-rail").or(sheet);
  await expect(panel.or(trigger)).toBeVisible();
  if (!await panel.isVisible() && await trigger.isVisible()) await trigger.click();
  await expect(panel).toBeVisible();
  return panel;
}

export async function readingSettings(page: Page) {
  const direct = page.getByRole("button", { name: "Reading settings", exact: true });
  if (await direct.isVisible()) await direct.click();
  else {
    const more = page.getByRole("group", { name: "Document reading tools", exact: true }).getByRole("button", { name: /^Actions for / });
    await more.click(); await page.getByRole("menuitem", { name: "Reading settings", exact: true }).click();
  }
  const settings = page.getByRole("dialog", { name: "Reading settings", exact: true });
  await expect(settings).toBeVisible();
  return settings;
}
