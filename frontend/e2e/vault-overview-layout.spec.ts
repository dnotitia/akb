import { expect, test, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP fixtures.");

// Every API request is fulfilled in the browser; no user Vault is read or changed.
async function fixture(page: Page, { dark = false, folded = false, empty = false, legacy = false, long = false } = {}) {
  await page.addInitScript(({ dark, folded }) => {
    localStorage.setItem("akb_token", "overview-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    if (folded) {
      localStorage.setItem("akb.treeVisible", "0");
      localStorage.setItem("akb.vaultRailCollapsed", "1");
    }
  }, { dark, folded });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", route => {
    const p = new URL(route.request().url()).pathname;
    let json: object = { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 };
    if (p.endsWith("/auth/config")) json = { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
    else if (p.endsWith("/auth/me")) json = { user_id: "overview-user", username: "fixture", display_name: "Review user", email: "review@example.invalid", auth_method: "local", is_admin: false };
    else if (p.endsWith("/vaults")) json = { vaults: [{ id: "overview-vault", name: "fixture", role: "owner" }] };
    else if (p.endsWith("/info")) json = legacy ? { name: "fixture", role: "reader", file_count: 0 } : {
      name: "fixture", role: "owner", public_access: "none", owner_display_name: long ? "A very long owner name that must remain readable without clipping" : "Review owner",
      description: "Architecture, operating guides, and shared knowledge for the platform team.",
      document_count: empty ? 1 : 36, collection_count: empty ? 1 : 8, table_count: empty ? 0 : 2, file_count: empty ? 0 : 7, member_count: 5,
      is_archived: false, is_external_git: false, tables: empty ? [] : [{ name: "service_catalog", row_count: 18 }, { name: "incidents" }],
    };
    else if (p.endsWith("/recent")) json = { changes: empty ? [] : ["Operations Runbook", "System Architecture", "API Gateway", "Indexing Pipeline", "Incident Response", "Vector Store"].map((title, i) => ({
      doc_id: `d-${i}`, title: long && i === 0 ? "An unusually long document title for shared operational ownership and incident response across multiple teams" : title,
      path: `guides/team/resource-${i}.md`, type: "note", changed_at: "2026-09-20T00:00:00Z", commit: "abc1234567", // pragma: allowlist secret — synthetic Git commit
    })) };
    else if (p.includes("/activity/")) json = { total: 1, activity: [{ hash: "abc1234567", subject: "Update operating guides", date: "2026-09-20T00:00:00Z", author_name: "Review owner" }] }; // pragma: allowlist secret — synthetic Git commit
    else if (p.includes("/browse/")) json = { items: [{ type: "collection", path: "guides", name: "Guides" }, { type: "collection", path: "platform", name: "Platform" }] };
    else if (p.endsWith("/help/skill-template")) return route.fulfill({ contentType: "text/plain", body: "# {vault} Guide\n\nStarter template." });
    else if (p.includes("/documents/fixture/")) json = { title: "Fixture guide", path: "overview/vault-skill.md", content: empty ? "# fixture Guide\n\nStarter template." : "# Fixture guide\n\nUse this vault for architecture decisions, operational procedures, and service ownership.", created_at: "2026-08-20T00:00:00Z", updated_at: "2026-09-20T00:00:00Z" };
    return route.fulfill({ json });
  });
}

for (const dark of [false, true]) for (const width of [375, 1440, 1920, 2560]) {
  test(`Overview prioritizes content at ${width}px in ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, { dark });
    await page.goto("/vault/fixture");
    const recent = page.getByRole("region", { name: "Recent activity", exact: true });
    const aside = page.getByRole("complementary", { name: "Vault overview details" });
    const summary = page.getByRole("region", { name: "Vault summary", exact: true });
    const contents = summary.getByRole("region", { name: "Contents", exact: true });
    await expect(contents).toBeVisible();
    await expect(summary.getByRole("button", { name: "Copy akb://fixture" })).toBeVisible();
    await expect(summary.getByText("owner", { exact: true })).toBeVisible();
    await expect(aside.getByRole("region", { name: "Contents", exact: true })).toHaveCount(0);
    await expect(aside.getByRole("region")).toHaveCount(2);
    await expect(aside.getByRole("region", { name: "Tables", exact: true })).toHaveCount(0);
    const sections = await aside.getByRole("region").all();
    for (let index = 1; index < sections.length; index++) {
      const previous = (await sections[index - 1].boundingBox())!;
      const current = (await sections[index].boundingBox())!;
      const gap = current.y - previous.y - previous.height;
      expect(gap).toBeGreaterThanOrEqual(12);
      expect(gap).toBeLessThanOrEqual(16);
      expect(current.x).toBe(previous.x);
      expect(current.width).toBe(previous.width);
    }
    const link = recent.getByRole("link", { name: /Operations Runbook/ });
    await expect(link).toHaveAttribute("href", "/vault/fixture/doc/guides%2Fteam%2Fresource-0.md");
    await expect(link).toContainText("guides / team");
    await expect(recent).not.toContainText("abc1234");
    const mainBox = (await recent.boundingBox())!;
    const asideBox = (await aside.boundingBox())!;
    if (width >= 1920) {
      expect(Math.abs(mainBox.y - asideBox.y)).toBeLessThanOrEqual(1);
      expect(asideBox.width).toBeGreaterThanOrEqual(304);
      expect(asideBox.width).toBeLessThanOrEqual(336);
      expect(mainBox.width).toBeGreaterThan(600);
    } else {
      expect(asideBox.y).toBeGreaterThan(mainBox.y + mainBox.height);
      expect(Math.abs(asideBox.width - mainBox.width)).toBeLessThanOrEqual(1);
    }
    if (width >= 1440) {
      const contentsBox = (await contents.boundingBox())!;
      const actionsBox = (await summary.getByRole("group", { name: "Create content" }).boundingBox())!;
      expect(Math.abs(contentsBox.y - actionsBox.y)).toBeLessThanOrEqual(1);
      expect(actionsBox.x).toBeGreaterThan(contentsBox.x + contentsBox.width + 16);
    }
    // Counts share the summary with actions instead of an expanded stats band.
    const identityBox = (await summary.boundingBox())!;
    expect(mainBox.y - identityBox.y - identityBox.height).toBeLessThanOrEqual(24);
    for (const button of await summary.getByRole("button").all()) {
      const buttonBox = (await button.boundingBox())!;
      expect(buttonBox.x + buttonBox.width).toBeLessThanOrEqual(identityBox.x + identityBox.width + 1);
    }
    for (const count of await contents.locator("dl > div").all()) {
      const box = (await count.boundingBox())!;
      expect(box.width).toBeLessThan(180);
      expect(box.height).toBeLessThanOrEqual(33);
      expect(box.x + box.width).toBeLessThanOrEqual(identityBox.x + identityBox.width + 1);
    }
    await expect(page.getByRole("button", { name: "Show commits" })).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByRole("link", { name: /Members.*5/ })).toHaveAttribute("href", "/vault/fixture/members");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("vault-overview.png"), fullPage: true });
  });
}

test("Overview uses available workspace width when the navigator is folded", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page, { folded: true });
  await page.goto("/vault/fixture");
  const recent = page.getByRole("region", { name: "Recent activity", exact: true });
  const aside = page.getByRole("complementary", { name: "Vault overview details" });
  await expect(aside).toBeVisible();
  expect(Math.abs((await recent.boundingBox())!.y - (await aside.boundingBox())!.y)).toBeLessThanOrEqual(1);
});

test("Empty Vault keeps Contents and all setup entry points", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await fixture(page, { empty: true });
  await page.goto("/vault/fixture");
  await expect(page.getByRole("region", { name: "Contents", exact: true })).toBeVisible();
  const setup = page.getByRole("region", { name: "Set up this Vault" });
  await expect(setup.getByRole("button", { name: /Import knowledge bundle/ })).toBeVisible();
  await expect(setup.getByRole("link", { name: /Describe this Vault/ })).toHaveAttribute("href", "/vault/fixture/settings#skill");
  await expect(setup.getByRole("link", { name: /Connect an agent/ })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("vault-empty.png") });
});

test("Legacy counts stay unavailable without an invented empty onboarding", async ({ page }) => {
  await fixture(page, { legacy: true });
  await page.goto("/vault/fixture");
  await expect(page.getByRole("region", { name: "Contents", exact: true })).toContainText("Some counts aren't available");
  await expect(page.getByRole("region", { name: "Access and ownership", exact: true })).not.toContainText("Private");
  await expect(page.getByRole("region", { name: "Set up this Vault" })).toHaveCount(0);
});

test("Long content and enlarged text remain readable with reduced motion", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 768, height: 600 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await fixture(page, { long: true });
  await page.goto("/vault/fixture");
  await expect(page.getByRole("region", { name: "Contents", exact: true })).toBeVisible();
  await page.addStyleTag({ content: "html { font-size: 20px !important; }" });
  const link = page.getByRole("link", { name: /An unusually long document title/ });
  await link.focus();
  await expect(link).toBeFocused();
  expect(await link.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("vault-text-scale.png") });
  const aside = page.getByRole("complementary", { name: "Vault overview details" });
  await aside.scrollIntoViewIfNeeded();
  expect(await aside.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  await expect(aside.getByText("A very long owner name that must remain readable without clipping")).toBeVisible();
  for (const target of await aside.getByRole("link").all()) {
    expect(await target.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  }
  await page.screenshot({ path: testInfo.outputPath("vault-context-text-scale.png"), animations: "disabled" });
});

for (const dark of [false, true]) test(`Context separates information from destinations in ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await fixture(page, { dark });
  await page.goto("/vault/fixture");
  const aside = page.getByRole("complementary", { name: "Vault overview details" });
  await expect(aside).toBeVisible();
  for (const name of ["Vault guide", "Access and ownership"]) {
    const section = aside.getByRole("region", { name, exact: true });
    const header = section.getByRole("heading", { name, exact: true }).locator("..");
    const surface = await section.evaluate(el => {
      let node: Element | null = el;
      while (node) {
        const bg = getComputedStyle(node).backgroundColor;
        if (bg !== "rgba(0, 0, 0, 0)") return bg;
        node = node.parentElement;
      }
      return "transparent";
    });
    expect(await header.evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe(surface);
    expect(await header.evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe("rgba(0, 0, 0, 0)");
  }
  const guide = aside.getByRole("link", { name: "Open vault guide" });
  await expect(guide).toHaveAccessibleDescription("Guide customized");
  // These opaque token surfaces must meet normal-text AA independently in both themes.
  const contrast = await aside.locator("h2, p, a, dt, dd, #vault-guide-status").evaluateAll(elements => {
    const channels = (color: string) => (color.match(/[\d.]+/g) || []).map(Number);
    const luminance = (rgb: number[]) => rgb.slice(0, 3).map(v => {
      const c = v / 255;
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
    return elements.map(el => {
      let parent: Element | null = el;
      let background = [255, 255, 255];
      while (parent) {
        const color = channels(getComputedStyle(parent).backgroundColor);
        if (color.length === 3 || color[3] === 1) { background = color; break; }
        parent = parent.parentElement;
      }
      const foreground = luminance(channels(getComputedStyle(el).color));
      const bg = luminance(background);
      return { label: el.textContent, ratio: (Math.max(foreground, bg) + 0.05) / (Math.min(foreground, bg) + 0.05) };
    });
  });
  for (const sample of contrast) expect(sample.ratio, sample.label || "Context text contrast").toBeGreaterThanOrEqual(4.5);
  for (const link of [aside.getByRole("link", { name: /Members/ })]) {
    const before = (await link.boundingBox())!;
    expect(before.height).toBeGreaterThanOrEqual(40);
    const resting = await link.evaluate(el => getComputedStyle(el).backgroundColor);
    await link.hover();
    await expect.poll(() => link.evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe(resting);
    await link.focus();
    await expect(link).toBeFocused();
    expect(await link.evaluate(el => getComputedStyle(el).boxShadow)).not.toBe("none");
    const after = (await link.boundingBox())!;
    expect(after.width).toBe(before.width);
    expect(after.height).toBe(before.height);
  }
  await page.screenshot({ path: testInfo.outputPath("vault-context.png"), animations: "disabled" });
});
