import { expect, test } from "@playwright/test";
import { readingFixture } from "./fixtures/reading-workspace";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated HTTP fixtures.");

for (const width of [375, 1440]) {
  test(`renames document titles from the available actions at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await readingFixture(page, width > 1000);
    let title = "Reading workflow";
    let revision = "opaque-fixture-revision-1";
    const content = "# Reading workflow\n\nKeep this original content.";
    const writes: Record<string, unknown>[] = [];
    await page.route("**/api/v1/documents/fixture/reading.md", async route => {
      if (route.request().method() === "PATCH") {
        const payload = route.request().postDataJSON();
        expect(payload).toEqual({ title: expect.any(String), expected_commit: revision, title_conflict_policy: "reject" });
        writes.push(payload);
        title = payload.title;
        revision = "opaque-fixture-revision-2";
        return route.fulfill({ json: { current_commit: revision, path: "reading.md" } });
      }
      return route.fulfill({ json: { path: "reading.md", title, content, uri: "akb://fixture/doc/reading.md",
        current_commit: revision, status: "active", created_by_name: "Reading reviewer" } });
    });
    await page.route("**/api/v1/browse/fixture?*", route => route.fulfill({ json: {
      complete: true, scanned: 1, items: [{ type: "document", path: "reading.md", name: title }],
    } }));
    await page.goto("/vault/fixture/doc/reading.md");
    const canvas = page.locator("#document-reading-canvas");
    await expect(canvas.getByRole("heading", { name: "Reading workflow", level: 1, exact: true })).toBeVisible();
    await expect(canvas.locator("h1")).toHaveCount(1);
    const tools = page.getByRole("group", { name: "Document reading tools", exact: true });
    const more = tools.getByRole("button", { name: "Actions for Reading workflow", exact: true });
    const sidebar = page.getByRole("navigation", { name: "fixture explorer", exact: true });
    if (width > 1000) await sidebar.getByRole("button", { name: "Actions for Reading workflow", exact: true }).click();
    else await more.click();
    await page.getByRole("menuitem", { name: "Rename", exact: true }).press("Enter");
    const dialog = page.getByRole("dialog", { name: "Rename document", exact: true });
    const field = dialog.getByRole("textbox", { name: "Title", exact: true });
    await expect(field).toBeFocused();
    await expect(field).toHaveValue("Reading workflow");
    expect(await field.evaluate((element: HTMLInputElement) => element.selectionEnd! - element.selectionStart!)).toBe("Reading workflow".length);
    await field.fill("새로운 문서 제목");
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("rename-document.png") });
    await dialog.getByRole("button", { name: "Save title", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("tab", { name: "새로운 문서 제목", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(tools.getByRole("button", { name: "Actions for 새로운 문서 제목", exact: true })).toBeVisible();
    const renamedTitle = canvas.getByRole("heading", { name: "새로운 문서 제목", level: 1, exact: true });
    await expect(renamedTitle).toBeVisible();
    await expect(canvas.locator("h1")).toHaveCount(1);
    const authoredHeading = canvas.getByRole("heading", { name: "Reading workflow", level: 2, exact: true });
    await expect(authoredHeading).toHaveAttribute("id", "reading-workflow");
    const titleBox = (await renamedTitle.boundingBox())!;
    const metadataBox = (await canvas.getByRole("group", { name: "Document metadata", exact: true }).boundingBox())!;
    const sectionBox = (await authoredHeading.boundingBox())!;
    expect(titleBox.y).toBeLessThan(metadataBox.y);
    expect(metadataBox.y).toBeLessThan(sectionBox.y);
    await page.screenshot({ path: testInfo.outputPath("renamed-document-title.png") });
    if (width > 1000) {
      const renamedRow = sidebar.getByRole("button", { name: "Actions for 새로운 문서 제목", exact: true });
      await expect(renamedRow).toBeFocused();
      await expect(sidebar.getByRole("link", { name: /Document: 새로운 문서 제목/ })).toBeVisible();
    } else await expect(tools.getByRole("button", { name: "Actions for 새로운 문서 제목", exact: true })).toBeFocused();
    expect(writes).toHaveLength(1);
    expect(writes[0]).not.toHaveProperty("content");
    await expect(page).toHaveURL(/\/doc\/reading.md$/);
    await tools.getByRole("radio", { name: "Raw markdown", exact: true }).click();
    await expect(page.getByTestId("doc-raw")).toHaveText(content);
    await expect(renamedTitle).toBeVisible();
    await tools.getByRole("button", { name: "Actions for 새로운 문서 제목", exact: true }).click();
    await page.getByRole("menuitem", { name: "Rename", exact: true }).click();
    await expect(field).toHaveValue("새로운 문서 제목");
    await expect(dialog.getByRole("button", { name: "Save title", exact: true })).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
  });
}
