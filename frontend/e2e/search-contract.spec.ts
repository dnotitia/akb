// Real-browser search interaction contract with mode-owned HTTP fixtures.
// Does not replace the live-backend login/indexing E2E suite.
import { test, expect } from "@playwright/test";

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
  { width: 320, height: 740 },
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
      await expect(page.getByRole("button", { name: "Result type", exact: true })).toHaveText("All content");
      await expect(page.getByRole("button", { name: "Search method", exact: true })).toHaveText("By meaning");
      const entry = page.getByTestId("search-query-entry");
      await expect(entry.getByRole("searchbox", { name: "Search query" })).toBeVisible();
      await expect(entry.getByRole("button", { name: "Search", exact: true })).toBeVisible();
      await expect(entry.getByRole("button", { name: "Search method", exact: true })).toHaveCount(1);
      await expect(page.getByRole("group", { name: "Filter by content kind" })).toHaveCount(0);
      await expect(page.getByTestId("search-workspace").getByRole("searchbox", { name: "Search query" })).toBeVisible();
      await expect(page.locator(".app-header").getByRole("searchbox", { name: "Search query" })).toHaveCount(0);
      await expect(page.locator(".app-header")).toHaveAttribute("data-surface", "paper");
      await expect(page.getByRole("button", { name: "Search knowledge", exact: true })).toBeVisible();
      if (viewport.width >= 1024) await expect(page.getByRole("navigation", { name: "Current page" })).toHaveText("Search");
      const headerBox = await page.locator(".app-header").boundingBox();
      const queryBox = await query.boundingBox();
      expect(queryBox!.y).toBeGreaterThanOrEqual(headerBox!.y + headerBox!.height);
      const titleBox = (await page.getByRole("heading", { name: "Search", exact: true }).boundingBox())!;
      const entryBox = (await entry.boundingBox())!;
      if (viewport.width >= 1024) {
        expect(titleBox.width).toBeLessThanOrEqual(1);
        expect(entryBox.y - headerBox!.y - headerBox!.height).toBeLessThanOrEqual(25);
      } else {
        expect(titleBox.height).toBeGreaterThanOrEqual(20);
        expect(titleBox.y + titleBox.height).toBeLessThanOrEqual(entryBox.y);
      }
      await query.click();
      await expect(query).toBeFocused();
      const focusRingClearance = await entry.evaluate(element => {
        const field = element.getBoundingClientRect();
        const clearance: number[] = [];
        for (let parent = element.parentElement; parent; parent = parent.parentElement) {
          const style = getComputedStyle(parent);
          const bounds = parent.getBoundingClientRect();
          if (/hidden|clip|auto|scroll/.test(style.overflowX)) {
            clearance.push(field.left - bounds.left, bounds.right - field.right);
          }
          if (/hidden|clip|auto|scroll/.test(style.overflowY)) {
            clearance.push(field.top - bounds.top, bounds.bottom - field.bottom);
          }
        }
        return clearance;
      });
      // The shared field has a 2px focus ring beyond a 2px offset.
      for (const clearance of focusRingClearance) expect(clearance).toBeGreaterThanOrEqual(4);
      await page.screenshot({ path: testInfo.outputPath("search-query-focused.png"), animations: "disabled" });
      await page.getByRole("button", { name: "Search knowledge", exact: true }).click();
      const quickSearch = page.getByTestId("global-search-dialog");
      await expect(quickSearch).toBeVisible();
      await quickSearch.getByRole("combobox").fill("separate lookup");
      await expect(page).toHaveURL(/\/search\?q=deployment$/);
      await page.keyboard.press("Escape");
      await expect(quickSearch).toHaveCount(0);
      await expect(query).toHaveValue("deployment");
      await expect(page.getByRole("button", { name: "Search knowledge", exact: true })).toBeFocused();
      await expect(page.getByRole("complementary", { name: "Search refinements" })).toHaveCount(0);
      const results = await page.getByRole("region", { name: "Search results" }).boundingBox();
      const controls = await page.getByTestId("search-tool-row").boundingBox();
      if (!controls || !results) throw new Error("Search workspace is not visible");
      expect(controls.y + controls.height).toBeLessThanOrEqual(results.y + 1);
      const submit = (await entry.getByRole("button", { name: "Search", exact: true }).boundingBox())!;
      const queryEntry = (await entry.boundingBox())!;
      expect(submit.x).toBeGreaterThan(queryEntry.x);
      expect(submit.x + submit.width).toBeLessThan(queryEntry.x + queryEntry.width);
      expect(submit.y).toBeGreaterThanOrEqual(queryEntry.y);
      const commandFrame = (await page.getByTestId("search-command-header").boundingBox())!;
      const resultFrame = (await page.getByTestId("search-results-panel").boundingBox())!;
      expect(Math.abs(resultFrame.x - commandFrame.x)).toBeLessThanOrEqual(1);
      expect(Math.abs(resultFrame.x + resultFrame.width - commandFrame.x - commandFrame.width)).toBeLessThanOrEqual(1);
      await expect(page.getByRole("button", { name: "Filters", exact: true })).toBeInViewport();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath("search-results.png"), animations: "disabled" });
      await page
        .getByRole("button", { name: "Filters", exact: true })
        .click();
      await page.getByRole("button", { name: "Toggle report" }).click();
      await page.getByRole("button", { name: "Done", exact: true }).click();
      await expect(
        page.getByText("Report outside initial top 25"),
      ).toBeVisible();
      await expect(page.getByRole("button", { name: "Filters", exact: true })).toBeFocused();
      await expect(page).toHaveURL(/doc_type=report/);
      await page.reload();
      await page.evaluate(
        (isDark) => document.documentElement.classList.toggle("dark", isDark),
        dark,
      );
      await expect(
        page.getByText("Report outside initial top 25"),
      ).toBeVisible();
      await page.getByRole("button", { name: "Search method", exact: true }).click();
      const methodMenu = (await page.getByRole("menu").boundingBox())!;
      expect(methodMenu.x).toBeGreaterThanOrEqual(8);
      expect(methodMenu.x + methodMenu.width).toBeLessThanOrEqual(viewport.width - 8);
      await page.getByRole("menuitemradio", { name: /^Exact text/ }).click();
      await page
        .getByRole("button", { name: "Filters", exact: true })
        .click();
      // Document metadata filters exclude Files; clear the semantic report filter.
      await page.getByRole("button", { name: "Toggle report" }).click();
      await expect(page.getByRole("button", { name: "Toggle report" })).toHaveAttribute("aria-pressed", "false");
      await expect(page).not.toHaveURL(/doc_type=report/);
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
      await page.getByRole("button", { name: "Done", exact: true }).click();
      await expect(page.getByRole("link", { name: /Deployment text File/ })).toHaveAttribute("href", "/vault/fixture/file/f-text");
      await expect(page.getByText("1 resource · 1 match")).toBeVisible();
      await expect(page.getByText("Body line 2")).toBeVisible();
      await page.reload();
      await page.evaluate((isDark) => document.documentElement.classList.toggle("dark", isDark), dark);
      await expect(page.getByText("Deployment text File")).toBeVisible();
      await page.getByRole("button", { name: "Filters", exact: true }).click();
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
      await page.getByRole("button", { name: "Done", exact: true }).click();
      await page.getByRole("button", { name: "Clear filters", exact: true }).click();
      await expect(page.getByRole("heading", { name: /No results/ })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("search-no-results.png"), animations: "disabled" });
      await page.getByRole("button", { name: "Clear search query" }).click();
      await expect(page.getByRole("heading", { name: "Suggested searches" })).toBeVisible();
      const readyPane = page.getByTestId("search-results-pane");
      const readyPaneBox = (await readyPane.boundingBox())!;
      const readyContentBox = (await page.getByTestId("search-start-state").boundingBox())!;
      const readyGeometry = await readyPane.evaluate(element => ({ width: element.clientWidth, scrollTop: element.scrollTop }));
      expect(Math.abs(readyContentBox.x - readyPaneBox.x)).toBeLessThanOrEqual(1);
      expect(Math.abs(readyContentBox.y + readyGeometry.scrollTop - readyPaneBox.y)).toBeLessThanOrEqual(1);
      expect(readyContentBox.width).toBe(readyGeometry.width);
      await page.screenshot({ path: testInfo.outputPath("search-start.png"), animations: "disabled" });
    });
  }
}
