import { expect, test } from "@playwright/test";

const FILE_TARGET = "akb://fixture/coll/notes/file/11111111-1111-4111-8111-111111111111";
const FILE_LABEL = "Searched File";

test.describe("shared Markdown link search in the document composer", () => {
  test.skip(
    process.env.AKB_FE_E2E_SCENARIO !== "markdown-reference-adapters",
    "The reference adapter scenario is selected by AKB_FE_E2E_SCENARIO",
  );

  test("saves and reopens a searched File link only after explicit Create", async ({ page, request }) => {
    await request.post("/__akb_mock__/reset", {
      data: { scenario: "markdown-reference-adapters" },
    });
    const initialStateResponse = await request.get("/__akb_mock__/fixture/state");
    expect(initialStateResponse.ok()).toBeTruthy();
    const initialState = await initialStateResponse.json();
    const baseCommit = initialState.document.current_commit as string;

    await page.goto("/vault/fixture/doc/new?collection=notes");
    const composer = page.getByRole("dialog", { name: "New document" });
    await expect(composer).toBeVisible();
    await page.locator("#doc-title").fill("File link E2E proof");
    await expect(page.locator("#doc-collection")).toHaveValue("notes");
    const createButton = composer.getByRole("button", { name: "Create document" });
    await expect(createButton).toBeDisabled();
    await expect(page.getByRole("button", { name: "Insert link" })).toBeEnabled();

    await page.evaluate(({ fileTarget, fileLabel, expectedCommit }) => {
      type E2EState = {
        searchQueries: string[];
        createBodies: Array<Record<string, unknown>>;
        formSubmitters: string[];
      };
      type E2EWindow = Window & { __markdownLinkSearchE2E?: E2EState };
      const testWindow = window as E2EWindow;
      const state: E2EState = { searchQueries: [], createBodies: [], formSubmitters: [] };
      testWindow.__markdownLinkSearchE2E = state;

      const form = document.querySelector<HTMLFormElement>("#doc-title")?.form;
      if (!form) throw new Error("DocumentCreateForm was not mounted");
      form.addEventListener("submit", (event) => {
        const submitter = event.submitter;
        state.formSubmitters.push(
          submitter instanceof HTMLElement
            ? submitter.getAttribute("aria-label") || submitter.textContent?.trim() || "unknown"
            : "unknown",
        );
      }, true);

      const originalFetch = window.fetch.bind(window);
      window.fetch = async (input, init) => {
        const rawUrl = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
        const url = new URL(rawUrl, window.location.href);
        const method = (init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();

        if (url.pathname === "/api/v1/search") {
          const query = url.searchParams.get("q") || "";
          state.searchQueries.push(query);
          return new Response(JSON.stringify({
            query,
            total: 1,
            returned: 1,
            total_matches: 1,
            results: [{
              uri: fileTarget,
              title: fileLabel,
              source_type: "file",
              matched_section: "existing fixture file",
            }],
          }), { status: 200, headers: { "Content-Type": "application/json" } });
        }

        if (url.pathname === "/api/v1/documents" && method === "POST") {
          const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
          state.createBodies.push(body);
          const commit = await originalFetch("/__akb_mock__/fixture/commit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ...body, expected_commit: expectedCommit, asset_ids: [] }),
          });
          if (!commit.ok) return commit;
          const committed = await commit.json() as { document: { current_commit: string } };
          return new Response(JSON.stringify({
            kind: "document_write",
            path: "notes/references.md",
            current_commit: committed.document.current_commit,
            commit_hash: committed.document.current_commit,
          }), { status: 201, headers: { "Content-Type": "application/json" } });
        }

        return originalFetch(input, init);
      };
    }, { fileTarget: FILE_TARGET, fileLabel: FILE_LABEL, expectedCommit: baseCommit });

    await page.getByRole("button", { name: "Insert link" }).click();
    const linkDialog = page.getByRole("dialog", { name: "Insert link" });
    const search = linkDialog.getByRole("combobox", { name: "Search Vault resources" });
    await search.fill("fixture-file-query");
    const option = linkDialog.getByRole("option", { name: `${FILE_LABEL} (file)` });
    await expect(option).toBeVisible();
    await search.press("ArrowDown");
    await search.press("Enter");
    await expect(linkDialog.getByLabel("URL")).toHaveValue(FILE_TARGET);
    await expect(linkDialog.getByLabel("Text")).toHaveValue(FILE_LABEL);
    await expect(page.locator('button[aria-label="Create document"]')).toBeDisabled();
    let state = await page.evaluate(() => (window as unknown as { __markdownLinkSearchE2E: { createBodies: unknown[]; formSubmitters: string[] } }).__markdownLinkSearchE2E);
    expect(state.createBodies).toHaveLength(0);
    expect(state.formSubmitters).toEqual([]);

    await linkDialog.getByRole("button", { name: "Insert link" }).click();
    await expect(linkDialog).not.toBeVisible();
    await expect(composer).toBeVisible();
    await expect(createButton).toBeEnabled();
    state = await page.evaluate(() => (window as unknown as { __markdownLinkSearchE2E: { createBodies: unknown[]; formSubmitters: string[] } }).__markdownLinkSearchE2E);
    expect(state.createBodies).toHaveLength(0);
    expect(state.formSubmitters).toEqual([]);

    await createButton.click();
    await expect.poll(async () =>
      page.evaluate(() => (window as unknown as { __markdownLinkSearchE2E: { createBodies: unknown[] } }).__markdownLinkSearchE2E.createBodies.length),
    ).toBe(1);
    state = await page.evaluate(() => (window as unknown as { __markdownLinkSearchE2E: { createBodies: Array<Record<string, unknown>>; formSubmitters: string[]; searchQueries: string[] } }).__markdownLinkSearchE2E);
    expect(state.createBodies[0]?.content).toContain(`[${FILE_LABEL}](${FILE_TARGET})`);
    expect(state.formSubmitters).toEqual(["Create document"]);
    expect(state.searchQueries).toContain("fixture-file-query");
    await expect(composer).not.toBeVisible();
    const savedStateResponse = await request.get("/__akb_mock__/fixture/state");
    expect(savedStateResponse.ok()).toBeTruthy();
    const savedState = await savedStateResponse.json();
    expect(savedState.document.title).toBe("File link E2E proof");
    expect(savedState.document.content).toContain(`[${FILE_LABEL}](${FILE_TARGET})`);

    const reopenedRaw = `/vault/fixture/doc/notes%2Freferences.md?view=raw`;
    await page.goto(reopenedRaw);
    const rawMarkdown = page.getByTestId("doc-raw");
    await expect(rawMarkdown).toContainText(`[${FILE_LABEL}](${FILE_TARGET})`);
    await expect(page.getByRole("heading", { name: "File link E2E proof", exact: true })).toBeVisible();
    expect(await rawMarkdown.textContent()).not.toContain("signed.example");
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Document body (markdown)" })).toBeVisible();
  });
});
