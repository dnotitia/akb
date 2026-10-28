import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP fixtures, never live Vault data.");

async function fixture(page: Page, dark: boolean, mime = "text/plain", raw = "Readable source") {
  await page.addInitScript(dark => {
    localStorage.setItem("akb_token", "file-lifecycle-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, dark);
  await page.route("**/file-preview-fixture/**", route => route.fulfill({ contentType: mime, body: raw }));
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "file-fixture", username: "reader", email: "reader@example.invalid", is_admin: false, auth_method: "local" } });
    if (path.endsWith("/my/vaults")) return route.fulfill({ json: { vaults: [{ id: "fixture", name: "fixture", role: "reader" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: { name: "fixture", role: "reader", role_source: "member", public_access: "none", is_archived: false, is_external_git: false } });
    if (path.endsWith("/files/fixture/f-reading")) return route.fulfill({ json: { kind: "file", uri: "akb://fixture/file/f-reading", name: "Reading file", mime_type: mime, size_bytes: raw.length, description: "Original source, unchanged", created_by: "Reader fixture", created_at: "2026-09-01T00:00:00Z" } });
    if (path.endsWith("/files/fixture/f-reading/download")) return route.fulfill({ json: { download_url: "/file-preview-fixture/source", expires_in: 60 } });
    return route.fulfill({ json: { items: [], vaults: [], history: [], relations: [], unread_count: 0, subscribed: false } });
  });
}

for (const width of [375, 1440]) for (const dark of [false, true]) {
  test(`File inspector overlays the unchanged reader canvas at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 812 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await fixture(page, dark);
    await page.goto("/vault/fixture/file/f-reading");
    const preview = page.locator('section[aria-label="File preview"]');
    await expect(page.getByText("Readable source", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Download file" })).toBeInViewport();
    await expect(page.getByRole("button", { name: /Actions for/ })).toHaveCount(0);
    const before = await preview.boundingBox();
    const info = page.getByRole("button", { name: "Info", exact: true });
    await info.click();
    const inspector = page.getByRole("dialog", { name: "File info" });
    await expect(inspector).toBeInViewport({ ratio: 1 });
    await expect(inspector.getByText("text/plain", { exact: true })).toBeVisible();
    expect(await preview.boundingBox()).toEqual(before);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("file-info.png") });
    await page.keyboard.press("Escape");
    await expect(info).toBeFocused();
  });
}

test("an expired link can be refreshed, and authenticated denial clears File identity", async ({ page }) => {
  await fixture(page, true);
  let phase = 1;
  await page.route("**/api/v1/files/fixture/f-reading/download", route => {
    return phase === 3 ? route.fulfill({ status: 403, json: { detail: "Forbidden" } })
      : route.fulfill({ json: { download_url: `/file-preview-fixture/source-${phase}`, expires_in: phase === 1 ? 0 : 60 } });
  });
  await page.goto("/vault/fixture/file/f-reading");
  await expect(page.getByText(/file link has expired/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Download file" })).toBeDisabled();
  phase = 2;
  await page.getByRole("button", { name: "Refresh link", exact: true }).first().click();
  await expect(page.getByRole("link", { name: "Download file" })).toHaveAttribute("href", "/file-preview-fixture/source-2");
  await expect(page.getByText("Readable source", { exact: true })).toBeVisible();
  phase = 3;
  await page.getByRole("button", { name: "Refresh link", exact: true }).click();
  await expect(page.getByText("You no longer have permission to read this file.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Info", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Download file" })).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "Resource location", exact: true })).toHaveCount(0);
});

test("large JSON is download-only and smaller JSON expands by one page", async ({ page }) => {
  await fixture(page, false, "application/json", JSON.stringify(Array.from({ length: 1000 }, (_, i) => `entry-${i}`)));
  await page.goto("/vault/fixture/file/f-reading");
  await expect(page.getByText('"entry-99"', { exact: true })).toBeVisible();
  await expect(page.getByText('"entry-100"', { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Show 100 more (900 remaining)" }).click();
  await expect(page.getByText('"entry-199"', { exact: true })).toBeVisible();
  await expect(page.getByText('"entry-200"', { exact: true })).toHaveCount(0);
  await page.route("**/file-preview-fixture/**", route => route.fulfill({ contentType: "application/json", body: JSON.stringify({ huge: "한".repeat(200_000) }) }));
  await page.route("**/api/v1/files/fixture/f-reading/download", route => route.fulfill({ json: { download_url: "/file-preview-fixture/renewed-source", expires_in: 60 } }));
  await page.getByRole("button", { name: "Refresh link", exact: true }).click();
  await expect(page.getByRole("button", { name: "Refresh link", exact: true })).toBeEnabled();
  await expect(page.getByText('"entry-199"', { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Reload preview", exact: true }).click();
  await expect(page.getByText(/too large for an inline preview/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Download file" })).toBeVisible();
  await expect(page.getByRole("region", { name: "JSON file content" })).toHaveCount(0);
});

async function uploadFixture(page: Page) {
  await fixture(page, false);
  await page.route("**/api/v1/vaults/fixture/info", route => route.fulfill({ json: {
    name: "fixture", role: "writer", role_source: "member", public_access: "none",
    is_archived: false, is_external_git: false, document_count: 1, collection_count: 0,
    table_count: 0, file_count: 0, member_count: 1,
  } }));
  let finishProof = () => {};
  let heldProof: Promise<void> | null = null;
  await page.route("**/api/v1/auth/me", async route => {
    if (heldProof) await heldProof;
    await route.fulfill({ json: { user_id: "file-fixture", username: "reader", email: "reader@example.invalid", is_admin: false, auth_method: "local" } });
  });
  return {
    async check() {
      heldProof = new Promise(resolve => { finishProof = resolve; });
      const requested = page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/auth/me"));
      await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      await requested;
      await expect(page.getByRole("dialog", { name: "Upload a file" })).toHaveCount(0);
    },
    finish() { heldProof = null; finishProof(); },
  };
}

test("native-picker foreground proof preserves the chosen File and upload fields", async ({ page }) => {
  const proof = await uploadFixture(page);
  await page.goto("/vault/fixture");
  await page.getByRole("button", { name: "Add content", exact: true }).click();
  await page.getByRole("menuitem", { name: "Upload file", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Upload a file" });
  const picker = page.getByLabel(/^File/);
  const pickerId = await picker.getAttribute("id");
  await dialog.getByLabel(/^Collection/).fill("draft/collection");
  await dialog.getByLabel(/^Description/).fill("Keep my source");
  const choosing = page.waitForEvent("filechooser");
  await dialog.getByRole("button", { name: /Choose a file or drop/ }).click();
  const chooser = await choosing;
  await proof.check();
  await expect(page.locator(`[id="${pickerId}"]`)).toHaveCount(1);
  await chooser.setFiles({ name: "report.txt", mimeType: "text/plain", buffer: Buffer.from("saved") });
  proof.finish();
  await expect(dialog.getByText("report.txt", { exact: true })).toBeVisible();
  await expect(dialog.getByLabel(/^Collection/)).toHaveValue("draft/collection");
  await expect(dialog.getByLabel(/^Description/)).toHaveValue("Keep my source");
  await expect(dialog.getByRole("button", { name: "Upload file", exact: true })).toBeEnabled();
});

for (const outcome of ["pending", "uncertain"] as const) {
  test(`same-account proof preserves ${outcome} upload and never reserves a duplicate`, async ({ page }) => {
    const proof = await uploadFixture(page);
    let reservations = 0;
    let confirmations = 0;
    let uncertain = outcome === "uncertain";
    let releaseConfirmation = () => {};
    const heldConfirmation = new Promise<void>(resolve => { releaseConfirmation = resolve; });
    const saved = { kind: "file", uri: "akb://fixture/file/saved-upload", vault: "fixture", name: "report.txt", mime_type: "text/plain", size_bytes: 5 };
    await page.route("**/api/v1/files/fixture/upload?*", route => {
      reservations += 1;
      return route.fulfill({ json: { uri: saved.uri, upload_url: "/file-preview-fixture/upload" } });
    });
    await page.route("**/file-preview-fixture/upload", route => route.fulfill({ status: 200 }));
    await page.route("**/api/v1/files/fixture/saved-upload/confirm", async route => {
      confirmations += 1;
      if (outcome === "pending") await heldConfirmation;
      await route.fulfill(uncertain ? { status: 503, json: { detail: "Response unavailable" } } : { json: saved });
    });
    await page.route("**/api/v1/files/fixture/saved-upload", route => route.fulfill(uncertain ? { status: 404 } : { json: saved }));
    await page.route("**/api/v1/files/fixture/saved-upload/download", route => route.fulfill({ json: { download_url: "/file-preview-fixture/source", expires_in: 60 } }));
    await page.goto("/vault/fixture");
    await page.getByRole("button", { name: "Add content", exact: true }).click();
    await page.getByRole("menuitem", { name: "Upload file", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Upload a file" });
    await page.getByLabel(/^File/).setInputFiles({ name: "report.txt", mimeType: "text/plain", buffer: Buffer.from("saved") });
    await dialog.getByRole("button", { name: "Upload file", exact: true }).click();
    if (outcome === "uncertain") await expect(dialog.getByRole("button", { name: "Check upload" })).toBeVisible();
    else await expect.poll(() => confirmations).toBe(1);
    await proof.check();
    if (outcome === "pending") {
      releaseConfirmation();
      // Settle the original request while proof still hides the dialog/job.
      await page.waitForResponse(response => response.url().endsWith("/saved-upload/confirm"));
    }
    expect(reservations).toBe(1);
    proof.finish();
    if (outcome === "uncertain") {
      await expect(dialog.getByText("report.txt", { exact: true })).toBeVisible();
      uncertain = false;
      await dialog.getByRole("button", { name: "Check upload" }).click();
    }
    await expect(page).toHaveURL(/\/vault\/fixture\/file\/saved-upload$/);
    expect(reservations).toBe(1);
    expect(confirmations).toBe(outcome === "pending" ? 1 : 3);
  });
}
