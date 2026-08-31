import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import {
  deleteVaultTableRow,
  insertVaultTableRow,
  listVaultTableRows,
  setToken,
  updateVaultTableRow,
} from "@/lib/api";

const server = setupServer();
const rowId = "6ab163e8-6ea4-4d20-8765-bf912716384c";

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
  setToken("table-row-test-token");
});
afterEach(() => server.resetHandlers());
afterAll(() => {
  setToken(null);
  server.close();
});

describe("Vault table row API contracts", () => {
  it("loads a bounded row page with an exact total", async () => {
    server.use(
      http.get(`*/api/v1/tables/ops/incidents/rows`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("limit")).toBe("50");
        expect(url.searchParams.get("offset")).toBe("0");
        expect(url.searchParams.get("order")).toBe("created_at.desc");
        expect(request.headers.get("prefer")).toBe("count=exact");
        return HttpResponse.json({
          kind: "table_query",
          columns: ["id", "title"],
          items: [{ id: rowId, title: "API outage" }],
          total: 1,
        });
      }),
    );

    const result = await listVaultTableRows("ops", "incidents");
    expect(result.total).toBe(1);
    expect(result.items[0].id).toBe(rowId);
  });

  it("inserts a row through the structured writer endpoint", async () => {
    server.use(
      http.post(`*/api/v1/tables/ops/incidents/rows`, async ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("select")).toBe("*");
        expect(request.headers.get("prefer")).toBe("return=representation");
        expect(await request.json()).toEqual({ title: "API outage", severity: "high" });
        return HttpResponse.json(
          {
            kind: "table_query",
            columns: ["id", "title", "severity"],
            items: [{ id: rowId, title: "API outage", severity: "high" }],
            total: 1,
          },
          { status: 201 },
        );
      }),
    );

    const result = await insertVaultTableRow("ops", "incidents", {
      title: "API outage",
      severity: "high",
    });
    expect(result.items[0].severity).toBe("high");
  });

  it("updates and deletes one row through an id filter", async () => {
    server.use(
      http.patch(`*/api/v1/tables/ops/incidents/rows`, async ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("id")).toBe(`eq.${rowId}`);
        expect(url.searchParams.get("select")).toBe("*");
        expect(await request.json()).toEqual({ severity: "critical" });
        return HttpResponse.json({
          kind: "table_query",
          columns: ["id", "severity"],
          items: [{ id: rowId, severity: "critical" }],
          total: 1,
        });
      }),
      http.delete(`*/api/v1/tables/ops/incidents/rows`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("id")).toBe(`eq.${rowId}`);
        expect(url.searchParams.get("select")).toBe("id");
        expect(request.headers.get("prefer")).toBe("return=representation");
        return HttpResponse.json({
          kind: "table_query",
          columns: ["id"],
          items: [{ id: rowId }],
          total: 1,
        });
      }),
    );

    await updateVaultTableRow("ops", "incidents", rowId, { severity: "critical" });
    const deleted = await deleteVaultTableRow("ops", "incidents", rowId);
    expect(deleted.items).toEqual([{ id: rowId }]);
  });
});
