import { expect, test } from "@playwright/test";

test.skip(process.env.AKB_FE_E2E_MODE === "mock", "Owns read-only HTTP fixtures; never writes user data.");

for (const width of [375, 2560]) for (const dark of [false, true]) {
  test(`Search page selected Vaults wrap and survive reload at ${width}px ${dark ? "dark" : "light"}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.addInitScript(({ dark }) => {
      localStorage.setItem("akb_token", "multi-vault-search-fixture");
      localStorage.setItem("akb_theme", dark ? "dark" : "light");
    }, { dark });
    const searches: URL[] = [];
    await page.route("**/health/**", route => route.fulfill({ json: {} }));
    await page.route("**/api/v1/**", route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path.endsWith("/auth/config")) return route.fulfill({ json: { schema_version: 2, auth_mode: "local", local_auth: { enabled: true }, keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false } } });
      if (path.endsWith("/auth/me")) return route.fulfill({ json: { user_id: "scope-reader", username: "reader", display_name: "Scope reader", email: "reader@example.invalid", auth_method: "local", is_admin: false } });
      if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: ["alpha", "beta", "gamma"].map(name => ({ id: name, name, role: "reader" })) } });
      if (path.endsWith("/info")) return route.fulfill({ json: { name: "alpha", role: "reader", document_count: 0, collection_count: 0 } });
      if (path.includes("/browse/")) return route.fulfill({ json: { vault: "alpha", items: [] } });
      if (path.endsWith("/search")) {
        searches.push(url);
        return route.fulfill({ json: { results: [{ source_type: "file", title: "Guide checklist", vault: "beta", path: "checklist.txt", uri: "akb://beta/file/file-1", score: 1 }], total: 1, returned: 1 } });
      }
      return route.fulfill({ json: { items: [], history: [], relations: [], subscribed: false, unread_count: 0 } });
    });

    await page.goto("/vault/alpha/search?q=guide&source=file");
    await page.getByRole("button", { name: "Search scope: alpha", exact: true }).click();
    await page.getByRole("searchbox", { name: "Filter vaults" }).fill("bet");
    await page.getByRole("menuitemcheckbox", { name: "beta", exact: true }).click();
    await page.keyboard.press("Escape");
    await expect.poll(() => searches.at(-1)?.searchParams.getAll("vault")).toEqual(["alpha", "beta"]);
    expect(searches.at(-1)?.searchParams.get("source_type")).toBe("file");
    const query = page.getByRole("searchbox", { name: "Search query", exact: true });
    await expect(query).toHaveValue("guide");
    const command = page.getByTestId("search-command-header");
    const removeAlpha = command.getByRole("button", { name: "Remove alpha from search scope" });
    const removeBeta = command.getByRole("button", { name: "Remove beta from search scope" });
    await expect(removeAlpha).toBeInViewport();
    await expect(removeBeta).toBeInViewport();
    await expect(query).toBeInViewport();
    const queryBox = (await query.boundingBox())!;
    const submitBox = (await command.getByRole("button", { name: "Search", exact: true }).boundingBox())!;
    expect(queryBox.x + queryBox.width <= submitBox.x).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("search-multi-vault.png") });

    await page.reload();
    await expect(page.getByRole("button", { name: "Search scope: alpha, beta", exact: true })).toBeVisible();
    await removeAlpha.click();
    await expect(page.getByRole("button", { name: "Search scope: beta", exact: true })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole("button", { name: "Search scope: alpha, beta", exact: true })).toBeVisible();
    await page.goForward();
    await expect(page.getByRole("button", { name: "Search scope: beta", exact: true })).toBeVisible();
    await removeBeta.click();
    await expect.poll(() => searches.at(-1)?.searchParams.getAll("vault")).toEqual([]);
    expect(new URL(page.url()).searchParams.has("v")).toBe(true);
    expect(new URL(page.url()).searchParams.get("v")).toBe("");
    await page.reload();
    await expect(page.getByRole("button", { name: "Search scope: All vaults", exact: true })).toBeVisible();
    await expect.poll(() => searches.at(-1)?.searchParams.getAll("vault")).toEqual([]);

    await query.fill("bet");
    await page.getByRole("button", { name: "Add beta to search scope", exact: true }).click();
    await expect(query).toHaveValue("");
    await expect(query).toBeFocused();
    await expect(page.getByRole("button", { name: "Remove beta from search scope" })).toBeVisible();
    expect(new URL(page.url()).searchParams.get("source")).toBe("file");
    await query.fill("new guide");
    await command.getByRole("button", { name: "Search", exact: true }).click();
    await expect.poll(() => searches.at(-1)?.searchParams.getAll("vault")).toEqual(["beta"]);

    // Header quick lookup has its own transient scope and hands multiple Vaults
    // to the full page without mutating the launching page's saved query.
    await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
    const dialog = page.getByRole("dialog");
    const modalQuery = dialog.getByRole("combobox");
    await expect(dialog.getByRole("button", { name: "Remove alpha from search scope" })).toBeVisible();
    await modalQuery.fill("bet");
    await dialog.getByRole("button", { name: "Add beta to search scope", exact: true }).click();
    await expect(modalQuery).toHaveValue("");
    await modalQuery.fill("modal guide");
    await dialog.getByRole("button", { name: /^Files(?:,|$)/ }).click();
    await expect.poll(() => searches.at(-1)?.searchParams.getAll("vault")).toEqual(["alpha", "beta"]);
    await expect.poll(() => searches.at(-1)?.searchParams.get("q")).toBe("modal guide");
    expect(searches.at(-1)?.searchParams.get("source_type")).toBe("file");
    await expect(modalQuery).toBeInViewport();
    await expect(dialog.getByRole("button", { name: "Remove alpha from search scope" })).toBeInViewport();
    await expect(dialog.getByRole("button", { name: "Remove beta from search scope" })).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("modal-multi-vault.png") });
    await dialog.getByRole("button", { name: "Continue in search page" }).click();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/search");
    await expect(query).toHaveValue("modal guide");
    expect(new URL(page.url()).searchParams.get("v")).toBe("alpha,beta");
    expect(new URL(page.url()).searchParams.get("source")).toBe("file");
    await expect(page.getByRole("button", { name: "Search scope: alpha, beta", exact: true })).toBeVisible();
    await page.goBack();
    await expect.poll(() => new URL(page.url()).pathname).toBe("/vault/alpha/search");
    await expect(query).toHaveValue("new guide");
    await expect(page.getByRole("button", { name: "Search scope: beta", exact: true })).toBeVisible();
  });
}
