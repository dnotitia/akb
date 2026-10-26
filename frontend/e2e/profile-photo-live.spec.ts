import { randomBytes } from "node:crypto";
import { expect, test } from "@playwright/test";

// Creates and deletes only its own account. Never opt in against shared data.
test("profile photo persists through real storage, reload and local reauthentication", async ({ page, request, baseURL }, info) => {
  test.skip(process.env.AKB_FE_E2E_MODE !== "real" || process.env.AKB_PROFILE_PHOTO_LIVE !== "1", "Requires an explicitly owned isolated local runtime.");
  expect(["127.0.0.1", "localhost"]).toContain(new URL(baseURL!).hostname);
  test.setTimeout(90_000);
  const username = `photo-e2e-${randomBytes(6).toString("hex")}`;
  const password = `${randomBytes(20).toString("base64url")}A1!`;
  const endpoint = "/api/v1";
  // A real PNG with valid chunk checksums, not just a browser-decodable preview.
  const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGNkcIhkYGBgYgADAAb9AJ0XD0UiAAAAAElFTkSuQmCC", "base64");
  const bearer = (token: string) => ({ Authorization: `Bearer ${token}` });
  const login = async () => {
    const response = await request.post(`${endpoint}/auth/login`, { data: { username, password } });
    expect(response.ok()).toBeTruthy(); return (await response.json()).token as string;
  };
  const registered = await request.post(`${endpoint}/auth/register`, { data: { username, password, email: `${username}@example.invalid`, display_name: "Profile photo QA" } });
  expect(registered.ok()).toBeTruthy();
  let userId = "";
  try {
    const token = await login();
    const identity = await request.get(`${endpoint}/auth/me`, { headers: bearer(token) });
    const user = await identity.json(); userId = user.user_id;
    expect(user.profile_capabilities.avatar.can_upload).toBe(true);
    expect(user.avatar).toEqual({ revision: null, url: null });
    await page.goto("/auth"); await page.evaluate(token => localStorage.setItem("akb_token", token), token);
    await page.goto("/settings");
    const dialog = page.getByRole("dialog", { name: "Settings", exact: true });
    const pickPhoto = async (withForegroundProof = false) => {
      const pickerEvent = page.waitForEvent("filechooser");
      await dialog.getByRole("button", { name: /^(Upload photo|Change photo|Choose another)$/ }).click();
      const picker = await pickerEvent;
      if (withForegroundProof) {
        let release!: () => void;
        let reached!: () => void;
        const pending = new Promise<void>(resolve => { release = resolve; });
        const started = new Promise<void>(resolve => { reached = resolve; });
        await page.route("**/api/v1/auth/me", async route => {
          reached();
          await pending;
          await route.continue();
        }, { times: 1 });
        try {
          await page.evaluate(() => window.dispatchEvent(new Event("focus")));
          await started;
          await expect(dialog.getByRole("button", { name: "Upload photo", exact: true })).not.toBeVisible();
          await picker.setFiles({ name: "owned-portrait.png", mimeType: "image/png", buffer: png });
          await expect(dialog.getByRole("button", { name: "Save photo", exact: true })).not.toBeVisible();
        } finally { release(); }
      } else await picker.setFiles({ name: "owned-portrait.png", mimeType: "image/png", buffer: png });
      await expect(dialog.getByRole("img", { name: "Selected profile photo preview" })).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Save photo" })).toBeEnabled();
    };
    const select = async (withForegroundProof = false) => {
      await pickPhoto(withForegroundProof);
      await dialog.getByRole("button", { name: "Save photo" }).click();
      await expect(dialog.getByRole("button", { name: "Change photo" })).toBeVisible();
      await expect(dialog.locator('[data-slot="user-avatar"] img')).toHaveCount(1);
    };
    await select(true);
    const saved = await (await request.get(`${endpoint}/auth/me`, { headers: bearer(token) })).json();
    const firstRevision = saved.avatar.revision;
    const bytes = await request.get(`${endpoint}${saved.avatar.url.replace(endpoint, "")}`, { headers: bearer(token) });
    expect(bytes.status()).toBe(200); expect(bytes.headers()["content-type"]).toBe("image/webp");
    expect(bytes.headers()["cache-control"]).toBe("private, no-store");
    expect((await bytes.body()).length).toBeLessThanOrEqual(256 * 1024);
    expect((await request.get(`${endpoint}/auth/me/avatar?v=${firstRevision}`)).status()).toBe(401);
    const patResponse = await request.post(`${endpoint}/auth/tokens`, { headers: bearer(token), data: { name: "owned-avatar-boundary-test" } });
    expect(patResponse.ok()).toBeTruthy();
    expect((await request.get(`${endpoint}/auth/me/avatar?v=${firstRevision}`, { headers: bearer((await patResponse.json()).token) })).status()).toBe(403);
    await page.reload(); await expect(dialog.locator('[data-slot="user-avatar"] img')).toHaveCount(1);

    // A committed replacement may lose only its HTTP acknowledgement. A retry
    // must conflict, not overwrite, and failed recovery must keep the fence.
    await dialog.getByLabel("Display name", { exact: true }).fill("Unrelated profile draft");
    await pickPhoto();
    await expect(dialog.getByRole("button", { name: "Save photo", exact: true })).toBeEnabled();
    let loseAcknowledgement = true;
    await page.route("**/api/v1/auth/me/avatar?**", async route => {
      if (route.request().method() === "PUT" && loseAcknowledgement) {
        const committed = await route.fetch();
        expect(committed.status()).toBe(200);
        loseAcknowledgement = false;
        await route.abort("failed");
      } else await route.continue();
    });
    await dialog.getByRole("button", { name: "Save photo", exact: true }).click();
    await expect(dialog.getByRole("alert")).toBeVisible();
    await expect(dialog.getByRole("img", { name: "Selected profile photo preview" })).toBeVisible();
    const committedRevision = (await (await request.get(`${endpoint}/auth/me`, { headers: bearer(token) })).json()).avatar.revision;
    expect(committedRevision).not.toBe(firstRevision);
    await dialog.getByRole("button", { name: "Save photo", exact: true }).click();
    await expect(dialog.getByRole("button", { name: "Save photo", exact: true })).toBeDisabled();
    await expect(dialog.getByRole("button", { name: "Reload photo", exact: true })).toBeVisible();
    await dialog.getByRole("button", { name: "Dismiss photo error", exact: true }).click();
    let loseReload = true;
    await page.route("**/api/v1/auth/me", async route => {
      if (loseReload) { loseReload = false; await route.abort("failed"); }
      else await route.continue();
    });
    await dialog.getByRole("button", { name: "Reload photo", exact: true }).click();
    await expect(dialog.getByRole("alert")).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Save photo", exact: true })).toBeDisabled();
    await expect(dialog.getByRole("button", { name: "Reload photo", exact: true })).toBeEnabled();
    await dialog.getByRole("button", { name: "Reload photo", exact: true }).click();
    await expect(dialog.getByRole("button", { name: "Change photo", exact: true })).toBeVisible();
    await expect(dialog.getByLabel("Display name", { exact: true })).toHaveValue("Unrelated profile draft");
    expect((await (await request.get(`${endpoint}/auth/me`, { headers: bearer(token) })).json()).avatar.revision).toBe(committedRevision);
    await dialog.getByLabel("Display name", { exact: true }).fill("Profile photo QA");
    await select();
    expect((await request.get(`${endpoint}/auth/me/avatar?v=${firstRevision}`, { headers: bearer(token) })).status()).toBe(404);
    await dialog.getByRole("button", { name: "Remove photo", exact: true }).click();
    await dialog.getByRole("group", { name: "Remove profile photo?" }).getByRole("button", { name: "Remove photo", exact: true }).click();
    await expect(dialog.getByRole("button", { name: "Upload photo" })).toBeVisible();
    const removed = await (await request.get(`${endpoint}/auth/me`, { headers: bearer(token) })).json();
    expect(removed.avatar.url).toBeNull(); expect(removed.avatar.revision).not.toBeNull();
    await select();
    await page.screenshot({ path: info.outputPath("profile-photo-real-storage.png") });
    await dialog.getByRole("button", { name: "Close settings", exact: true }).click();
    await page.locator("#account-menu-trigger").click(); await page.getByRole("menuitem", { name: "Sign out", exact: true }).click();
    await expect(page).toHaveURL(/\/auth/);
    await page.getByLabel("Username or email", { exact: true }).fill(username);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.locator('#account-menu-trigger [data-slot="user-avatar"] img')).toHaveCount(1);
    await page.goto("/settings"); await expect(dialog.getByRole("button", { name: "Change photo" })).toBeVisible();
  } finally {
    const fresh = await login();
    if (!userId) userId = (await (await request.get(`${endpoint}/auth/me`, { headers: bearer(fresh) })).json()).user_id;
    const removed = await request.post(`${endpoint}/my/account/deletion`, { headers: bearer(fresh), data: { expected_user_id: userId, confirm_username: username, current_password: password } });
    expect(removed.ok(), "Owned profile-photo account cleanup").toBeTruthy();
  }
});
