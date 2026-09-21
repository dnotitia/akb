import { expect, test, type APIRequestContext } from "@playwright/test";

type MockDescriptor = {
  scenario?: string;
  mock?: {
    fixture?: {
      identity?: { start_url?: string };
      operations?: Record<string, { method: "GET" | "POST"; url: string }>;
    } | null;
  };
};

test.describe("common Markdown image rendering mock contract", () => {
  test.skip(
    process.env.AKB_FE_E2E_SCENARIO !== "markdown-image-rendering",
    "The image fixture is selected by AKB_FE_E2E_SCENARIO",
  );

  async function fixture(request: APIRequestContext) {
    const response = await request.get("/__akb_mock__/discover");
    expect(response.ok()).toBeTruthy();
    const descriptor = (await response.json()) as MockDescriptor;
    expect(descriptor.scenario).toBe("markdown-image-rendering");
    const imageFixture = descriptor.mock?.fixture;
    expect(imageFixture?.identity?.start_url).toBeTruthy();
    return imageFixture!;
  }

  test.beforeEach(async ({ request }) => {
    const response = await request.post("/__akb_mock__/reset", {
      data: { scenario: "markdown-image-rendering" },
    });
    expect(response.ok()).toBeTruthy();
  });

  test("shows available, request-failure, and decode-failure states", async ({ page, request }) => {
    const imageFixture = await fixture(request);
    await page.goto(imageFixture.identity!.start_url!);

    const frames = page.locator("[data-markdown-image-frame]");
    await expect(frames).toHaveCount(3);
    await expect(frames.nth(0)).toHaveAttribute("data-markdown-image-state", "available");
    await expect(frames.nth(0).locator("img")).toHaveAttribute(
      "data-markdown-target",
      "/api/assets/123e4567-e89b-42d3-a456-426614174000",
    );
    await expect(frames.nth(1)).toHaveAttribute("data-markdown-image-state", "unavailable");
    await expect(frames.nth(1)).toHaveAccessibleName("Image unavailable: Request failure");
    await expect(frames.nth(2)).toHaveAttribute("data-markdown-image-state", "decode");
    await expect(frames.nth(2)).toHaveAccessibleName("Image unavailable: Decode failure");
  });

  test("keeps image-only continuation and canonical Markdown through edit/save/reopen", async ({
    page,
    request,
  }) => {
    const imageFixture = await fixture(request);
    const startUrl = `${imageFixture.identity!.start_url}?view=edit`;
    const canonicalMarkdown = [
      "![Available image](/api/assets/123e4567-e89b-42d3-a456-426614174000)",
      "",
      "Continue below",
    ].join("\n");
    await page.goto(startUrl);

    await page.getByRole("button", { name: "Source", exact: true }).click();
    const source = page.getByRole("textbox", { name: "Document body (markdown)" });
    await expect(source).toContainText("/api/assets/123e4567-e89b-42d3-a456-426614174000");
    await expect(source).not.toContainText("blob:");
    await source.fill(canonicalMarkdown);
    await page.getByRole("button", { name: "WYSIWYG", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Document body (markdown)" })).toContainText(
      "Continue below",
    );
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByRole("button", { name: "Save changes" })).toHaveCount(0);

    const stateResponse = await request.get(imageFixture.operations!.state.url);
    expect(stateResponse.ok()).toBeTruthy();
    const state = await stateResponse.json();
    expect(state.document.content).toBe(canonicalMarkdown);

    await page.getByRole("button", { name: "Edit" }).click();
    await page.getByRole("button", { name: "Source", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Document body (markdown)" })).toHaveValue(
      canonicalMarkdown,
    );
  });
});
