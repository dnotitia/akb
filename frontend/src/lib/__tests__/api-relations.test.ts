import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRelation, deleteRelation } from "@/lib/api";

function jsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    status: init?.status ?? 200,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
}

const SRC = "akb://v1/coll/notes/doc/alpha.md";
const TGT = "akb://v1/coll/notes/doc/beta.md";

describe("createRelation", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });
  afterEach(() => vi.restoreAllMocks());

  it("POSTs to /relations with the full source/target/relation body", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ linked: true, source: SRC, target: TGT, relation: "references" }),
    );

    const result = await createRelation(SRC, TGT, "references");

    expect(result.linked).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/relations");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ source: SRC, target: TGT, relation: "references" });
  });

  it("includes metadata when provided", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ linked: true }));
    await createRelation(SRC, TGT, "depends_on", { note: "x" });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.metadata).toEqual({ note: "x" });
  });
});

describe("deleteRelation", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });
  afterEach(() => vi.restoreAllMocks());

  it("DELETEs with source+target+relation in the query string", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ unlinked: 1, source: SRC, target: TGT }));

    const result = await deleteRelation(SRC, TGT, "references");

    expect(result.unlinked).toBe(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe("DELETE");
    const parsed = new URL(url, "http://x");
    expect(parsed.pathname).toBe("/api/v1/relations");
    expect(parsed.searchParams.get("source")).toBe(SRC);
    expect(parsed.searchParams.get("target")).toBe(TGT);
    expect(parsed.searchParams.get("relation")).toBe("references");
  });

  it("omits `relation` from the query when not given (drop-all-edges path)", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ unlinked: 2, source: SRC, target: TGT }));

    await deleteRelation(SRC, TGT);

    const url = fetchMock.mock.calls[0][0];
    const parsed = new URL(url, "http://x");
    expect(parsed.searchParams.has("relation")).toBe(false);
    expect(parsed.searchParams.get("source")).toBe(SRC);
  });
});
