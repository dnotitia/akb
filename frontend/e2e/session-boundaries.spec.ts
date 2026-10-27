import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP fixtures; no backend writes.");

async function fixture(page: Page) {
  await page.addInitScript(() => localStorage.setItem("akb_token", "session-boundary-fixture"));
  await page.route("**/health**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "boundary-a", username: "alice", display_name: "Alice", email: "alice@example.test", auth_method: "local", is_admin: false } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "fixture-vault", name: "fixture", role: "owner" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: { name: "fixture", description: "Fixture vault", role: "owner", role_source: "member", public_access: "none", is_archived: false, is_external_git: false } });
    if (path.includes("/documents/fixture/") && !path.endsWith("/history")) {
      const guide = path.includes("vault-skill");
      return route.fulfill({ json: { uri: guide ? "akb://fixture/coll/overview/doc/vault-skill.md" : "akb://fixture/coll/guides/doc/reading.md", path: guide ? "overview/vault-skill.md" : "guides/reading.md", title: guide ? "Vault guide" : "Operating guide", content: "# Original\n\nOriginal body.", current_commit: "aaaaaaaaaaaaa", status: "active" } });
    }
    if (path.endsWith("/search")) return route.fulfill({ json: { results: [], total: 0 } });
    return route.fulfill({ json: { items: [], history: [], relations: [], tokens: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
}

const settings = (page: Page) => page.getByRole("dialog", { name: "Settings", exact: true });
const discard = (page: Page) => page.getByRole("dialog", { name: "Discard unsaved changes?", exact: true });
async function openSettings(page: Page) {
  await page.getByTestId("app-sidebar").getByRole("button", { name: "Settings", exact: true }).click();
  await expect(settings(page).getByLabel("Display name")).toHaveValue("Alice");
}

test("Settings protects indexed browser Back and Forward, then replays each approved destination once", async ({ page }) => {
  await fixture(page);
  await page.goto("/search?q=prior");
  await page.getByRole("link", { name: "AKB home — Agent Knowledgebase" }).click();
  await expect(page).toHaveURL(/\/$/);
  await openSettings(page);
  await settings(page).getByLabel("Display name").fill("Unsaved profile");
  await page.goBack();
  await expect(discard(page)).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
  await discard(page).getByRole("button", { name: "Keep editing" }).click();
  await expect(settings(page).getByLabel("Display name")).toHaveValue("Unsaved profile");
  await page.goBack();
  await discard(page).getByRole("button", { name: "Discard changes" }).click();
  await expect(page).toHaveURL(/\/search\?q=prior$/);
  await expect(settings(page)).toHaveCount(0);
  await openSettings(page);
  await settings(page).getByLabel("Current password").fill("private password draft");
  await page.goForward();
  await expect(discard(page)).toBeVisible();
  await expect(page).toHaveURL(/\/search\?q=prior$/);
  await discard(page).getByRole("button", { name: "Discard changes" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(settings(page)).toHaveCount(0);
});

test("Guide draft protects a global sidebar link and approved leave does not ask twice", async ({ page }) => {
  await fixture(page);
  await page.goto("/vault/fixture/settings#skill");
  await page.getByRole("button", { name: "Edit guide", exact: true }).click();
  await page.getByRole("textbox", { name: "Vault guide body" }).fill("Keep this guide draft");
  await page.getByRole("link", { name: "AKB home — Agent Knowledgebase" }).click();
  const prompt = page.getByRole("dialog", { name: "Discard unsaved settings changes?" });
  await expect(prompt).toBeVisible();
  await prompt.getByRole("button", { name: "Keep editing" }).click();
  await expect(page.getByRole("textbox", { name: "Vault guide body" })).toHaveValue("Keep this guide draft");
  await page.getByRole("link", { name: "AKB home — Agent Knowledgebase" }).click();
  await prompt.getByRole("button", { name: "Discard changes and leave" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(prompt).toHaveCount(0);
});

test("Password form errors preserve the session and success explains its real revocation", async ({ page }) => {
  await fixture(page);
  let correct = false;
  await page.route("**/auth/change-password", route => route.fulfill(correct ? { json: { ok: true } } : { status: 400, json: { detail: { message: "Current password is incorrect", code: "current_password_incorrect" } } }));
  await page.goto("/settings");
  await settings(page).getByLabel("Current password").fill("wrong");
  await settings(page).getByLabel("New password", { exact: true }).fill("new-password-123");
  await settings(page).getByLabel("Confirm new password").fill("new-password-123");
  await settings(page).getByRole("button", { name: "Change password", exact: true }).click();
  await expect(settings(page).getByLabel("Current password")).toBeFocused();
  await expect(settings(page).getByLabel("Current password")).toHaveAttribute("aria-invalid", "true");
  expect(await page.evaluate(() => localStorage.getItem("akb_token"))).toBe("session-boundary-fixture");
  correct = true;
  await settings(page).getByLabel("Current password").fill("correct");
  await settings(page).getByRole("button", { name: "Change password", exact: true }).click();
  await expect(page).toHaveURL(/\/auth\?reason=password-changed$/);
  await expect(page.getByText(/Your password was changed. Local sign-ins/)).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("akb_token"))).toBeNull();
});

test("document editor global navigation preserves a recoverable draft and resumes once", async ({ page }) => {
  await fixture(page);
  await page.goto("/vault/fixture/doc/guides%2Freading.md?view=edit");
  await page.getByLabel("Document title", { exact: true }).fill("Unsaved title");
  await page.getByRole("link", { name: "AKB home — Agent Knowledgebase" }).click();
  const prompt = page.getByRole("dialog", { name: "Leave this document?" });
  await expect(prompt).toBeVisible();
  await prompt.getByRole("button", { name: "Keep editing" }).click();
  await expect(page.getByLabel("Document title", { exact: true })).toHaveValue("Unsaved title");
  await page.getByRole("link", { name: "AKB home — Agent Knowledgebase" }).click();
  await prompt.getByRole("button", { name: "Leave document" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(prompt).toHaveCount(0);
});

test("nested preview edit protects close, keeps its draft, and approved close returns to search once", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/v1/search**", route => route.fulfill({ json: { total: 1, results: [{ title: "Operating guide", uri: "akb://fixture/coll/guides/doc/reading.md", path: "guides/reading.md", vault: "fixture", source_type: "document", score: 1 }] } }));
  await page.goto("/search?q=guide");
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  const search = page.getByTestId("global-search-dialog");
  await search.getByRole("combobox").fill("guide");
  await search.getByRole("option", { name: /Operating guide/ }).click();
  const preview = page.getByTestId("document-preview-dialog");
  await preview.getByRole("button", { name: "Edit", exact: true }).click();
  await preview.getByLabel("Document title", { exact: true }).fill("Preview draft");
  await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
  const prompt = page.getByRole("dialog", { name: "Leave this document?" });
  await expect(prompt).toBeVisible();
  await prompt.getByRole("button", { name: "Keep editing" }).click();
  await expect(preview.getByLabel("Document title", { exact: true })).toHaveValue("Preview draft");
  await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
  await prompt.getByRole("button", { name: "Leave document" }).click();
  await expect(preview).toHaveCount(0);
  await expect(page).toHaveURL(/\/search\?q=guide$/);
  await expect(search).toBeVisible();
});

test("routed preview shares shell proof, retains same-account edits, and clears them on account switch", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/v1/search**", route => route.fulfill({ json: { total: 1, results: [{ title: "Operating guide", uri: "akb://fixture/coll/guides/doc/reading.md", path: "guides/reading.md", vault: "fixture", source_type: "document", score: 1 }] } }));
  await page.goto("/search?q=guide");
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  const search = page.getByTestId("global-search-dialog");
  await search.getByRole("combobox").fill("guide");
  await search.getByRole("option", { name: /Operating guide/ }).click();
  const preview = page.getByTestId("document-preview-dialog");
  await preview.getByRole("button", { name: "Edit", exact: true }).click();
  const title = preview.getByLabel("Document title", { exact: true });
  await title.fill("Alice private draft");

  let finishProof!: () => Promise<void>;
  let requested!: () => void;
  const proofRequested = new Promise<void>(resolve => { requested = resolve; });
  await page.route("**/api/v1/auth/me", route => {
    finishProof = () => route.fulfill({ json: { user_id: "boundary-a", username: "alice", display_name: "Alice", email: "alice@example.test", auth_method: "local", is_admin: false } });
    requested();
  });
  await page.evaluate(() => window.dispatchEvent(new Event("akb:revalidate-access")));
  await proofRequested;
  await expect(title).toBeHidden();
  await expect(preview).toBeVisible();
  await page.unroute("**/api/v1/auth/me");
  await finishProof();
  await expect(title).toBeVisible();
  await expect(title).toHaveValue("Alice private draft");

  await page.route("**/api/v1/auth/me", route => route.fulfill({ json: { user_id: "boundary-b", username: "bob", display_name: "Bob", email: "bob@example.test", auth_method: "local", is_admin: false } }));
  await page.evaluate(() => window.dispatchEvent(new Event("akb:revalidate-access")));
  await expect(title).toHaveValue("Operating guide");
  await expect(title).not.toHaveValue("Alice private draft");
});

test("Settings proof hides private drafts, restores the same account, and destroys them on account switch", async ({ page }) => {
  await fixture(page);
  await page.goto("/settings");
  const name = settings(page).getByLabel("Display name");
  await name.fill("Private same-account draft");
  await settings(page).getByLabel("Current password").fill("Private password draft");
  let finishProof!: () => Promise<void>;
  let requested!: () => void;
  const proofRequested = new Promise<void>(resolve => { requested = resolve; });
  await page.route("**/api/v1/auth/me", route => {
    finishProof = () => route.fulfill({ json: { user_id: "boundary-a", username: "alice", display_name: "Alice", email: "alice@example.test", auth_method: "local", is_admin: false } });
    requested();
  });
  await page.evaluate(() => window.dispatchEvent(new Event("akb:revalidate-access")));
  await proofRequested;
  await expect(name).toBeHidden();
  await expect(settings(page).getByLabel("Current password")).toBeHidden();
  await page.unroute("**/api/v1/auth/me");
  await finishProof();
  await expect(name).toHaveValue("Private same-account draft");
  await expect(name).toBeVisible();
  await expect(settings(page).getByLabel("Current password")).toHaveValue("Private password draft");
  await page.route("**/api/v1/auth/me", route => route.fulfill({ json: { user_id: "boundary-b", username: "bob", display_name: "Bob", email: "bob@example.test", auth_method: "local", is_admin: false } }));
  await page.evaluate(() => window.dispatchEvent(new Event("akb:revalidate-access")));
  // /settings is a compatibility route: it may reopen for the newly proved
  // account, but never with the previous account's private form state.
  await expect(settings(page).getByLabel("Display name")).toHaveValue("Bob");
  await expect(settings(page).getByLabel("Current password")).toHaveValue("");
  await expect(page.locator('input[value="Private password draft"]')).toHaveCount(0);
});

test("Settings history approval does not bypass or obscure a dirty underlying document", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/v1/search**", route => route.fulfill({ json: { total: 1, results: [{ title: "Operating guide", uri: "akb://fixture/coll/guides/doc/reading.md", path: "guides/reading.md", vault: "fixture", source_type: "document", score: 1 }] } }));
  await page.goto("/search?q=guide");
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  await page.getByTestId("global-search-dialog").getByRole("combobox").fill("guide");
  await page.getByRole("option", { name: /Operating guide/ }).click();
  const preview = page.getByTestId("document-preview-dialog");
  await preview.getByRole("button", { name: "Open document in vault" }).click();
  // Both surfaces expose Edit. Wait for promotion so the outgoing preview
  // cannot replace the route again with its own preview history state.
  await expect(preview).toHaveCount(0);
  const document = page.getByRole("region", { name: "Document workspace", exact: true });
  await expect(document).toHaveAttribute("data-presentation", "page");
  await page.getByRole("group", { name: "Document reading tools", exact: true }).getByRole("button", { name: "Edit", exact: true }).click();
  await expect(document.getByRole("textbox", { name: "Document body (markdown)", exact: true })).toBeVisible();
  await document.getByLabel("Document title", { exact: true }).fill("Underlying draft");
  await openSettings(page);
  await settings(page).getByLabel("Display name").fill("Settings draft");
  await page.goBack();
  await discard(page).getByRole("button", { name: "Discard changes" }).click();
  await expect(settings(page)).toHaveCount(0);
  const documentPrompt = page.getByRole("dialog", { name: "Leave this document?" });
  await expect(documentPrompt).toBeVisible();
  await documentPrompt.getByRole("button", { name: "Leave document" }).click();
  await expect(page).toHaveURL(/\/search\?q=guide$/);
  await expect(documentPrompt).toHaveCount(0);
});
