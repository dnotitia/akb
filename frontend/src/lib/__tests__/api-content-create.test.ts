import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import {
  createVaultTable,
  setToken,
  uploadVaultFile,
  type FileUploadStage,
} from "@/lib/api";

const server = setupServer();

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
  setToken("content-create-test-token");
});
afterEach(() => server.resetHandlers());
afterAll(() => {
  setToken(null);
  server.close();
});

describe("Vault content creation contracts", () => {
  it("completes the reserve, transfer, and confirm file-upload flow", async () => {
    const stages: FileUploadStage[] = [];
    const fileId = "4f9d5609-c6c7-4b62-a8be-79308c18cb8d";

    server.use(
      http.post(`*/api/v1/files/ops/upload`, ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("filename")).toBe("runbook.pdf");
        expect(url.searchParams.get("collection")).toBe("guides");
        expect(url.searchParams.get("description")).toBe("Incident response source");
        expect(url.searchParams.get("mime_type")).toBe("application/pdf");
        return HttpResponse.json({
          kind: "file",
          uri: `akb://ops/coll/guides/file/${fileId}`,
          upload_url: `http://storage.test/upload/${fileId}`,
          deduplicated: false,
        });
      }),
      http.put(`http://storage.test/upload/${fileId}`, ({ request }) => {
        expect(request.headers.get("content-type")).toBe("application/pdf");
        return new HttpResponse(null, { status: 200 });
      }),
      http.post(`*/api/v1/files/ops/${fileId}/confirm`, () =>
        HttpResponse.json({
          kind: "file",
          uri: `akb://ops/coll/guides/file/${fileId}`,
          vault: "ops",
          name: "runbook.pdf",
          collection: "guides",
          mime_type: "application/pdf",
          size_bytes: 7,
        }),
      ),
    );

    const result = await uploadVaultFile(
      "ops",
      new File(["runbook"], "runbook.pdf", { type: "application/pdf" }),
      {
        collection: "guides",
        description: "Incident response source",
        onStageChange: (stage) => stages.push(stage),
      },
    );

    expect(stages).toEqual(["preparing", "uploading", "confirming"]);
    expect(result.name).toBe("runbook.pdf");
    expect(result.uri).toContain(`/file/${fileId}`);
  });

  it("posts the table schema to the Vault table endpoint", async () => {
    server.use(
      http.post(`*/api/v1/tables/ops`, async ({ request }) => {
        expect(await request.json()).toEqual({
          name: "incidents",
          description: "Operational incidents",
          collection: "operations",
          columns: [
            { name: "status", type: "text", required: true, unique: false },
          ],
        });
        return HttpResponse.json({
          kind: "table",
          uri: "akb://ops/coll/operations/table/incidents",
          vault: "ops",
          name: "incidents",
        });
      }),
    );

    const result = await createVaultTable("ops", {
      name: "incidents",
      description: "Operational incidents",
      collection: "operations",
      columns: [
        { name: "status", type: "text", required: true, unique: false },
      ],
    });

    expect(result.name).toBe("incidents");
  });
});
