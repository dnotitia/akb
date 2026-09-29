import { expect, test } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP responses; never writes user data.");

const collection = "notes/팀 자료 & 운영";
const docPath = `${collection}/deploy.md`;

for (const width of [375, 2560]) for (const dark of [false, true]) {
  test(`Collection sidebar scopes names without navigation at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(({ dark }) => {
      localStorage.setItem("akb_token", "collection-search-fixture");
      localStorage.setItem("akb_theme", dark ? "dark" : "light");
      localStorage.setItem("akb.treeVisible", "1");
    }, { dark });
    const searches: URL[] = [];
    await page.route("**/health/**", route => route.fulfill({ json: {} }));
    await page.route("**/api/v1/**", route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
      if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "collection-reader", username: "reader", display_name: "Collection reader", email: "reader@example.invalid", auth_method: "local", is_admin: false } });
      if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "alpha", name: "alpha", role: "reader" }] } });
      if (path.endsWith("/info")) return route.fulfill({ json: { name: "alpha", role: "reader", document_count: 1, collection_count: 2, is_archived: false, is_external_git: false } });
      if (path.includes("/browse/")) return route.fulfill({ json: { vault: "alpha", items: [
        { type: "collection", name: "Notes", path: "notes" },
        { type: "collection", name: "팀 자료 & 운영", path: collection },
        { type: "collection", name: "Other", path: `${collection}-old` },
        { type: "collection", name: "Child", path: `${collection}/child` },
        { type: "document", name: "Deployment guide", path: docPath },
        { type: "document", name: "Deployment nested guide", path: `${collection}/child/nested.md` },
        { type: "document", name: "Deployment outside", path: `${collection}-old/outside.md` },
        { type: "table", name: "deployment_table", path: "deployment_table", collection },
        { type: "file", name: "Deployment checklist", path: "checklist.txt", collection, uri: `akb://alpha/coll/${collection}/file/file-1` },
      ] } });
      if (path.endsWith("/search")) {
        searches.push(url);
        return route.fulfill({ json: { results: [{ source_type: "file", title: "Deployment checklist", vault: "alpha", collection, path: "checklist.txt", uri: `akb://alpha/coll/${collection}/file/file-1`, score: 1 }], total: 1, returned: 1 } });
      }
      if (path.includes("/documents/alpha/") && !path.endsWith("/history")) return route.fulfill({ json: { title: "Deployment guide", path: docPath, uri: `akb://alpha/coll/${collection}/doc/deploy.md`, content: "# Deployment guide\n\nRead the checklist before deploying.", status: "active" } });
      return route.fulfill({ json: { items: [], history: [], relations: [], subscribed: false, unread_count: 0 } });
    });
    await page.goto(`/vault/alpha/doc/${encodeURIComponent(docPath)}`);
    const drawer = page.getByRole("button", { name: "Open vault navigation", exact: true });
    if (width < 1024) await drawer.click();
    const scope = page.getByRole("button", { name: "Collection search scope", exact: true });
    await expect(scope).toHaveCount(0);
    const documents = page.getByRole("treeitem", { name: "Documents, 1 item", exact: true });
    await expect(documents).toHaveAttribute("aria-expanded", "true");
    await documents.click();
    await expect(documents).toHaveAttribute("aria-expanded", "false");
    await page.getByRole("button", { name: "Refresh collections", exact: true }).click();
    await expect(page.getByRole("tree", { name: "alpha explorer" })).not.toHaveAttribute("aria-busy", "true");
    await expect(documents).toHaveAttribute("aria-expanded", "false");
    await documents.focus();
    await page.keyboard.press("ArrowRight");
    await expect(documents).toHaveAttribute("aria-expanded", "true");
    const launcher = page.getByRole("button", { name: `Collection actions for ${collection}`, exact: true });
    await launcher.click();
    await page.getByRole("menuitem", { name: "Search in collection", exact: true }).click();
    const input = page.getByRole("searchbox", { name: "Filter resources" });
    await expect(input).toBeFocused();
    await expect(scope).toContainText(collection);
    await expect(page.getByRole("dialog", { name: "Search knowledge" })).toHaveCount(0);
    await input.fill("deploy");
    await expect(page.getByRole("treeitem", { name: "Document: Deployment guide", exact: true })).toBeVisible();
    await expect(page.getByRole("treeitem", { name: "Document: Deployment nested guide", exact: true })).toBeVisible();
    await expect(page.getByRole("treeitem", { name: "File: Deployment checklist", exact: true })).toBeVisible();
    await expect(page.getByRole("treeitem", { name: "Table: deployment_table", exact: true })).toBeVisible();
    await expect(page.getByRole("treeitem", { name: "Document: Deployment outside", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Clear collection scope" })).toBeInViewport();
    const scopedFilters = page.getByRole("button", { name: "Filter collections, 1 active", exact: true });
    await expect(scopedFilters).toHaveAttribute("aria-expanded", "true");
    await scopedFilters.click();
    await expect(scope).toHaveCount(0);
    await expect(page.getByRole("treeitem", { name: "Document: Deployment nested guide", exact: true })).toBeVisible();
    await expect(page.getByRole("treeitem", { name: "Document: Deployment outside", exact: true })).toHaveCount(0);
    await scopedFilters.click();
    await expect(scope).toContainText(collection);
    await page.getByRole("button", { name: "Resource type", exact: true }).click();
    await page.getByRole("menuitemradio", { name: /^Files/ }).click();
    await expect(page.getByRole("treeitem", { name: "File: Deployment checklist", exact: true })).toBeVisible();
    await expect(page.getByRole("treeitem", { name: "Document: Deployment guide", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Filter collections, 2 active", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Reset filters", exact: true }).click();
    await expect(scope).toContainText("All collections");
    await expect(input).toHaveValue("deploy");
    await expect(page.getByRole("treeitem", { name: "Document: Deployment outside", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Filter collections", exact: true })).toBeVisible();
    await launcher.click();
    await page.getByRole("menuitem", { name: "Search in collection", exact: true }).click();
    await expect(scope).toContainText(collection);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("collection-search.png") });
    await input.fill("missing-name");
    await expect(page.getByText("No resources match these filters.")).toBeVisible();
    await expect(scope).toContainText(collection);
    await page.getByRole("button", { name: "Clear collection scope" }).click();
    await input.fill("deploy");
    await expect(page.getByRole("treeitem", { name: "Document: Deployment outside", exact: true })).toBeVisible();
    await scope.click();
    const picker = page.getByRole("combobox", { name: "Find a collection", exact: true });
    await picker.fill(`${collection}/child`);
    await picker.press("ArrowDown");
    await picker.press("Enter");
    await expect(scope).toContainText(`${collection}/child`);
    await expect(page.getByRole("treeitem", { name: "Document: Deployment nested guide", exact: true })).toBeVisible();
    await expect(page.getByRole("treeitem", { name: "Document: Deployment outside", exact: true })).toHaveCount(0);
    expect(searches).toHaveLength(0);
    expect(decodeURIComponent(new URL(page.url()).pathname)).toBe(`/vault/alpha/doc/${docPath}`);
  });
}
