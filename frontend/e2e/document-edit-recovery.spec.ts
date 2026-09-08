import { expect, test, type APIRequestContext, type Locator } from "@playwright/test";

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

  async function expectInstantKeyboardFocus(button: Locator) {
    await button.evaluate((element) => {
      const sentinel = document.createElement("span");
      sentinel.tabIndex = 0;
      sentinel.dataset.focusTestSentinel = "true";
      sentinel.style.cssText = "position:fixed;width:1px;height:1px;opacity:0;";
      element.before(sentinel);
    });
    await button.page().locator("[data-focus-test-sentinel]").last().focus();
    await button.page().keyboard.press("Tab");
    await expect(button).toBeFocused();
    await expect(button).toHaveClass(/focus-ring-instant/);
    const focusStyle = await button.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        focusVisible: element.matches(":focus-visible"),
        transitionDuration: style.transitionDuration,
        transitionProperty: style.transitionProperty,
      };
    });
    expect(focusStyle.focusVisible).toBe(true);
    expect(focusStyle.transitionDuration).toBe("0s");
    expect(focusStyle.transitionProperty).toBe("none");
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
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.addInitScript(() => {
      localStorage.setItem("akb.vaultRailWidth.v2", "320");
      localStorage.setItem("akb.treeWidth.v2", "270");
    });
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
    const conflict = page
      .getByRole("alert")
      .filter({ hasText: "This document changed on the server" });
    await expect(conflict).toBeVisible();
    const snapshots = conflict.locator("[data-conflict-snapshot]");
    await expect(snapshots).toHaveCount(3);
    const snapshotWidths = await snapshots.evaluateAll((elements) =>
      elements.map((element) => Math.round(element.getBoundingClientRect().width)),
    );
    expect(Math.min(...snapshotWidths)).toBeGreaterThanOrEqual(240);
    const metadata = await conflict.locator("[data-conflict-metadata]").evaluateAll((elements) =>
      elements.map((element) => {
        const style = getComputedStyle(element);
        return {
          textOverflow: style.textOverflow,
          whiteSpace: style.whiteSpace,
        };
      }),
    );
    expect(metadata).toHaveLength(9);
    expect(
      metadata.every(
        ({ textOverflow, whiteSpace }) =>
          textOverflow !== "ellipsis" && whiteSpace !== "nowrap",
      ),
    ).toBe(true);
    const actionStyles = await conflict.getByRole("button").evaluateAll((buttons) =>
      buttons.map((button) => getComputedStyle(button).whiteSpace),
    );
    expect(actionStyles.every((whiteSpace) => whiteSpace === "nowrap")).toBe(true);
    for (const width of [320, 634, 1920]) {
      await page.setViewportSize({ width, height: 900 });
      await expect
        .poll(async () =>
          page.evaluate(
            () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
          ),
        )
        .toBe(true);
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await expectInstantKeyboardFocus(
      conflict.getByRole("button", { name: "Apply draft to latest" }),
    );

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
    const draftTitle = "AC7-TITLE-248";
    const markerOne = "AC7-ORIGINAL-ONE-248";
    const markerTwo = "AC7-ORIGINAL-TWO-248";
    const draftBody = `${markerOne}\n\n${markerTwo}\n`;
    const recovery = await fixture(request);
    await page.goto(recovery.identity!.start_url!);
    await page.getByRole("textbox", { name: "Document title" }).fill(draftTitle);
    const editor = page.getByRole("textbox", { name: "Document body (markdown)" });
    await editor.fill(draftBody);
    await expect(page.getByText("Draft saved locally")).toBeVisible();
    await operate(request, recovery.operations!.expire_draft);
    await page.waitForTimeout(500);
    let beforeUnloadAccepted = false;
    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("beforeunload");
      beforeUnloadAccepted = true;
      await dialog.accept();
    });
    await page.reload();
    expect(beforeUnloadAccepted).toBe(true);
    await page.waitForTimeout(3_000);
    await expect(page.getByRole("textbox", { name: "Document title" })).toHaveValue(draftTitle);
    await expect(editor).toContainText(markerOne);
    await expect(editor).toContainText(markerTwo);
    await expect(page.getByText("Local draft restored")).toHaveCount(0);
    await expect(page.getByText("This draft has expired")).toBeVisible();
    await expect(page.getByText(/attached images are not guaranteed/)).toBeVisible();
    const expiredCopy = page.getByRole("button", { name: "Copy expired Markdown" });
    await expect(expiredCopy).toBeVisible();
    await expectInstantKeyboardFocus(expiredCopy);
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"], {
      origin: new URL(page.url()).origin,
    });
    await expiredCopy.click();
    await expect
      .poll(async () => page.evaluate(() => navigator.clipboard.readText()))
      .toBe(draftBody);
    await page.evaluate(() => {
      const target = document.createElement("textarea");
      target.dataset.clipboardPasteTarget = "true";
      target.style.cssText = "position:fixed;left:0;top:0;width:240px;height:40px;";
      document.body.append(target);
    });
    const pasteTarget = page.locator("[data-clipboard-paste-target]");
    await pasteTarget.focus();
    await pasteTarget.press(process.platform === "darwin" ? "Meta+V" : "Control+V");
    await expect(pasteTarget).toHaveValue(draftBody);

    await page.getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Discard changes" }).click();
    await expect(page.locator("#doc-title")).toHaveText("Recovery document");
    await page.getByRole("button", { name: "Edit" }).click();
    await expect(page.getByRole("textbox", { name: "Document title" })).toHaveValue("Recovery document");
    const reopenedEditor = page.getByRole("textbox", { name: "Document body (markdown)" });
    await expect(reopenedEditor).toContainText("The original body is safe to edit.");
    await expect(reopenedEditor).not.toContainText(markerOne);
    await expect(reopenedEditor).not.toContainText(markerTwo);
    await expect(page.getByText("This draft has expired")).toHaveCount(0);
    const state = await operate(request, recovery.operations!.state);
    expect(state.scenario).toBe("document-edit-recovery");
  });
});
