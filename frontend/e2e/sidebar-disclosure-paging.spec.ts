import { expect, test } from "@playwright/test";
import { readingFixture } from "./fixtures/reading-workspace";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Uses isolated browser-owned directory pages.");

test("reveals the last collection from its own bounded request while keeping siblings visible", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 720 });
  await readingFixture(page);
  const requests: URL[] = [];
  await page.route("**/api/v1/browse/fixture?*", async route => {
    const url = new URL(route.request().url());
    requests.push(url);
    const collection = url.searchParams.get("collection");
    const cursor = url.searchParams.get("cursor");
    const base = { vault: "fixture", path: collection || "", archive_scope: "unarchived" };
    if (collection === "collection-11") return route.fulfill({ json: {
      ...base, complete: true, next_cursor: null,
      items: [{ type: "document", path: "collection-11/first.md", name: "First child in the last collection" }],
    } });
    if (url.searchParams.get("content_type") !== "all") return route.fulfill({ json: { ...base, complete: true, items: [] } });
    if (!cursor) return route.fulfill({ json: { ...base, complete: false, next_cursor: "hidden-1",
      items: Array.from({ length: 12 }, (_, i) => ({ type: "collection", name: `collection-${String(i).padStart(2, "0")}`, path: `collection-${String(i).padStart(2, "0")}` })),
    } });
    if (cursor === "hidden-1" || cursor === "hidden-2") return route.fulfill({ json: {
      ...base, complete: false, next_cursor: cursor === "hidden-1" ? "hidden-2" : "last-page",
      items: [{ type: "document", path: `collection-00/${cursor}.md`, name: `Unrelated ${cursor}` }],
    } });
    return route.fulfill({ json: { ...base, complete: true, next_cursor: null,
      items: [{ type: "document", path: "collection-11/first.md", name: "First child in the last collection" }],
    } });
  });
  await page.goto("/vault/fixture");
  const explorer = page.getByRole("navigation", { name: "fixture explorer", exact: true });
  const last = explorer.getByRole("button", { name: "collection-11", exact: true });
  await last.scrollIntoViewIfNeeded();
  await last.click();
  await expect(last).toHaveAttribute("aria-expanded", "true");
  await expect.poll(() => requests.some(url => url.searchParams.get("collection") === "collection-11"
    && url.searchParams.get("page_size") === "100")).toBe(true);
  await expect(explorer.getByRole("link", { name: "Document: First child in the last collection", exact: true })).toBeInViewport();
  await expect(explorer.getByRole("button", { name: "collection-00", exact: true })).toBeAttached();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
});

test("reveals already loaded children at the bottom while keeping the opening row visible", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 });
  await readingFixture(page);
  await page.route("**/api/v1/browse/fixture?*", route => route.fulfill({ json: {
    vault: "fixture", path: "", archive_scope: "unarchived", complete: true,
    items: [...Array.from({ length: 20 }, (_, i) => ({ type: "collection", path: `collection-${String(i).padStart(2, "0")}`, name: `collection-${i}` })),
      { type: "document", path: "collection-19/ready.md", name: "Ready child" }],
  } }));
  await page.goto("/vault/fixture");
  const explorer = page.getByRole("navigation", { name: "fixture explorer", exact: true });
  const last = explorer.getByRole("button", { name: "collection-19", exact: true });
  await last.scrollIntoViewIfNeeded();
  await last.click();
  await expect(explorer.getByRole("link", { name: "Document: Ready child", exact: true })).toBeInViewport();
  await expect(last).toBeInViewport();
  await expect(last).toBeFocused();
});

test("a late branch response respects scrolling away from the opening row", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 600 });
  await readingFixture(page);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/v1/browse/fixture?*", async route => {
    const url = new URL(route.request().url());
    const collection = url.searchParams.get("collection");
    if (collection === "collection-19") {
      await gate;
      return route.fulfill({ json: { vault: "fixture", path: collection, complete: true,
        items: [{ type: "document", path: `${collection}/late.md`, name: "Late child" }] } });
    }
    return route.fulfill({ json: { vault: "fixture", path: "", complete: false, next_cursor: "global-tail",
      items: url.searchParams.has("cursor") ? [] : Array.from({ length: 20 }, (_, i) => ({
        type: "collection", path: `collection-${String(i).padStart(2, "0")}`, name: `collection-${i}`,
      })),
    } });
  });
  await page.goto("/vault/fixture");
  const explorer = page.getByRole("navigation", { name: "fixture explorer", exact: true });
  const last = explorer.getByRole("button", { name: "collection-19", exact: true });
  await last.scrollIntoViewIfNeeded();
  await last.click();
  await expect(explorer.getByText("Loading collection-19", { exact: true })).toBeVisible();
  await explorer.hover();
  await page.mouse.wheel(0, -2000);
  await expect.poll(() => explorer.evaluate(element => element.scrollTop)).toBe(0);
  await expect(last).not.toBeInViewport();
  release();
  await expect(explorer.getByRole("link", { name: "Document: Late child", exact: true })).toBeAttached();
  expect(await explorer.evaluate(element => element.scrollTop)).toBe(0);
  await expect(last).not.toBeInViewport();
});

for (const width of [375, 1440]) test(`continues the expanded collection's own pages at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 720 });
  await readingFixture(page);
  const branchCursors: (string | null)[] = [];
  await page.route("**/api/v1/browse/fixture?*", route => {
    const url = new URL(route.request().url());
    const collection = url.searchParams.get("collection");
    const cursor = url.searchParams.get("cursor");
    const base = { vault: "fixture", path: collection || "", archive_scope: "unarchived" };
    if (collection === "last") {
      branchCursors.push(cursor);
      expect(url.searchParams.get("page_size")).toBe("100");
      expect(url.searchParams.get("depth")).toBe("0");
      return route.fulfill({ json: { ...base, complete: Boolean(cursor), next_cursor: cursor ? null : "branch-tail",
        items: cursor ? [{ type: "document", path: "last/100.md", name: "Child 100" }]
          : Array.from({ length: 100 }, (_, i) => ({ type: "document", path: `last/${String(i).padStart(3, "0")}.md`, name: `Child ${String(i).padStart(3, "0")}` })),
      } });
    }
    return route.fulfill({ json: { ...base, complete: false, next_cursor: "global-hidden",
      items: cursor ? [{ type: "document", path: "closed/hidden.md", name: "Hidden document" }]
        : [{ type: "collection", path: "closed", name: "Closed" }, { type: "collection", path: "last", name: "Last" }],
    } });
  });
  await page.goto("/vault/fixture");
  if (width < 1024) await page.getByRole("button", { name: "Open vault navigation", exact: true }).click();
  const explorer = page.getByRole("navigation", { name: "fixture explorer", exact: true });
  const last = explorer.getByRole("button", { name: "last", exact: true });
  await last.click();
  await expect(explorer.getByRole("link", { name: "Document: Child 000", exact: true })).toBeInViewport();
  await explorer.getByRole("link", { name: "Document: Child 099", exact: true }).scrollIntoViewIfNeeded();
  await expect(explorer.getByRole("link", { name: "Document: Child 100", exact: true })).toBeAttached();
  // Development StrictMode may cancel and remount the first query. The next
  // page must still advance once, using this collection's own cursor.
  expect([...new Set(branchCursors)]).toEqual([null, "branch-tail"]);
  expect(branchCursors.filter(cursor => cursor === "branch-tail")).toHaveLength(1);
  await last.scrollIntoViewIfNeeded();
  await last.click();
  await expect(explorer.getByRole("link", { name: "Document: Child 000", exact: true })).toHaveCount(0);
});

test("the Vault directory sidebar reveals all subsequent batches in its own scroller", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 720 });
  await readingFixture(page);
  await page.route("**/api/v1/my/vaults", route => route.fulfill({ json: {
    vaults: Array.from({ length: 80 }, (_, i) => ({ id: `vault-${i}`, name: `vault-${String(i).padStart(3, "0")}`, role: "writer" })),
  } }));
  await page.goto("/vault");
  const sidebar = page.locator('[data-slot="vault-directory-sidebar"]');
  const links = sidebar.getByRole("link", { name: /^vault-\d+$/ });
  await expect(links).toHaveCount(30);
  await links.last().scrollIntoViewIfNeeded();
  await expect(links).toHaveCount(60);
  await links.last().scrollIntoViewIfNeeded();
  await expect(links).toHaveCount(80);
});

test("restored expanded collections outside the viewport wait for scroll demand", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 720 });
  await readingFixture(page);
  await page.addInitScript(() => localStorage.setItem("akb-explorer-expanded:fixture", JSON.stringify(
    Array.from({ length: 100 }, (_, i) => `collection-${String(i).padStart(3, "0")}`),
  )));
  const scopes: string[] = [];
  await page.route("**/api/v1/browse/fixture?*", route => {
    const url = new URL(route.request().url());
    const collection = url.searchParams.get("collection");
    if (collection) {
      scopes.push(collection);
      return route.fulfill({ json: { vault: "fixture", path: collection, complete: true, items: [] } });
    }
    if (url.searchParams.has("cursor")) return route.fulfill({ json: {
      vault: "fixture", path: "", complete: false, next_cursor: "more-hidden", items: [],
    } });
    return route.fulfill({ json: { vault: "fixture", path: "", complete: false, next_cursor: "hidden-resources",
      items: Array.from({ length: 100 }, (_, i) => ({ type: "collection", name: `collection-${i}`, path: `collection-${String(i).padStart(3, "0")}` })),
    } });
  });
  await page.goto("/vault/fixture");
  const explorer = page.getByRole("navigation", { name: "fixture explorer", exact: true });
  const last = explorer.getByRole("button", { name: "collection-099", exact: true });
  await expect(last).toBeAttached();
  await expect.poll(() => scopes.includes("collection-000")).toBe(true);
  expect(scopes).not.toContain("collection-099");
  await last.scrollIntoViewIfNeeded();
  await expect.poll(() => scopes.includes("collection-099")).toBe(true);
});
