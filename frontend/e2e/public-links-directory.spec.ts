import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP fixtures.");

async function fixture(page: Page, { dark = false, failNext = false } = {}) {
  await page.addInitScript(dark => {
    localStorage.setItem("akb_token", "public-links-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, dark);
  const requests: URL[] = [];
  const deleted: string[] = [];
  let nextFailed = false;
  const publications = Array.from({ length: 31 }, (_, i) => ({
    slug: `guide-${i}`, title: ["Operating guide", "API integration checklist", "Platform architecture", "Research notes", "Published service inventory"][i % 5] + ` ${i + 1}`,
    vault: i % 2 ? "research" : "engineering", resource_type: "document", resource_uri: `akb://engineering/doc/guides/guide-${i}.md`, resource_path: `guides/guide-${i}.md`,
    share_url: `https://example.test/p/guide-${i}`, mode: "live", view_count: 3 + i, max_views: null, expires_at: i % 2 ? new Date(Date.now() + 2 * 86_400_000).toISOString() : null,
    created_at: "2026-09-01T00:00:00Z", snapshot_at: null, allow_embed: true, section_filter: null, password_protected: i % 3 === 0,
    availability: "active", can_unpublish: i !== 1, unpublish_disabled_reason: i === 1 ? "Writer access required" : null,
  }));
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let json: object = { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 };
    if (path.endsWith("/auth/config")) json = { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
    else if (path.endsWith("/auth/me")) json = { user_id: "public-links-user", username: "fixture", display_name: "Review user", email: "review@example.invalid", auth_method: "local", is_admin: false };
    else if (path.endsWith("/vaults")) json = { vaults: ["engineering", "research"].map((name, i) => ({ id: `vault-${i}`, name, role: "owner" })) };
    else if (path.endsWith("/info")) json = { role: "owner", name: "engineering", is_archived: false, is_external_git: false, tables: [] };
    else if (path === "/api/v1/publications") {
      requests.push(url);
      if (url.searchParams.has("cursor") && failNext && !nextFailed) { nextFailed = true; return route.fulfill({ status: 503, json: { detail: "Temporary page failure" } }); }
      let matched = publications.filter(item => !deleted.includes(item.slug));
      if (url.searchParams.get("vault")) matched = matched.filter(item => item.vault === url.searchParams.get("vault"));
      if (url.searchParams.get("q")) matched = matched.filter(item => item.title.toLowerCase().includes(url.searchParams.get("q")!.toLowerCase()));
      if (url.searchParams.get("expiry") === "soon") matched = matched.filter(item => item.expires_at !== null);
      if (url.searchParams.get("status") === "unavailable") matched = [];
      const next = url.searchParams.has("cursor");
      json = { publications: next ? matched.slice(30) : matched.slice(0, 30), total: matched.length, complete: next || matched.length <= 30, next_cursor: !next && matched.length > 30 ? "next-page" : null, observed_at: new Date().toISOString() };
    }
    else if (path.startsWith("/api/v1/publications/") && route.request().method() === "DELETE") { deleted.push(path.split("/").at(-1)!); json = { deleted: 1 }; }
    else if (path === "/api/v1/publications/engineering") json = { publications: publications.filter(item => item.vault === "engineering") };
    return route.fulfill({ json });
  });
  return { requests, deleted };
}

for (const dark of [false, true]) for (const width of [320, 375, 768, 1440]) {
  test(`compact directory fits ${width}px ${dark ? "dark" : "light"}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 960 });
    await fixture(page, { dark });
    await page.goto("/publications");
    await expect(page.getByRole("heading", { level: 1, name: "Publications" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Operating guide 1", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Publications vault scope" })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Search publications" })).toBeVisible();
    const overflow = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
    expect(overflow.scroll).toBeLessThanOrEqual(overflow.width);
    const controls = page.getByRole("banner").locator("a, button");
    const bounds = await controls.evaluateAll(elements => elements.map(element => element.getBoundingClientRect()).filter(box => box.width && box.height).map(box => ({ x: box.x, right: box.right })).sort((a, b) => a.x - b.x));
    for (let i = 1; i < bounds.length; i++) expect(bounds[i].x).toBeGreaterThanOrEqual(bounds[i-1].right - 1);
    await page.screenshot({ path: info.outputPath("directory.png"), fullPage: false });
    if (width < 1024) await expect(page.getByRole("navigation", { name: "Primary mobile navigation" }).getByRole("link", { name: "Publications", exact: true })).toHaveAttribute("aria-current", "page");
    else await expect(page.getByRole("navigation", { name: "Workspace navigation" }).getByRole("link", { name: "Publications", exact: true })).toHaveAttribute("aria-current", "page");
  });
}

test("scope, expiry and search are server filters with navigable URL state", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/publications");
  await page.getByRole("button", { name: "Publications vault scope" }).click();
  await page.getByRole("menuitemradio", { name: "research", exact: true }).click();
  await expect.poll(() => state.requests.at(-1)?.searchParams.get("vault")).toBe("research");
  await page.getByRole("button", { name: "Publication expiry", exact: true }).click();
  await page.getByRole("menuitemradio", { name: "Expires within 7 days", exact: true }).click();
  await page.getByRole("textbox", { name: "Search publications" }).fill("API integration");
  await expect.poll(() => state.requests.at(-1)?.searchParams.get("q")).toBe("API integration");
  await expect(page.getByRole("list", { name: "Publications", exact: true }).getByRole("listitem")).toHaveCount(3);
  await page.getByRole("button", { name: "Unavailable", exact: true }).click();
  await expect(page.getByText("No publications match these filters")).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("button", { name: "Published", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("textbox", { name: "Search publications" })).toHaveValue("API integration");
});

test("scroll appends pages, preserves rows on failure and retries explicitly", async ({ page }) => {
  const state = await fixture(page, { failNext: true });
  await page.goto("/publications");
  await expect(page.getByRole("list", { name: "Publications", exact: true }).getByRole("listitem")).toHaveCount(30);
  await page.locator('[data-slot="auto-load-more"]').scrollIntoViewIfNeeded();
  const retry = page.getByRole("button", { name: "Retry: load more publications" });
  await expect(retry).toBeVisible();
  expect(state.requests.filter(url => url.searchParams.has("cursor"))).toHaveLength(1);
  await retry.click();
  await expect(page.getByRole("list", { name: "Publications", exact: true }).getByRole("listitem")).toHaveCount(31);
  expect(state.requests.filter(url => url.searchParams.has("cursor"))).toHaveLength(2);
});

test("read-only management is explained and writable rows use their own Vault", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/publications");
  await page.getByRole("button", { name: "More actions for API integration checklist 2", exact: true }).click();
  await expect(page.getByRole("menuitem", { name: "Unpublish", exact: true })).toBeDisabled();
  await expect(page.getByRole("menuitem", { name: "Unpublish", exact: true })).toHaveAccessibleDescription("Writer access required");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "More actions for Operating guide 1", exact: true }).click();
  await expect(page.getByRole("menuitem", { name: "Open publication" })).toHaveAttribute("href", "https://example.test/p/guide-0");
  await page.getByRole("menuitem", { name: "Unpublish", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Unpublish", exact: true }).click();
  await expect.poll(() => state.deleted).toEqual(["guide-0"]);
  await expect(page.getByRole("textbox", { name: "Search publications" })).toBeFocused();
});

test("existing Vault page connects to the complete inventory", async ({ page }) => {
  await fixture(page);
  await page.goto("/vault/engineering/publications");
  await page.getByRole("link", { name: "View publications from all vaults" }).click();
  await expect(page).toHaveURL(/\/publications$/);
  await expect(page.getByRole("heading", { level: 1, name: "Publications" })).toBeVisible();
});
