import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses.");

const title = "Team operating guide";
const path = "guides/reading.md";
const uri = "akb://fixture/coll/guides/doc/reading.md";

async function fixture(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem("akb_token", "search-return-fixture");
    localStorage.setItem("akb_theme", "light");
  });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (pathname.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "search-return-user", username: "fixture", display_name: "Author", auth_method: "local", is_admin: false } });
    if (pathname.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "fixture-vault", name: "fixture", role: "writer" }, { id: "handbook-vault", name: "handbook", role: "reader" }] } });
    if (pathname.endsWith("/info")) return route.fulfill({ json: { name: "fixture", role: "writer", is_archived: false, is_external_git: false, document_count: 2 } });
    if (pathname.includes("/browse/")) return route.fulfill({ json: { items: [{ type: "collection", name: "Guides", path: "guides" }, { type: "document", name: title, path }] } });
    if (pathname.endsWith("/search")) return route.fulfill({ json: { query: "reading", total: 2, returned: 2, total_matches: 2, results: [
      { title: "Introduction", uri: "akb://fixture/coll/guides/doc/intro.md", vault: "fixture", path: "guides/intro.md", source_type: "document", score: 1 },
      { title, uri, vault: "fixture", path, source_type: "document", score: 0.9 },
    ] } });
    if (pathname.includes("/documents/fixture/") && !pathname.endsWith("/history")) return route.fulfill({ json: { uri, path, title, content: "# Reading\n\nKeep this document in context.", summary: "Original summary", type: "guide", domain: "engineering", tags: ["team"], status: "active", current_commit: "aaaaaaaaaaaaa", is_public: false } });
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
}

async function openGlobalSearchPreview(page: Page) {
  await fixture(page);
  await page.goto("/search");
  await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
  const search = page.getByTestId("global-search-dialog");
  await search.getByRole("button", { name: "Search scope: All vaults" }).click();
  await page.getByRole("menuitemcheckbox", { name: "fixture", exact: true }).click();
  await page.getByRole("menuitemcheckbox", { name: "handbook", exact: true }).click();
  await page.keyboard.press("Escape");
  await search.getByRole("button", { name: "Documents", exact: true }).click();
  const input = search.getByRole("combobox");
  await input.fill("reading");
  await expect(search.getByRole("option")).toHaveCount(2);
  await input.press("ArrowDown");
  await expect(search.getByRole("option", { name: new RegExp(title) })).toHaveAttribute("aria-selected", "true");
  await input.press("Enter");
  const preview = page.getByTestId("document-preview-dialog");
  await expect(preview).toBeVisible();
  await expect(search).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(1);
  return { search, preview };
}

for (const dismissal of ["close control", "browser Back"] as const) {
  test(`global Search resumes after preview Edit, Cancel and ${dismissal}`, async ({ page }) => {
    const { search, preview } = await openGlobalSearchPreview(page);
    await preview.getByRole("button", { name: "Edit", exact: true }).click();
    await expect(preview.getByRole("textbox", { name: "Summary", exact: true })).toBeVisible();
    await expect(search).toHaveCount(0);
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await preview.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(preview.getByRole("button", { name: "Edit", exact: true })).toBeVisible();
    if (dismissal === "close control") await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
    else await page.evaluate(() => history.back());

    await expect(preview).toHaveCount(0);
    await expect(search).toBeVisible();
    await expect(page).toHaveURL(/\/search$/);
    await expect(page.getByRole("dialog")).toHaveCount(1);
    const input = search.getByRole("combobox", { name: "Search in fixture, handbook" });
    await expect(input).toHaveValue("reading");
    await expect(input).toBeFocused();
    await expect(input).toHaveAttribute("aria-activedescendant", "global-search-result-1");
    await expect(search.getByRole("option", { name: new RegExp(title) })).toHaveAttribute("aria-selected", "true");
    await expect(search.getByRole("button", { name: "Documents, 2 results" })).toHaveAttribute("aria-pressed", "true");
  });
}

test("Open in vault promotes the document without reopening global Search", async ({ page }) => {
  const { search, preview } = await openGlobalSearchPreview(page);
  await preview.getByRole("button", { name: "Open document in vault", exact: true }).click();
  await expect(preview).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Document workspace", exact: true })).toHaveAttribute("data-presentation", "page");
  await expect(search).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page).toHaveURL(/\/vault\/fixture\/doc\//);
});
