import { expect, test, type APIRequestContext } from "@playwright/test";

type MockOperation = {
  method: "GET" | "POST";
  url: string;
  body?: Record<string, unknown>;
};

type MockDescriptor = {
  scenario?: string;
  mock?: {
    fixture?: {
      identity?: { start_url?: string };
      operations?: Record<string, MockOperation>;
    } | null;
  };
};

test.describe("document edit recovery mock contract", () => {
  test.skip(
    process.env.CRABBOX_RUNTIME_SCENARIO !== "document-edit-recovery",
    "The recovery fixture is selected by CRABBOX_RUNTIME_SCENARIO",
  );
  test.describe.configure({ mode: "serial" });

  async function fixture(request: APIRequestContext) {
    const response = await request.get("/__akb_mock__/discover");
    expect(response.ok()).toBeTruthy();
    const descriptor = (await response.json()) as MockDescriptor;
    expect(descriptor.scenario).toBe("document-edit-recovery");
    const recovery = descriptor.mock?.fixture;
    expect(recovery?.identity?.start_url).toBeTruthy();
    expect(recovery?.operations).toBeTruthy();
    return recovery!;
  }

  async function reset(request: APIRequestContext) {
    const response = await request.post("/__akb_mock__/reset", {
      data: { scenario: "document-edit-recovery" },
    });
    expect(response.ok()).toBeTruthy();
  }

  async function operate(
    request: APIRequestContext,
    operation: MockOperation,
    body: Record<string, unknown> = {},
  ) {
    const response = operation.method === "GET"
      ? await request.get(operation.url)
      : await request.post(operation.url, {
          data: { ...(operation.body || {}), ...body },
        });
    expect(response.ok()).toBeTruthy();
    return response.json();
  }

  test.beforeEach(async ({ request }) => {
    await reset(request);
  });

  test("keeps a dirty body through refetch and exposes a three-way OCC conflict", async ({
    page,
    request,
  }) => {
    const recovery = await fixture(request);
    await page.goto(recovery.identity!.start_url!);
    const editor = page.getByRole("textbox", { name: "Document body (markdown)" });
    await expect(editor).toBeVisible();
    await editor.fill("Local draft before another editor saves");

    await operate(request, recovery.operations!.remote_revision, {
      actor: "editor-b",
      title: "Remote revision",
      content: "Remote editor body",
    });
    await operate(request, recovery.operations!.refetch);
    await expect(editor).toContainText("Local draft before another editor saves");

    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("This document changed on the server")).toBeVisible();
    await expect(page.getByText("Original base", { exact: true })).toBeVisible();
    await expect(page.getByText("Your draft", { exact: true })).toBeVisible();
    await expect(page.getByText("Latest server version", { exact: true })).toBeVisible();

    await page.getByRole("button", { name: "Apply draft to latest" }).click();
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByRole("button", { name: "Save" })).toHaveCount(0);
    await expect(page.getByText("This document changed on the server")).toHaveCount(0);
  });

  test("keeps a failed save draft across reload, retries an image, and claims it on success", async ({
    page,
    request,
  }) => {
    const recovery = await fixture(request);
    await page.goto(recovery.identity!.start_url!);
    const editor = page.getByRole("textbox", { name: "Document body (markdown)" });
    await editor.fill("Draft retained after a retryable server failure");
    await expect(page.getByText("Draft saved locally")).toBeVisible();
    await operate(request, recovery.operations!.retryable_save_failure);
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("The server hit an error while saving. Please retry.")).toBeVisible();

    await page.reload();
    await expect(page.getByText("Local draft restored")).toBeVisible();
    await expect(editor).toContainText("Draft retained after a retryable server failure");

    const input = page.locator('input[type="file"]');
    await operate(request, recovery.operations!.retryable_upload_failure);
    await input.setInputFiles({
      name: "recovery.png",
      mimeType: "image/png",
      buffer: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
        "base64",
      ),
    });
    await expect(page.getByText("Image upload failed")).toBeVisible();
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByRole("button", { name: /Remove image: recovery/ })).toBeVisible();

    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("This document changed on the server")).toHaveCount(0);
    await expect
      .poll(
        async () => {
          const state = await operate(request, recovery.operations!.state);
          return state.assets.some((asset: { status: string }) => asset.status === "claimed");
        },
        { timeout: 5_000 },
      )
      .toBe(true);
  });

  test("makes expiry and explicit draft discard observable without storage access", async ({
    page,
    request,
  }) => {
    const recovery = await fixture(request);
    await page.goto(recovery.identity!.start_url!);
    await page.getByRole("textbox", { name: "Document body (markdown)" }).fill("Draft that will expire");
    await operate(request, recovery.operations!.expire_draft);
    await page.reload();
    await expect(page.getByText("This draft has expired")).toBeVisible();
    await expect(page.getByRole("button", { name: "Copy expired Markdown" })).toBeVisible();

    await page.getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Discard changes" }).click();
    await expect(page.locator("#doc-title")).toHaveText("Recovery document");
    const state = await operate(request, recovery.operations!.state);
    expect(state.scenario).toBe("document-edit-recovery");
  });
});
