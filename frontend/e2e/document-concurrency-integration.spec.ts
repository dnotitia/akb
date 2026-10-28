import { randomUUID } from "node:crypto";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

test.describe("concurrency recovery in the reading workspace", () => {
  test.skip(process.env.AKB_FE_E2E_MODE !== "real", "Requires an isolated real API.");
  const cleanup: { vault: string; headers: { Authorization: string } }[] = [];

  test.afterEach(async ({ request }) => {
    for (const fixture of cleanup.splice(0)) {
      expect((await request.delete("/api/v1/vaults/" + fixture.vault, { headers: fixture.headers })).ok()).toBeTruthy();
    }
  });

  async function setup(page: Page, request: APIRequestContext) {
    const vault = "integration-" + randomUUID().slice(0, 12);
    const password = randomUUID() + "Aa!";
    expect((await request.post("/api/v1/auth/register", { data: {
      username: vault, email: vault + "@example.test", password,
    } })).ok()).toBeTruthy();
    const login = await request.post("/api/v1/auth/login", { data: { username: vault, password } });
    expect(login.ok()).toBeTruthy();
    const { token } = await login.json();
    const headers = { Authorization: "Bearer " + token };
    expect((await request.post("/api/v1/vaults?name=" + vault, { headers })).ok()).toBeTruthy();
    cleanup.push({ vault, headers });
    const documents = [];
    for (const [slug, title] of [["first", "First integration note"], ["second", "Second integration note"]]) {
      const created = await request.post("/api/v1/documents", { headers, data: {
        vault, collection: "notes", slug, title, content: "Base line\n",
      } });
      expect(created.ok()).toBeTruthy();
      documents.push(await created.json());
    }
    await page.addInitScript((value: string) => localStorage.setItem("akb_token", value), token);
    const href = (path: string) => "/vault/" + vault + "/doc/" + encodeURIComponent(path);
    const api = (path: string) => "/api/v1/documents/" + vault + "/" + encodeURIComponent(path);
    await page.goto(href(documents[0].path));
    await expect(page.getByRole("tab", { name: "First integration note", exact: true })).toBeVisible();
    return { vault, headers, documents, href, api };
  }

  test("preserves a reviewed draft and its revision through tab guards and a confirmed departure", async ({ page, request }) => {
    const fixture = await setup(page, request);
    // Open the second document through the app so both tabs share one
    // mounted workspace. A full-page navigation would discard that state.
    await page.getByRole("navigation", { name: "Resource location" }).getByRole("link", { name: "notes", exact: true }).click();
    await page.locator(`a[href="${fixture.href(fixture.documents[1].path)}"]`).first().click();
    await expect(page.getByRole("tab", { name: "Second integration note", exact: true })).toBeVisible();
    await page.getByRole("tab", { name: "First integration note", exact: true }).click();
    await expect(page.getByRole("tab", { name: "First integration note", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { name: "First integration note", exact: true }).first()).toBeVisible();
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await page.getByRole("button", { name: /^Editor mode:/ }).click();
    await page.getByRole("menuitemradio", { name: "Markdown", exact: true }).click();
    const source = page.getByRole("textbox", { name: "Document body (markdown)" });
    await source.fill("Base line\nLocal tab edit\n");
    const changed = await request.patch(fixture.api(fixture.documents[0].path), { headers: fixture.headers, data: {
      content: "Base line\nRemote tab edit\n", expected_commit: fixture.documents[0].current_commit,
    } });
    expect(changed.ok()).toBeTruthy();
    const remote = await changed.json();
    await page.getByRole("button", { name: "Check for changes" }).click();
    await page.getByRole("button", { name: "Compare changes" }).click();
    const composed = "Base line\nRemote tab edit\nLocal tab edit\n";
    await page.getByRole("textbox", { name: "Combined document body" }).fill(composed);
    await page.getByRole("button", { name: "Apply combined changes" }).click();
    await page.getByRole("button", { name: /^Editor mode:/ }).click();
    await page.getByRole("menuitemradio", { name: "Markdown", exact: true }).click();

    await page.getByRole("tab", { name: "Second integration note", exact: true }).click();
    const leave = page.getByRole("dialog", { name: "Leave this document?" });
    await expect(leave).toBeVisible();
    await leave.getByRole("button", { name: "Keep editing", exact: true }).click();
    await expect(source).toHaveValue(composed);
    await expect(page.getByRole("tab", { name: "First integration note", exact: true })).toHaveAttribute("aria-selected", "true");

    await page.getByRole("button", { name: "Close First integration note", exact: true }).click();
    await expect(leave).toBeVisible();
    await leave.getByRole("button", { name: "Keep editing", exact: true }).click();
    await expect(source).toHaveValue(composed);
    await expect(page.getByRole("button", { name: "Close First integration note", exact: true })).toBeVisible();

    await page.getByRole("tab", { name: "Second integration note", exact: true }).click();
    await leave.getByRole("button", { name: "Leave document", exact: true }).click();
    await expect(page.getByRole("tab", { name: "Second integration note", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("tab", { name: "First integration note", exact: true }).click();
    await expect(page.getByText("Local draft restored", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: /^Editor mode:/ }).click();
    await page.getByRole("menuitemradio", { name: "Markdown", exact: true }).click();
    await expect(source).toHaveValue(composed);
    const receipt = page.waitForResponse(response => response.request().method() === "PATCH" && new URL(response.url()).pathname === fixture.api(fixture.documents[0].path));
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    const saved = await receipt;
    expect(saved.ok()).toBeTruthy();
    expect(saved.request().postDataJSON().expected_commit).toBe(remote.current_commit);
    const final = await request.get(fixture.api(fixture.documents[0].path), { headers: fixture.headers });
    expect((await final.json()).content.trim()).toBe(composed.trim());
  });

  test("retains a Guide draft and reviewed revision through its route-backed preview", async ({ page, request }) => {
    const fixture = await setup(page, request);
    const guidePath = fixture.api("overview/vault-skill.md");
    const initial = await request.get(guidePath, { headers: fixture.headers });
    const guide = await initial.json();
    await page.goto("/vault/" + fixture.vault + "/settings#skill");
    await page.getByRole("button", { name: "Edit guide", exact: true }).click();
    const source = page.getByRole("textbox", { name: "Vault guide body" });
    await source.fill("My local guide edit\n");
    await page.getByRole("link", { name: "Open guide", exact: true }).click();
    const preview = page.getByRole("dialog", { name: "Document preview", exact: true });
    await expect(preview).toBeVisible();
    await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
    await expect(source).toHaveValue("My local guide edit\n");

    const changed = await request.patch(guidePath, { headers: fixture.headers, data: {
      content: "Remote guide edit\n", expected_commit: guide.current_commit,
    } });
    expect(changed.ok()).toBeTruthy();
    const remote = await changed.json();
    await page.getByRole("button", { name: "Check for changes", exact: true }).click();
    await page.getByRole("button", { name: "Compare changes", exact: true }).click();
    const composed = "Remote guide edit\nMy reviewed guide edit\n";
    await page.getByRole("textbox", { name: "Combined document body" }).fill(composed);
    const viewport = page.viewportSize();
    await page.setViewportSize({ width: 375, height: 812 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.screenshot({ path: test.info().outputPath("comparison-narrow-light.png"), fullPage: true, animations: "disabled" });
    await page.emulateMedia({ colorScheme: "dark" });
    await expect(page.locator("html")).toHaveClass(/dark/);
    await page.screenshot({ path: test.info().outputPath("comparison-narrow-dark.png"), fullPage: true, animations: "disabled" });
    const darkControls = await Promise.all([
      page.getByRole("button", { name: "Copy Original base markdown", exact: true }),
      page.getByRole("button", { name: "Reload comparison", exact: true }),
    ].map(control => control.evaluate(element => {
      const style = getComputedStyle(element);
      return { label: element.getAttribute("aria-label") || element.textContent?.trim(), color: style.color, background: style.backgroundColor };
    })));
    await test.info().attach("dark-comparison-controls", { body: Buffer.from(JSON.stringify(darkControls)), contentType: "application/json" });
    await page.emulateMedia({ colorScheme: "light" });
    if (viewport) await page.setViewportSize(viewport);
    await page.getByRole("button", { name: "Apply combined changes", exact: true }).click();
    await expect(source).toHaveValue(composed);
    // The comparison is modal. Preview is opened only after applying its
    // reviewed result to the editor, without saving or changing that base.
    const beforePreview = await request.get(guidePath, { headers: fixture.headers });
    const serverGuide = await beforePreview.json();
    expect(serverGuide.current_commit).toBe(remote.current_commit);
    expect(serverGuide.content.trim()).toBe("Remote guide edit");
    await page.getByRole("link", { name: "Open guide", exact: true }).click();
    await expect(preview).toBeVisible();
    // The read preview can retain its ordinary cached server snapshot. It
    // must never publish the unsaved combined result from the editor.
    await expect(preview.getByRole("region", { name: "Document content" })).not.toContainText("My reviewed guide edit");
    const identity = page.waitForResponse(response => response.url().endsWith("/api/v1/auth/me") && response.ok());
    const role = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/vaults/" + fixture.vault + "/info" && response.ok());
    await page.evaluate(() => window.dispatchEvent(new Event("akb:revalidate-access")));
    await Promise.all([identity, role]);
    await expect(preview.getByRole("region", { name: "Document content" })).toContainText("Remote guide edit");
    await expect(preview.getByRole("region", { name: "Document content" })).not.toContainText("My reviewed guide edit");
    await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
    await expect(source).toHaveValue(composed);
    const receipt = page.waitForResponse(response => response.request().method() === "PATCH" && new URL(response.url()).pathname === guidePath);
    await page.getByRole("button", { name: "Save guide", exact: true }).click();
    const saved = await receipt;
    expect(saved.ok()).toBeTruthy();
    expect(saved.request().postDataJSON().expected_commit).toBe(remote.current_commit);
    const final = await request.get(guidePath, { headers: fixture.headers });
    expect((await final.json()).content.trim()).toBe(composed.trim());
  });
});
