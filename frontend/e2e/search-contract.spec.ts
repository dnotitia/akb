// Real-browser search interaction contract with intercepted HTTP fixtures.
// Does not replace the live-backend login/indexing E2E suite.
import { test, expect } from "@playwright/test";

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  for (const dark of [false, true]) {
    test(`search filters, history and exact options at ${viewport.width}px (${dark ? "dark" : "light"})`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(() =>
        localStorage.setItem("akb_token", "browser-contract-fixture"),
      );
      const requests: URL[] = [];
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
        if (url.pathname.endsWith("/search")) {
          requests.push(url);
          const title = url.searchParams.has("doc_types")
            ? "Report outside initial top 25"
            : "Initial result";
          return route.fulfill({
            json: {
              query: "deployment",
              total: 1,
              returned: 1,
              total_matches: 30,
              truncated: true,
              results: [
                {
                  title,
                  uri: "akb://fixture/doc/report.md",
                  vault: "fixture",
                  path: "report.md",
                  doc_type: "report",
                  source_type: "document",
                  score: 1,
                },
              ],
            },
          });
        }
        if (url.pathname.endsWith("/grep")) {
          requests.push(url);
          return route.fulfill({
            json: {
              pattern: "deployment",
              regex: url.searchParams.get("regex") === "true",
              total_docs: 0,
              total_matches: 0,
              returned_docs: 0,
              returned_matches: 0,
              results: [],
            },
          });
        }
        return route.fulfill({ json: { vaults: [], items: [], total: 0 } });
      });
      await page.goto("/search?q=deployment");
      await page.evaluate(
        (isDark) => document.documentElement.classList.toggle("dark", isDark),
        dark,
      );
      await expect(page.getByText("Initial result")).toBeVisible();
      await page
        .getByRole("button", { name: "Filter by document type" })
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
        .getByRole("button", { name: "Filter by document type" })
        .click();
      // URL navigation is a React transition; wait for its controlled state.
      for (const label of [
        "Regular expression",
        "Case sensitive",
        "Include archived documents",
      ]) {
        await page.getByLabel(label).click();
        await expect(page.getByLabel(label)).toBeChecked();
      }
      await expect
        .poll(() => requests.at(-1)?.searchParams.get("include_archived"))
        .toBe("true");
      expect(requests.at(-1)?.searchParams.get("regex")).toBe("true");
      expect(requests.at(-1)?.searchParams.get("case_sensitive")).toBe("true");
      expect(requests.at(-1)?.searchParams.getAll("doc_types")).toEqual([
        "report",
      ]);
      await page.goBack();
      await expect(
        page.getByLabel("Include archived documents"),
      ).not.toBeChecked();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: test.info().outputPath("search-filters.png"),
        fullPage: true,
      });
    });
  }
}
