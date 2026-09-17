import { beforeEach, describe, expect, it, vi } from "vitest";
import { moveDocument } from "@/lib/api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("document move API", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("posts the destination fields to an encoded document move endpoint", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        kind: "document_write",
        uri: "akb://team vault/coll/archive/doc/final-spec.md",
        vault: "team vault",
        path: "archive/final-spec.md",
        commit_hash: "abc1234",
        current_commit: "abc1234",
        action: "moved",
      }),
    );

    const result = await moveDocument("team vault", "drafts/spec v1.md", {
      collection: "archive",
      slug: "final spec",
      message: "Move the approved spec",
    });

    expect(result.path).toBe("archive/final-spec.md");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/documents/team%20vault/drafts%2Fspec%20v1.md/move",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          collection: "archive",
          slug: "final spec",
          message: "Move the approved spec",
        }),
      }),
    );
  });
});
