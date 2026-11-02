import type { Page } from "@playwright/test";

export async function readingFixture(page: Page, dark = false, contents?: Record<string, string>) {
  await page.addInitScript(({ dark }) => {
    localStorage.setItem("akb_token", "reading-studio-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, { dark });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/reader-diagram.svg", route => route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="400"><rect width="1000" height="400" fill="#004059"/><text x="80" y="210" fill="white" font-size="48">Reader diagram</text></svg>' }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
    if (pathname.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "studio-reader", username: "fixture", display_name: "Reading reviewer", email: "reader@example.invalid", auth_method: "local", is_admin: false } });
    if (pathname.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "studio-vault", name: "fixture", role: "writer" }] } });
    if (pathname.endsWith("/info")) return route.fulfill({ json: { name: "fixture", role: "writer", is_archived: false, is_external_git: false, document_count: 2 } });
    if (pathname.includes("/browse/")) return route.fulfill({ json: { complete: true, scanned: 2, items: [
      { type: "document", path: "reading.md", name: "Reading workflow" },
      { type: "document", path: "reference.md", name: "Reference notes" },
    ] } });
    if (pathname.endsWith("/relations")) return route.fulfill({ json: { uri: "akb://fixture/doc/reading.md", relations: [
      { direction: "outgoing", relation: "references", uri: "akb://fixture/doc/reference.md", name: "Reference notes", kind: "explicit", resource_type: "document" },
      { direction: "incoming", relation: "depends_on", uri: "akb://fixture/doc/reference.md", name: "Reference notes", kind: "explicit", resource_type: "document" },
    ] } });
    if (pathname.includes("/documents/fixture/") && !pathname.endsWith("/history")) {
      const path = decodeURIComponent(pathname.split("/documents/fixture/")[1]);
      const main = path === "reading.md";
      return route.fulfill({ json: { path, title: main ? "Reading workflow" : "Reference notes",
        content: contents?.[path] ?? (main ? '# Reading workflow\n\nA needle in a document.\n\n## Code example\n\n```ts\nconst needle = "example";\nconsole.log(needle);\n```\n\n### Nested detail\n\nDetailed reading instructions.\n\n![Reading diagram](http://127.0.0.1:4174/reader-diagram.svg)\n\n[Inspect reference](akb://fixture/doc/reference.md)\n\n## Final notes\n\nEnd of the document.'
          : '# Reference notes\n\nA separate reference.'),
        uri: `akb://fixture/doc/${path}`, current_commit: "aaaaaaaaaaaaa", status: main ? "draft" : "active", created_by_name: "Reading reviewer", updated_at: "2026-10-27T03:00:00Z", created_at: "2026-10-26T03:00:00Z", tags: [] } });
    }
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
}
