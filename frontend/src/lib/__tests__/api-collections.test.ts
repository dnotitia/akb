import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createCollection,
  deleteCollection,
  type CollectionNotEmptyDetail,
} from "@/lib/api";

// Helper: build a Response with a JSON body.
function jsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    status: init?.status ?? 200,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
}

// jsdom doesn't expose `location.pathname` in a writeable way out of the box,
// but the api() helper only touches `location` on a 401 response. None of these
// tests hit that branch, so we don't need to stub it.

describe("createCollection", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("POSTs and returns the parsed result", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        ok: true,
        created: true,
        collection: {
          path: "design/specs",
          name: "specs",
          summary: "ui specs",
          doc_count: 0,
        },
      }),
    );

    const result = await createCollection("v1", "design/specs", "ui specs");

    expect(result.created).toBe(true);
    expect(result.collection.path).toBe("design/specs");
    expect(result.collection.doc_count).toBe(0);

    // Verify request shape.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/collections/v1");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ path: "design/specs", summary: "ui specs" });
  });
});

describe("deleteCollection — empty / happy path", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("returns the deletion summary on 200", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        ok: true,
        collection: "scratch",
        deleted_docs: 0,
        deleted_files: 0,
      }),
    );

    const result = await deleteCollection("v1", "scratch", false);

    expect(result.deleted_docs).toBe(0);
    expect(result.deleted_files).toBe(0);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/collections/v1/scratch");
    expect(fetchMock.mock.calls[0][1].method).toBe("DELETE");
  });

  it("appends ?recursive=true when recursive is set", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ok: true, collection: "deep", deleted_docs: 3, deleted_files: 1 }),
    );
    await deleteCollection("v1", "deep", true);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/collections/v1/deep?recursive=true");
  });
});

describe("deleteCollection — 409 non-empty", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("throws ApiError carrying doc_count / file_count", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            message: "Collection is not empty",
            doc_count: 4,
            file_count: 2,
          },
        },
        { status: 409 },
      ),
    );

    await expect(deleteCollection("v1", "design", false)).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
    });

    // Re-call to inspect the structured detail (mocks are one-shot).
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            message: "Collection is not empty",
            doc_count: 4,
            file_count: 2,
          },
        },
        { status: 409 },
      ),
    );
    try {
      await deleteCollection("v1", "design", false);
      throw new Error("expected ApiError");
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      const apiErr = e as ApiError<CollectionNotEmptyDetail>;
      expect(apiErr.status).toBe(409);
      expect(apiErr.detail.doc_count).toBe(4);
      expect(apiErr.detail.file_count).toBe(2);
      expect(apiErr.message).toBe("Collection is not empty");
    }
  });

  it("falls back to plain Error when detail is a string", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "boom" }, { status: 400 }),
    );
    try {
      await deleteCollection("v1", "x", false);
      throw new Error("expected throw");
    } catch (e) {
      expect(e).toBeInstanceOf(Error);
      expect(e).not.toBeInstanceOf(ApiError);
      expect((e as Error).message).toBe("boom");
    }
  });
});

describe("deleteCollection — URL encoding", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("encodes segments individually so '/' stays as separator", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ok: true, collection: "a b/c", deleted_docs: 0, deleted_files: 0 }),
    );
    await deleteCollection("v", "a b/c", false);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/collections/v/a%20b/c");
  });

  it("encodes the vault segment too", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ok: true, collection: "x", deleted_docs: 0, deleted_files: 0 }),
    );
    await deleteCollection("my vault", "x", false);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/collections/my%20vault/x");
  });
});
