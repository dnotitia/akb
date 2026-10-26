import { expect, test, type Locator, type Page } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns isolated HTTP fixtures.");

// Every API request is fulfilled in the browser; no user Vault is read or changed.
async function fixture(page: Page, { dark = false, folded = false, empty = false, legacy = false, long = false, description = "Architecture, operating guides, and shared knowledge for the platform team." } = {}) {
  await page.addInitScript(({ dark, folded }) => {
    localStorage.setItem("akb_token", "overview-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
    if (folded) {
      localStorage.setItem("akb.treeVisible", "0");
      localStorage.setItem("akb.vaultRailCollapsed", "1");
    }
  }, { dark, folded });
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  let browses = 0;
  const createdCollections: string[] = [];
  await page.route("**/api/v1/**", route => {
    const p = new URL(route.request().url()).pathname;
    let json: object = { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 };
    if (p.endsWith("/collections/fixture") && route.request().method() === "POST") {
      const { path } = route.request().postDataJSON() as { path: string };
      createdCollections.push(path);
      return route.fulfill({ json: { ok: true, created: true, collection: { path, name: path.split("/").at(-1), summary: null, doc_count: 0 } } });
    }
    if (p.endsWith("/auth/config")) json = { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } };
    else if (p.endsWith("/auth/me")) json = { user_id: "overview-user", username: "fixture", display_name: "Review user", email: "review@example.invalid", auth_method: "local", is_admin: false };
    else if (p.endsWith("/vaults")) json = { vaults: [{ id: "overview-vault", name: "fixture", role: "owner" }] };
    else if (p.endsWith("/info")) json = legacy ? { name: "fixture", role: "reader", file_count: 0 } : {
      name: "fixture", role: "owner", public_access: "none", owner_display_name: long ? "A very long owner name that must remain readable without clipping" : "Review owner",
      description,
      document_count: empty ? 1 : 36, collection_count: (empty ? 1 : 8) + createdCollections.length, table_count: empty ? 0 : 2, file_count: empty ? 0 : 7, member_count: 5,
      is_archived: false, is_external_git: false, tables: empty ? [] : [{ name: "service_catalog", row_count: 18 }, { name: "incidents" }],
    };
    else if (p.endsWith("/recent")) json = { changes: empty ? [] : ["Operations Runbook", "System Architecture", "API Gateway", "Indexing Pipeline", "Incident Response", "Vector Store"].map((title, i) => ({
      doc_id: `d-${i}`, title: long && i === 0 ? "An unusually long document title for shared operational ownership and incident response across multiple teams" : title,
      path: `guides/team/resource-${i}.md`, type: "note", changed_at: "2026-09-20T00:00:00Z", commit: "abc1234567", // pragma: allowlist secret — synthetic Git commit
    })) };
    else if (p.includes("/activity/")) json = { total: 1, activity: [{ hash: "abc1234567", subject: "Update operating guides", date: "2026-09-20T00:00:00Z", author_name: "Review owner" }] }; // pragma: allowlist secret — synthetic Git commit
    else if (p.includes("/browse/")) {
      browses++;
      json = { items: [
        { type: "collection", path: "guides", name: "Guides" },
        { type: "collection", path: "platform", name: "Platform" },
        { type: "collection", path: "guides/operations", name: "Operations" },
        ...createdCollections.map(path => ({ type: "collection", path, name: path.split("/").at(-1) })),
        { type: "document", path: "guides/runbook.md", name: "Runbook" },
        { type: "table", path: "services", name: "services", collection: "guides" },
        { type: "file", path: "legacy-file-id", name: "diagram.svg", uri: "akb://fixture/file/7ead8d7b-2861-4d64-8190-4cab55372b91", collection: "guides" },
      ] };
    }
    else if (p.endsWith("/help/skill-template")) return route.fulfill({ contentType: "text/plain", body: "# {vault} Guide\n\nStarter template." });
    else if (p.includes("/documents/fixture/")) json = { title: "Fixture guide", path: "overview/vault-skill.md", content: empty ? "# fixture Guide\n\nStarter template." : "# Fixture guide\n\nUse this vault for architecture decisions, operational procedures, and service ownership.", created_at: "2026-08-20T00:00:00Z", updated_at: "2026-09-20T00:00:00Z" };
    return route.fulfill({ json });
  });
  return { browses: () => browses, createdCollections: () => [...createdCollections] };
}

/** Measure natural contents inside the same named container so responsive icons
 * retain their actual display state while the temporary copies are measured. */
async function expectSummaryUsesAvailableWidth(summary: Locator) {
  const fit = await summary.evaluate(element => {
    const contents = element.querySelector('[aria-label="Contents"]')!;
    const pages = element.querySelector('nav[aria-label="Vault pages"]')!;
    const row = contents.parentElement!;
    const naturalWidth = (source: Element) => {
      const clone = source.cloneNode(true) as HTMLElement;
      Object.assign(clone.style, { position: "absolute", visibility: "hidden", width: "max-content", maxWidth: "none", inset: "0 auto auto 0" });
      row.appendChild(clone);
      const width = clone.getBoundingClientRect().width;
      clone.remove();
      return width;
    };
    const contentsWidth = naturalWidth(contents);
    const pagesWidth = naturalWidth(pages);
    const style = getComputedStyle(row);
    return {
      fits: contentsWidth + pagesWidth + parseFloat(style.columnGap) <= row.clientWidth + 1,
      available: row.clientWidth,
      required: contentsWidth + pagesWidth + parseFloat(style.columnGap),
    };
  });
  const contents = (await summary.getByRole("region", { name: "Contents", exact: true }).boundingBox())!;
  const pages = (await summary.getByRole("navigation", { name: "Vault pages", exact: true }).boundingBox())!;
  if (fit.fits) {
    expect(Math.abs(contents.y + contents.height / 2 - pages.y - pages.height / 2), `${fit.required}px of content fits within ${fit.available}px`).toBeLessThanOrEqual(1);
    expect(pages.x).toBeGreaterThanOrEqual(contents.x + contents.width + 19);
  } else {
    expect(pages.y).toBeGreaterThanOrEqual(contents.y + contents.height);
  }
  return fit;
}

for (const dark of [false, true]) for (const width of [375, 874, 1280, 1440, 1920, 2560]) {
  test(`Overview prioritizes content at ${width}px in ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await fixture(page, { dark });
    await page.goto("/vault/fixture");
    const recent = page.getByRole("region", { name: "Recent activity", exact: true });
    const collections = page.getByRole("region", { name: "Collection workspace", exact: true });
    const aside = page.getByRole("complementary", { name: "Vault overview details" });
    const summary = page.getByRole("region", { name: "Vault summary", exact: true });
    const contents = summary.getByRole("region", { name: "Contents", exact: true });
    const pages = summary.getByRole("navigation", { name: "Vault pages", exact: true });
    await expect(contents).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const create = summary.getByRole("button", { name: "New content", exact: true });
    await expect(create).toBeVisible();
    await expect(create).toHaveText("");
    const createBox = (await create.boundingBox())!;
    expect(createBox.width).toBe(width < 640 ? 44 : 36);
    expect(createBox.height).toBe(width < 640 ? 44 : 36);
    expect(Math.abs(createBox.x + createBox.width - ((await summary.boundingBox())!.x + (await summary.boundingBox())!.width))).toBeLessThanOrEqual(1);
    await expect(page.getByRole("button", { name: "Navigate in fixture", exact: true })).toHaveCount(0);
    await expect(pages).toBeVisible();
    await expect(pages.getByRole("link")).toHaveCount(4);
    for (const [label, path] of [["Graph", "graph"], ["Public links", "publications"], ["Members", "members"], ["Settings", "settings"]]) {
      await expect(pages.getByRole("link", { name: label, exact: true })).toHaveAttribute("href", `/vault/fixture/${path}`);
    }
    await expect(page.getByRole("navigation", { name: "Vault sections", exact: true })).toHaveCount(0);
    await expect(summary.getByRole("button", { name: "Copy akb://fixture" })).toBeVisible();
    await expect(summary.getByText("owner", { exact: true })).toBeVisible();
    await expect(aside.getByRole("region", { name: "Contents", exact: true })).toHaveCount(0);
    await expect(aside.getByRole("region")).toHaveCount(2);
    const access = summary.getByRole("region", { name: "Access and ownership", exact: true });
    const guide = summary.getByRole("region", { name: "Vault guide", exact: true });
    await expect(access.getByRole("button", { name: "Vault access: Private", exact: true })).toBeVisible();
    await expect(access).toContainText("Review owner");
    await expect(guide.getByRole("link", { name: "Open vault guide", exact: true })).toHaveAttribute("href", "/vault/fixture/doc/overview%2Fvault-skill.md");
    await expect(aside.getByRole("region", { name: "Vault guide", exact: true })).toHaveCount(0);
    await expect(aside.getByRole("region", { name: "Access and ownership", exact: true })).toHaveCount(0);
    await expect(summary.getByText("Use this vault for architecture decisions, operational procedures, and service ownership.", { exact: true })).toHaveCount(0);
    await expect(aside.getByRole("region", { name: "Recent activity", exact: true })).toBeVisible();
    await expect(collections.getByRole("list", { name: "Collections", exact: true }).getByRole("link")).toHaveCount(2);
    await expect(collections.getByRole("link", { name: "Open collection guides", exact: true })).toHaveAttribute("href", "/vault/fixture?collection=guides");
    await expect(aside.getByRole("region", { name: "Tables", exact: true })).toHaveCount(0);
    const availableRems = await page.getByRole("region", { name: "fixture Vault overview", exact: true }).evaluate(element => {
      const style = getComputedStyle(element);
      const width = element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      return width / parseFloat(getComputedStyle(document.documentElement).fontSize);
    });
    const contextBesideCollections = availableRems >= 56;
    const sections = await aside.locator(":scope > *").all();
    for (let index = 1; index < sections.length; index++) {
      const previous = (await sections[index - 1].boundingBox())!;
      const current = (await sections[index].boundingBox())!;
      const gap = current.y - previous.y - previous.height;
      expect(gap).toBeGreaterThanOrEqual(12);
      expect(gap).toBeLessThanOrEqual(16);
      expect(current.x).toBe(previous.x);
      expect(current.width).toBeCloseTo(previous.width, 0);
    }
    const link = recent.getByRole("link", { name: /Operations Runbook/ });
    await expect(link).toHaveAttribute("href", "/vault/fixture/doc/guides%2Fteam%2Fresource-0.md");
    await expect(link).toContainText("guides / team");
    await expect(recent).not.toContainText("abc1234");
    const mainBox = (await collections.boundingBox())!;
    const asideBox = (await aside.boundingBox())!;
    expect(await collections.evaluate(element => Boolean(element.compareDocumentPosition(
      document.querySelector('aside[aria-label="Vault overview details"]')!,
    ) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
    if (contextBesideCollections) {
      expect(Math.abs(mainBox.y - asideBox.y)).toBeLessThanOrEqual(1);
      expect(asideBox.width).toBeCloseTo(256, 0);
      expect(asideBox.x).toBeGreaterThanOrEqual(mainBox.x + mainBox.width + 23);
      expect(mainBox.width).toBeGreaterThan(500);
    } else {
      expect(asideBox.y).toBeGreaterThan(mainBox.y + mainBox.height);
      expect(Math.abs(asideBox.width - mainBox.width)).toBeLessThanOrEqual(1);
    }
    const contentsBox = (await contents.boundingBox())!;
    await expectSummaryUsesAvailableWidth(summary);
    const actions = summary.getByRole("group", { name: "Create content" });
    if (await actions.count()) {
      const actionsBox = (await actions.boundingBox())!;
      expect(actionsBox.y + actionsBox.height).toBeLessThanOrEqual(contentsBox.y);
    }
    // Counts and page destinations stay inside the compact summary.
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
    const members = pages.getByRole("link", { name: "Members", exact: true });
    await expect(members).toHaveAccessibleDescription("5 members");
    await expect(members).toContainText("5");
    await expect(aside.getByText("Members", { exact: true })).toHaveCount(0);
    expect(await summary.evaluate(element => Boolean(element.compareDocumentPosition(
      document.querySelector('[aria-label="Collection workspace"]')!,
    ) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("vault-overview.png"), fullPage: true });
  });
}

for (const width of [874, 1280]) test(`Overview description uses available header width at ${width}px`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 1000 });
  const description = "Keep architecture decisions, operating guides, incident learnings, and service ownership together so teams can find the context behind each change. Describe the decisions and procedures clearly, preserve the evidence that supports them, and link the source material so the next person can continue the work with confidence.";
  await fixture(page, { description });
  await page.goto("/vault/fixture");
  const summary = page.getByRole("region", { name: "Vault summary", exact: true });
  const paragraph = summary.getByText(description, { exact: true });
  const trigger = summary.getByRole("button", { name: "New content", exact: true });
  await expect(paragraph).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  const textBox = (await paragraph.boundingBox())!;
  const actionBox = (await trigger.boundingBox())!;
  // The identity reserves only its normal gap and the action at the right.
  expect(Math.abs(actionBox.x - textBox.x - textBox.width - 12)).toBeLessThanOrEqual(1);
  expect(await paragraph.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  expect(actionBox.y).toBeLessThan(textBox.y);
  const fit = await expectSummaryUsesAvailableWidth(summary);
  expect(fit.fits).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("vault-header-paragraph.png"), fullPage: true });
});

test("Overview creation stays available with inline navigation and keeps collection defaults", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const inventory = await fixture(page);
  await page.goto("/vault/fixture?collection=guides");
  const summary = page.getByRole("region", { name: "Vault summary", exact: true });
  const trigger = summary.getByRole("button", { name: "New content", exact: true });
  await expect(page.locator('#vault-workspace-navigation')).toBeVisible();
  await expect(trigger).toBeVisible();
  await expect(trigger).toHaveText("");
  await trigger.focus();
  await page.keyboard.press("Enter");
  const menu = page.getByRole("menu");
  await expect(menu.getByRole("menuitem")).toHaveText(["New document", "New collection", "New table", "Upload file"]);
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();

  await trigger.click();
  await page.getByRole("menuitem", { name: "New document", exact: true }).click();
  const document = page.getByRole("dialog", { name: "New document", exact: true });
  await expect(document).toBeVisible();
  await expect(document.getByLabel(/^Collection/)).toHaveValue("guides");
  await document.getByRole("button", { name: "Close document composer", exact: true }).click();
  await expect(document).toHaveCount(0);
  await expect(trigger).toBeFocused();

  for (const [item, name] of [["New table", "Create a table"], ["Upload file", "Upload a file"]]) {
    await trigger.click();
    await page.getByRole("menuitem", { name: item, exact: true }).click();
    const dialog = page.getByRole("dialog", { name, exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel(/^Collection/)).toHaveValue("guides");
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toBeFocused();
  }

  await trigger.click();
  await page.getByRole("menuitem", { name: "New collection", exact: true }).click();
  const collection = page.getByRole("dialog", { name: "New collection", exact: true });
  await expect(collection.getByLabel("Path", { exact: true })).toHaveValue("guides/");
  await collection.getByLabel("Path", { exact: true }).fill("guides/decisions");
  await collection.getByRole("button", { name: "Create", exact: true }).click();
  await expect(collection).toHaveCount(0);
  await expect(page).toHaveURL(/\/vault\/fixture\?collection=guides$/);
  expect(inventory.createdCollections()).toEqual(["guides/decisions"]);
  await expect(summary.getByRole("region", { name: "Contents", exact: true }).getByText("Collections", { exact: true }).locator("..")).toContainText("9");
  await expect(page.getByRole("region", { name: "Collection workspace", exact: true }).getByRole("link", { name: "Open collection guides/decisions", exact: true })).toBeVisible();
  await expect(trigger).toBeFocused();
});

test("Overview uses available workspace width when the navigator is folded", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page, { folded: true });
  await page.goto("/vault/fixture");
  const collections = page.getByRole("region", { name: "Collection workspace", exact: true });
  const aside = page.getByRole("complementary", { name: "Vault overview details" });
  await expect(aside).toBeVisible();
  expect(Math.abs((await collections.boundingBox())!.y - (await aside.boundingBox())!.y)).toBeLessThanOrEqual(1);
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
  const owner = page.getByRole("region", { name: "Vault summary", exact: true }).getByText("A very long owner name that must remain readable without clipping", { exact: true });
  await owner.scrollIntoViewIfNeeded();
  await expect(owner).toBeVisible();
  expect(await owner.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  for (const target of await aside.getByRole("link").all()) {
    expect(await target.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  }
  await page.screenshot({ path: testInfo.outputPath("vault-context-text-scale.png"), animations: "disabled" });
});

for (const dark of [false, true]) test(`Header metadata stays readable and opens access details in ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1920, height: 1000 });
  await fixture(page, { dark });
  await page.goto("/vault/fixture");
  const summary = page.getByRole("region", { name: "Vault summary", exact: true });
  const aside = page.getByRole("complementary", { name: "Vault overview details" });
  const access = summary.getByRole("region", { name: "Access and ownership", exact: true });
  const guideSection = summary.getByRole("region", { name: "Vault guide", exact: true });
  const guide = guideSection.getByRole("link", { name: "Open vault guide", exact: true });
  await expect(guide).toHaveAccessibleDescription("Guide customized");
  await expect(aside.getByRole("region")).toHaveCount(2);
  await expect(aside.getByRole("heading", { name: "Vault guide", exact: true })).toHaveCount(0);
  await expect(aside.getByRole("heading", { name: "Access and ownership", exact: true })).toHaveCount(0);
  await expect(guideSection.getByRole("heading")).toHaveCount(0);
  await expect(access.getByRole("heading")).toHaveCount(0);
  const contents = summary.getByRole("region", { name: "Contents", exact: true });
  for (const metadata of [access, guideSection]) {
    const box = (await metadata.boundingBox())!;
    expect(box.y + box.height).toBeLessThanOrEqual((await contents.boundingBox())!.y);
  }
  // Normal-size metadata and the member count must meet AA in both themes.
  const contrast = await summary.locator('section[aria-label="Access and ownership"] button, section[aria-label="Access and ownership"] p > span, section[aria-label="Vault guide"] a, nav[aria-label="Vault pages"] a[aria-label="Members"], nav[aria-label="Vault pages"] a[aria-label="Members"] > span').evaluateAll(elements => {
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
  expect(contrast.length).toBeGreaterThanOrEqual(6);
  for (const sample of contrast) expect(sample.ratio, sample.label || "Header metadata contrast").toBeGreaterThanOrEqual(4.5);
  const before = (await guide.boundingBox())!;
  expect(before.height).toBeGreaterThanOrEqual(32);
  const resting = await guide.evaluate(el => getComputedStyle(el).color);
  await guide.hover();
  await expect.poll(() => guide.evaluate(el => getComputedStyle(el).color)).not.toBe(resting);
  await guide.focus();
  await expect(guide).toBeFocused();
  expect(await guide.evaluate(el => getComputedStyle(el).boxShadow)).not.toBe("none");
  const after = (await guide.boundingBox())!;
  expect(after.width).toBe(before.width);
  expect(after.height).toBe(before.height);
  const members = summary.getByRole("navigation", { name: "Vault pages", exact: true }).getByRole("link", { name: "Members", exact: true });
  await expect(members).toHaveAccessibleDescription("5 members");
  const membersBox = (await members.boundingBox())!;
  expect(membersBox.height).toBeGreaterThanOrEqual(44);
  const restingColor = await members.evaluate(el => getComputedStyle(el).color);
  await members.hover();
  await expect.poll(() => members.evaluate(el => getComputedStyle(el).color)).not.toBe(restingColor);
  await members.focus();
  await expect(members).toBeFocused();
  expect(await members.evaluate(el => parseFloat(getComputedStyle(el).outlineWidth))).toBeGreaterThan(0);
  expect(await members.boundingBox()).toEqual(membersBox);

  const trigger = access.getByRole("button", { name: "Vault access: Private", exact: true });
  await trigger.focus();
  await page.keyboard.press("Enter");
  const policy = page.getByRole("dialog", { name: "Vault access", exact: true });
  await expect(policy).toBeVisible();
  await expect(policy).toContainText("Only people with access can open this vault.");
  await expect(policy).toContainText("Public links are managed separately and may allow access without signing in.");
  await expect(policy.getByRole("button", { name: "Close vault access", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(policy).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("vault-header-metadata.png"), animations: "disabled" });
});

test("Guide keeps its native URL and opens a preview over the current Overview", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture?collection=guides");
  const guide = page.getByRole("region", { name: "Vault summary", exact: true }).getByRole("link", { name: "Open vault guide", exact: true });
  await expect(guide).toHaveAttribute("href", "/vault/fixture/doc/overview%2Fvault-skill.md");
  await guide.click();
  await expect(page).toHaveURL(/\/vault\/fixture\/doc\/overview%2Fvault-skill\.md$/);
  const preview = page.getByRole("dialog", { name: "Document preview", exact: true });
  await expect(preview).toBeVisible();
  await expect(preview).toContainText("Use this vault for architecture decisions, operational procedures, and service ownership.");
  await preview.getByRole("button", { name: "Close dialog", exact: true }).click();
  await expect(preview).toHaveCount(0);
  await expect(page).toHaveURL(/\/vault\/fixture\?collection=guides$/);
  await expect(guide).toBeFocused();
});

for (const width of [375, 1440]) test(`Collection selection keeps main contents readable at ${width}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const inventory = await fixture(page);
  await page.goto("/vault/fixture");
  const workspace = page.getByRole("region", { name: "Collection workspace", exact: true });
  await workspace.getByRole("link", { name: "Open collection guides", exact: true }).click();
  await expect(page).toHaveURL(/\?collection=guides$/);
  await expect(workspace.getByRole("heading", { name: "guides", exact: true })).toBeVisible();
  await expect(workspace.getByRole("link", { name: "Open collection guides/operations", exact: true })).toBeVisible();
  const resources = workspace.getByRole("region", { name: "Collection resources" });
  await expect(resources.getByRole("link", { name: "Runbook document", exact: true })).toHaveAttribute("href", "/vault/fixture/doc/guides%2Frunbook.md");
  await expect(resources.getByRole("link", { name: "services table", exact: true })).toHaveAttribute("href", "/vault/fixture/table/services");
  await expect(resources.getByRole("link", { name: "diagram.svg file", exact: true })).toHaveAttribute("href", "/vault/fixture/file/7ead8d7b-2861-4d64-8190-4cab55372b91");
  if (width < 1024) await expect(page.getByRole("button", { name: "Open vault navigation", exact: true })).toHaveAttribute("aria-expanded", "false");
  const back = workspace.getByRole("link", { name: "All collections", exact: true });
  if (width < 640) expect((await back.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  const browsesBeforeReturn = inventory.browses();
  await back.click();
  await expect(page).toHaveURL(/\/vault\/fixture$/);
  const recent = workspace.getByRole("navigation", { name: "Recently visited collections" });
  await expect(recent.getByRole("link", { name: "Open collection guides", exact: true })).toBeVisible();
  await page.goBack();
  await expect(workspace.getByRole("heading", { name: "guides", exact: true })).toBeVisible();
  await expect(resources).toBeVisible();
  expect(inventory.browses()).toBe(browsesBeforeReturn);
});
