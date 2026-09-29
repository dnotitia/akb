import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");

const path = "guides/large.md";
const body = Array.from({ length: 500 }, (_, i) => `## Section ${i + 1}\n\nA practical guide to shared knowledge. This paragraph includes **important details** and a checklist for the team.\n\n- Verify inputs\n- Review changes`).join("\n\n");

async function fixture(page: Page) {
  await page.addInitScript(() => localStorage.setItem("akb_token", "editor-performance-fixture"));
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url()).pathname;
    if (url.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (url.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "perf-user", username: "fixture", display_name: "Editor reviewer", email: "editor@example.invalid", auth_method: "local", is_admin: false } });
    if (url.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ name: "fixture", role: "writer" }] } });
    if (url.endsWith("/info")) return route.fulfill({ json: { name: "fixture", role: "writer", is_archived: false, is_external_git: false } });
    if (url.includes("/browse/")) return route.fulfill({ json: { items: [{ type: "collection", path: "guides", name: "Guides" }, { type: "document", path, name: "Large document" }] } });
    if (url.includes("/documents/fixture/") && !url.endsWith("/history")) return route.fulfill({ json: { uri: "akb://fixture/coll/guides/doc/large.md", path, title: "Large document", content: body, status: "active", current_commit: "aaaaaaaaaaaaa", tags: [], is_public: false } });
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
}

test("long document typing, mode switching and immediate save retain every character", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  await fixture(page);
  await page.goto(`/vault/fixture/doc/${encodeURIComponent(path)}`);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  const editor = page.getByRole("textbox", { name: "Document body (markdown)", exact: true });
  await expect(editor).toBeVisible({ timeout: 45_000 });
  await editor.focus();
  await page.keyboard.press("ControlOrMeta+Home");
  const session = await page.context().newCDPSession(page);
  await session.send("Profiler.enable");
  await session.send("Profiler.start");
  const start = Date.now();
  await page.keyboard.type("Typed");
  const elapsed = Date.now() - start;
  const { profile } = await session.send("Profiler.stop");
  console.log("PROFILE_TOP", profile.nodes.filter(node => node.hitCount).sort((a, b) => (b.hitCount ?? 0) - (a.hitCount ?? 0)).slice(0, 12).map(node => [node.callFrame.functionName, node.hitCount]));
  await testInfo.attach("typing-profile", { body: JSON.stringify(profile), contentType: "application/json" });
  await testInfo.attach("typing-timing", { body: JSON.stringify({ milliseconds: elapsed, sourceBytes: new TextEncoder().encode(body).length }), contentType: "application/json" });
  console.log(`LONG_DOCUMENT_TYPING ${elapsed} ms, ${body.length} characters`);
  // Deliberately loose: catches the former multi-second-per-key regression,
  // not variations in CI hardware or a product performance SLA.
  expect(elapsed).toBeLessThan(5_000);
  await expect(editor).toContainText("Typed");
  await page.keyboard.type(" continuously without dropped text.");
  await page.getByRole("button", { name: "Editor mode: Visual" }).click();
  await page.getByRole("menuitemradio", { name: "Markdown", exact: true }).click();
  await expect(editor).toHaveValue(/Typed continuously without dropped text\./);
  await editor.press("ControlOrMeta+End");
  await page.keyboard.type("\n\nSource edit retained.");
  await page.getByRole("button", { name: "Editor mode: Markdown" }).click();
  await page.getByRole("menuitemradio", { name: "Visual", exact: true }).click();
  await expect(editor).toContainText("Source edit retained.");
  const save = page.waitForRequest(request => request.method() === "PATCH" && request.url().includes("/documents/fixture/"));
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  const request = await save;
  expect(request.postDataJSON().content).toContain("Typed continuously without dropped text.");
  expect(request.postDataJSON().content).toContain("Section 500");
  expect(request.postDataJSON().content).toContain("Source edit retained.");
});

test("new document accepts a large pasted draft and saves the final typed text", async ({ page }) => {
  test.setTimeout(60_000);
  await fixture(page);
  await page.goto("/vault/fixture/doc/new?collection=guides");
  const composer = page.getByRole("dialog", { name: "New document", exact: true });
  await composer.getByLabel(/^Title/).fill("Large new document");
  await composer.getByRole("button", { name: "Editor mode: Visual" }).click();
  await page.getByRole("menuitemradio", { name: "Markdown", exact: true }).click();
  await composer.getByRole("textbox", { name: "Content (required)" }).fill(body);
  await composer.getByRole("button", { name: "Editor mode: Markdown" }).click();
  await page.getByRole("menuitemradio", { name: "Visual", exact: true }).click();
  const editor = composer.locator('[contenteditable="true"]');
  await editor.focus();
  await page.keyboard.press("ControlOrMeta+Home");
  const start = Date.now();
  await page.keyboard.type("Fresh");
  const elapsed = Date.now() - start;
  console.log(`LONG_NEW_DOCUMENT_TYPING ${elapsed} ms, ${body.length} characters`);
  expect(elapsed).toBeLessThan(5_000);
  const saved = page.waitForRequest(request => request.method() === "POST" && /\/documents\/?$/.test(new URL(request.url()).pathname));
  await composer.getByRole("button", { name: "Create document", exact: true }).click();
  const payload = (await saved).postDataJSON();
  expect(payload.title).toBe("Large new document");
  expect(payload.collection).toBe("guides");
  expect(payload.content).toContain("Fresh");
  expect(payload.content).toContain("Section 500");
});
