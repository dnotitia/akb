// Integration-style tests for /api/v1 search + grep + vault_info that
// hit a Mock Service Worker handler instead of the real backend. Catch
// contract drift: every time the backend changes a response shape the
// frontend type / hook expects, these fail fast.
//
// Why MSW (over module-mocking @/lib/api):
//   - Verifies the actual `fetch` + JSON.parse round-trip, including
//     URL building and querystring construction in api.ts.
//   - Forces handlers to match the wire shape — a typo in a field
//     name surfaces here, not in production.

import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { setupServer } from "msw/node";
import { http, HttpResponse } from "msw";

import { setToken, searchDocs, grepDocs, getVaultInfo } from "@/lib/api";

const API = "http://localhost/api/v1";

const server = setupServer();
// MSW v2: `"error"` runs through the experimental frame path and the
// "Cannot bypass a request" guard fires on handler matches that the
// frame can't categorize. `"warn"` keeps the same fast feedback (the
// test still fails on missing fields) without that infrastructure quirk.
beforeAll(() => server.listen({ onUnhandledRequest: "warn" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

// api.ts builds `${API_BASE}/...` with API_BASE = "/api/v1". MSW resolves
// requests by URL — since jsdom uses `http://localhost`, prefix accordingly.

describe("search response contract — returned vs total_matches (PR #39)", () => {
  it("exposes returned + total_matches alongside the legacy `total` alias", async () => {
    server.use(
      http.get(`*/api/v1/search`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("q")).toBe("postgres");
        expect(url.searchParams.get("limit")).toBe("3");
        return HttpResponse.json({
          query: "postgres",
          total: 3,            // legacy alias of `returned`
          returned: 3,         // post-limit
          total_matches: 17,   // pre-limit population
          results: [{ uri: "akb://v/doc/x.md", title: "X" }],
        });
      }),
    );
    setToken("fake-jwt");
    const resp = await searchDocs("postgres", undefined, 3);
    expect(resp.total).toBe(3);
    expect(resp.returned).toBe(3);
    expect(resp.total_matches).toBe(17);
    expect(resp.results).toHaveLength(1);
  });

  it("survives legacy backends that omit the new fields (frontend reads them as undefined, not throws)", async () => {
    server.use(
      http.get(`*/api/v1/search`, () =>
        HttpResponse.json({
          query: "x",
          total: 0,
          results: [],
        }),
      ),
    );
    const resp = await searchDocs("x");
    expect(resp.total).toBe(0);
    // TS shape claims these are numbers; at runtime an older backend
    // will deliver undefined. The frontend treats them as optional —
    // this test pins that the call resolves rather than throwing.
    expect(resp.returned).toBeUndefined();
    expect(resp.total_matches).toBeUndefined();
  });
});

describe("grep response contract — total_matches + total_docs", () => {
  it("returns the count fields for the default response shape", async () => {
    server.use(
      http.get(`*/api/v1/grep`, () =>
        HttpResponse.json({
          pattern: "shared_buffers",
          regex: false,
          total_docs: 2,
          total_matches: 5,
          results: [],
        }),
      ),
    );
    const resp = await grepDocs("shared_buffers");
    expect(resp.total_docs).toBe(2);
    expect(resp.total_matches).toBe(5);
  });
});

describe("vault_info contract — tables[] schema exposure (PR #34)", () => {
  it("returns the tables array with columns + row_count + jsonb hint", async () => {
    server.use(
      http.get(`*/api/v1/vaults/eng/info`, () =>
        HttpResponse.json({
          name: "eng",
          description: "Engineering",
          status: "active",
          is_archived: false,
          is_external_git: false,
          public_access: "none",
          role: "owner",
          role_source: "member",
          owner: "alice",
          owner_display_name: "Alice",
          member_count: 1,
          document_count: 0,
          table_count: 1,
          file_count: 0,
          edge_count: 0,
          tables: [
            {
              name: "incidents",
              row_count: 30,
              columns: [
                { name: "ir_number", type: "text", example: "IR-001" },
                {
                  name: "attack_groups",
                  type: "jsonb",
                  search_hint: "attack_groups::text ILIKE '%X%'",
                },
              ],
            },
          ],
          last_activity: null,
          last_active_user: null,
          created_at: "2026-05-01T00:00:00Z",
        }),
      ),
    );
    const resp = await getVaultInfo("eng");
    expect(resp.table_count).toBe(1);
    // `tables` isn't on the legacy type yet — read defensively.
    const tables = (resp as any).tables;
    expect(tables).toHaveLength(1);
    expect(tables[0].name).toBe("incidents");
    expect(tables[0].row_count).toBe(30);
    expect(tables[0].columns[0].example).toBe("IR-001");
    expect(tables[0].columns[1].search_hint).toMatch(/::text ILIKE/);
  });
});
