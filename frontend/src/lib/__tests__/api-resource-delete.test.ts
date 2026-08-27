import { beforeEach, describe, expect, it, vi } from "vitest";
import { deleteVaultFile, deleteVaultTable } from "@/lib/api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("resource delete API", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  it("deletes a file through its user-facing endpoint with encoded segments", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        kind: "file",
        vault: "team vault",
        name: "notes.txt",
        deleted: true,
      }),
    );

    const result = await deleteVaultFile("team vault", "file/id");

    expect(result.deleted).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/files/team%20vault/file%2Fid",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("deletes a table through the admin endpoint with encoded segments", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        kind: "table",
        uri: "akb://v/table/audit_log",
        vault: "v",
        name: "audit_log",
        deleted: true,
      }),
    );

    const result = await deleteVaultTable("v", "audit log");

    expect(result.deleted).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/tables/v/audit%20log",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("surfaces a table reference conflict without converting it to success", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "other vault tables reference it" }, 409),
    );

    await expect(deleteVaultTable("v", "parent")).rejects.toThrow(
      "other vault tables reference it",
    );
  });
});
