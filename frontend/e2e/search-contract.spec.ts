// Real-browser search interaction contract with mode-owned HTTP fixtures.
// Does not replace the live-backend login/indexing E2E suite.
import { test, expect } from "@playwright/test";

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
  { width: 667, height: 375 },
]) {
  for (const dark of [false, true]) {
    test(`search filters, history and exact options at ${viewport.width}px (${dark ? "dark" : "light"})`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(() =>
        localStorage.setItem("akb_token", "browser-contract-fixture"),
      );
      const requests: URL[] = [];
      page.on("request", (request) => {
        const url = new URL(request.url());
        if (url.pathname.endsWith("/search") || url.pathname.endsWith("/grep")) {
          requests.push(url);
        }
      });
      if (process.env.AKB_FE_E2E_MODE !== "mock") {
        await page.route("**/api/v1/**", async (route) => {
          const url = new URL(route.request().url());
          if (url.pathname.endsWith("/auth/config"))
            return route.fulfill({
              json: {
                schema_version: 2,
                auth_mode: "local",
                local_auth: { enabled: true },
                keycloak: { enabled: false, browser_session_ready: false },
                providers: [],
                mcp_oauth: { enabled: false },
              },
            });
          if (url.pathname.endsWith("/auth/me"))
            return route.fulfill({
              json: {
                user_id: "fixture-user",
                username: "fixture",
                email: "fixture@example.invalid",
                display_name: "Fixture user",
                is_admin: false,
                auth_method: "local",
                key_class: null,
              },
            });
          if (url.pathname.endsWith("/search"))
            return route.fulfill({
              json: {
                query: "deployment",
                archive_scope: url.searchParams.get("archive_scope") || "unarchived",
                total: 1,
                returned: 1,
                total_matches: 30,
                truncated: true,
                results: [
                  {
                    title: url.searchParams.has("doc_types")
                      ? "Report outside initial top 25"
                      : "Initial result",
                    uri: "akb://fixture/doc/report.md",
                    vault: "fixture",
                    path: "report.md",
                    doc_type: "report",
                    source_type: "document",
                    summary: "Deploy the workspace safely: review configuration, start the services, and verify that your team can access its knowledge.",
                    score: 1,
                  },
                ],
              },
            });
          if (url.pathname.endsWith("/grep"))
            return route.fulfill({
              json: {
                pattern: "deployment",
                archive_scope: url.searchParams.get("archive_scope") || "unarchived",
                regex: url.searchParams.get("regex") === "true",
                total_docs: 0,
                total_matches: url.searchParams.has("include_text_files") ? 1 : 0,
                returned_docs: 0,
                total_resources: url.searchParams.has("include_text_files") ? 1 : 0,
                returned_resources: url.searchParams.has("include_text_files") ? 1 : 0,
                returned_matches: url.searchParams.has("include_text_files") ? 1 : 0,
                results: url.searchParams.has("include_text_files") ? [{
                  uri: "akb://fixture/file/f-text", vault: "fixture", path: "deploy.txt",
                  title: "Deployment text File", resource_type: "file", revision: "revision-1",
                  matches: [{ text: "deployment", line: 2, section: null }],
                }] : [],
              },
            });
          return route.fulfill({ json: { vaults: [], items: [], total: 0 } });
        });
      }
      await page.goto("/search?q=deployment");
      await page.evaluate(
        (isDark) => document.documentElement.classList.toggle("dark", isDark),
        dark,
      );
      await expect(page.getByText("Initial result")).toBeVisible();
      const query = page.getByRole("searchbox", { name: "Search query" });
      await expect(query).toHaveCount(1);
      await expect(page.getByRole("button", { name: "All", exact: true })).toHaveCSS("text-align", "left");
      const inactiveModeBorder = await page.getByRole("button", { name: "Literal", exact: true }).evaluate((button) => getComputedStyle(button).borderBottomColor);
      await expect(page.getByRole("button", { name: "Semantic", exact: true })).not.toHaveCSS("border-bottom-color", inactiveModeBorder);
      await expect(page.getByTestId("search-workspace").getByRole("searchbox", { name: "Search query" })).toBeVisible();
      await expect(page.locator(".app-header").getByRole("searchbox", { name: "Search query" })).toHaveCount(0);
      await expect(page.locator(".app-header")).toHaveAttribute("data-surface", "paper");
      await expect(page.getByRole("button", { name: "Search knowledge", exact: true })).toBeVisible();
      if (viewport.width >= 1024) await expect(page.getByRole("navigation", { name: "Current page" })).toHaveText("Search");
      const headerBox = await page.locator(".app-header").boundingBox();
      const queryBox = await query.boundingBox();
      expect(queryBox!.y).toBeGreaterThanOrEqual(headerBox!.y + headerBox!.height);
      await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
      const quickSearch = page.getByTestId("global-search-dialog");
      await expect(quickSearch).toBeVisible();
      await quickSearch.getByRole("combobox").fill("separate lookup");
      await expect(page).toHaveURL(/\/search\?q=deployment$/);
      await page.keyboard.press("Escape");
      await expect(quickSearch).toHaveCount(0);
      await expect(query).toHaveValue("deployment");
      await expect(page.getByRole("button", { name: "Search knowledge", exact: true })).toBeFocused();
      const rail = await page.getByRole("complementary", { name: "Search refinements" }).boundingBox();
      const results = await page.getByRole("region", { name: "Search results" }).boundingBox();
      if (!rail || !results) throw new Error("Search workspace is not visible");
      if (viewport.width > 1000) expect(rail.x + rail.width).toBeLessThanOrEqual(results.x + 1);
      else expect(rail.y + rail.height).toBeLessThanOrEqual(results.y + 1);
      await page.screenshot({ path: testInfo.outputPath("search-results.png"), animations: "disabled" });
      await page
        .getByRole("button", { name: "More filters" })
        .click();
      await page.getByRole("button", { name: "Toggle report" }).click();
      await expect(
        page.getByText("Report outside initial top 25"),
      ).toBeVisible();
      await expect(page).toHaveURL(/doc_type=report/);
      await page.reload();
      await page.evaluate(
        (isDark) => document.documentElement.classList.toggle("dark", isDark),
        dark,
      );
      await expect(
        page.getByText("Report outside initial top 25"),
      ).toBeVisible();
      await page
        .getByRole("button", { name: "Literal", pressed: false })
        .click();
      await page
        .getByRole("button", { name: "More filters" })
        .click();
      // Document metadata filters exclude Files; clear the semantic report filter.
      await page.getByRole("button", { name: "Toggle report" }).click();
      // URL navigation is a React transition; wait for its controlled state.
      for (const label of [
        "Include text Files",
        "Regular expression",
        "Case sensitive",
      ]) {
        await page.getByRole("checkbox", { name: label, exact: true }).click();
        await expect(page.getByRole("checkbox", { name: label, exact: true })).toBeChecked();
      }
      await page.getByRole("button", { name: "Document state", exact: true }).click();
      await page.getByRole("menuitemradio", { name: "All documents", exact: true }).click();
      await expect(page.getByRole("button", { name: "Document state", exact: true })).toHaveText("All documents");
      await expect
        .poll(() => requests.at(-1)?.searchParams.get("archive_scope"))
        .toBe("all");
      expect(requests.at(-1)?.searchParams.get("include_text_files")).toBe("true");
      expect(requests.at(-1)?.searchParams.get("regex")).toBe("true");
      expect(requests.at(-1)?.searchParams.get("case_sensitive")).toBe("true");
      expect(requests.at(-1)?.searchParams.getAll("doc_types")).toEqual([]);
      await expect(page.getByRole("link", { name: /Deployment text File/ })).toHaveAttribute("href", "/vault/fixture/file/f-text");
      await expect(page.getByText("1 resource · 1 match")).toBeVisible();
      await expect(page.getByText("Body line 2")).toBeVisible();
      await page.reload();
      await page.evaluate((isDark) => document.documentElement.classList.toggle("dark", isDark), dark);
      await expect(page.getByText("Deployment text File")).toBeVisible();
      await page.getByRole("button", { name: "More filters" }).click();
      await expect(page.getByLabel("Include text Files")).toBeChecked();
      await page.goBack();
      await expect(
        page.getByRole("button", { name: "Document state", exact: true }),
      ).toHaveText("Current documents");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: test.info().outputPath("search-filters.png"),
        fullPage: true,
        animations: "disabled",
      });
      await page.getByRole("button", { name: "Clear filters", exact: true }).click();
      await page.getByRole("button", { name: "More filters" }).click();
      await expect(page.getByRole("heading", { name: /No results/ })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("search-no-results.png"), animations: "disabled" });
      await page.getByRole("button", { name: "Clear search query" }).click();
      await expect(page.getByRole("heading", { name: "Find your next starting point" })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("search-start.png"), animations: "disabled" });
    });
  }
}
