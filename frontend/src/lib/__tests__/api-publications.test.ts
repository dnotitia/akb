import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createPublication,
  createPublicationSnapshot,
  previewTablePublicationQuery,
  listPublicationDirectory,
} from "@/lib/api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("publication API", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("passes server filters and cancellation to the bounded directory", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ publications: [], total: 0, complete: true, next_cursor: null }));
    const signal = new AbortController().signal;
    await listPublicationDirectory({ vault: "team vault", q: "100% & guide", status: "unavailable", expiry: "soon", page_size: 30, cursor: "next+page", signal });
    const [path, options] = fetchMock.mock.calls[0];
    const url = new URL(path, "https://example.test");
    expect(url.pathname).toBe("/api/v1/publications");
    expect(Object.fromEntries(url.searchParams)).toEqual({ vault: "team vault", q: "100% & guide", status: "unavailable", expiry: "soon", page_size: "30", cursor: "next+page" });
    expect(options.signal).toBe(signal);
  });

  it("previews a generated table query through the authenticated SQL endpoint", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        kind: "table_query",
        columns: ["ticket_id"],
        items: [{ ticket_id: "t-1" }],
        total: 1,
      }),
    );

    const result = await previewTablePublicationQuery(
      "team vault",
      "SELECT ticket_id FROM tickets LIMIT 100",
    );

    expect(result.total).toBe(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/tables/team%20vault/sql",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ sql: "SELECT ticket_id FROM tickets LIMIT 100" }),
      }),
    );
  });

  it("creates a file publication and snapshots a table publication by slug", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ slug: "file-link" }))
      .mockResolvedValueOnce(jsonResponse({ slug: "table-link", mode: "snapshot" }));

    await createPublication("demo", {
      resource_type: "file",
      uri: "akb://demo/file/11111111-1111-1111-1111-111111111111",
    });
    await createPublicationSnapshot("demo", "table/link");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/v1/publications/demo/create",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/v1/publications/demo/table/link/snapshot",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
