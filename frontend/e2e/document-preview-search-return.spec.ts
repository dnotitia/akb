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

for (const dismissal of ["close control", "browser Back"] as const) {
  test(`dirty preview confirms ${dismissal} once and restores the search session`, async ({ page }) => {
    const { search, preview } = await openGlobalSearchPreview(page);
    await preview.getByRole("button", { name: "Edit", exact: true }).click();
    await preview.getByRole("textbox", { name: "Document title", exact: true }).fill("Unsaved preview title");
    const exit = async () => {
      if (dismissal === "close control") await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
      else await page.evaluate(() => history.back());
    };
    const confirmation = page.getByRole("dialog", { name: "Leave this document?", exact: true });
    await exit();
    await confirmation.getByRole("button", { name: "Keep editing", exact: true }).click();
    await expect(preview.getByRole("textbox", { name: "Document title", exact: true })).toHaveValue("Unsaved preview title");
    await expect(search).toHaveCount(0);
    await exit();
    await confirmation.getByRole("button", { name: "Leave document", exact: true }).click();
    await expect(preview).toHaveCount(0);
    await expect(confirmation).toHaveCount(0);
    await expect(search).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(1);
    await expect(page).toHaveURL(/\/search$/);
    await expect(search.getByRole("combobox", { name: "Search in fixture, handbook" })).toHaveValue("reading");
  });
}

for (const dismissal of ["close control", "browser Back"] as const) {
  test(`full Search preserves filters, draft, result scroll and focus after ${dismissal}`, async ({ page }) => {
    await fixture(page);
    await page.route("**/api/v1/search?**", route => route.fulfill({ json: {
      query: "reading", archive_scope: "unarchived", total: 25, returned: 25,
      results: Array.from({ length: 25 }, (_, index) => ({
        title: `${title} ${index + 1}`, uri: `akb://fixture/coll/guides/doc/reading-${index}.md`,
        vault: "fixture", path: `guides/reading-${index}.md`, collection: "guides",
        source_type: "document", doc_type: "report", tags: ["team"],
        summary: "Keep the document in its search context.", score: 1 / (index + 1),
      })),
    } }));
    await page.goto("/search?q=reading&source=document&doc_type=report&tag=team&collection=guides");
    const query = page.getByRole("searchbox", { name: "Search query", exact: true });
    const pane = page.getByTestId("search-results-pane");
    const result = page.locator("#search-dense-result-18");
    await expect(result).toBeVisible();
    await query.fill("unsubmitted draft");
    await result.scrollIntoViewIfNeeded();
    const before = await pane.evaluate(element => element.scrollTop);
    expect(before).toBeGreaterThan(0);
    const searchURL = page.url();
    await result.click();
    const preview = page.getByTestId("document-preview-dialog");
    await expect(preview).toBeVisible();
    if (dismissal === "close control") await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
    else await page.goBack();
    await expect(preview).toHaveCount(0);
    await expect(page).toHaveURL(searchURL);
    await expect(query).toHaveValue("unsubmitted draft");
    await expect(result).toBeFocused();
    await expect.poll(() => pane.evaluate(element => element.scrollTop)).toBe(before);
    // The active chips can be above the scrolled viewport; their saved state survives.
    await expect(page.getByRole("button", { name: "Remove filter Type: report", exact: true })).toHaveCount(1);
    await expect(page.getByRole("button", { name: "Remove filter Tag: team", exact: true })).toHaveCount(1);
  });
}
