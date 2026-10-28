import { describe, expect, it } from "vitest";
import { ApiError, throwJsonApiError } from "@/lib/api";

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 409, headers: { "Content-Type": "application/json" } });
}

describe("JSON API error contract", () => {
  it("keeps the canonical code when the response also carries a legacy detail string", async () => {
    const body = { message: "The document changed", error: "The document changed", code: "conflict", detail: "The document changed" };
    await expect(throwJsonApiError(response(body))).rejects.toMatchObject({
      name: "ApiError", status: 409, message: body.message, detail: body,
    });
  });

  it("retains nested structured details for existing field-specific errors", async () => {
    const detail = { message: "A matching title exists", code: "document_title_conflict", existing_path: "notes/existing.md" };
    await expect(throwJsonApiError(response({ detail }))).rejects.toMatchObject({ status: 409, message: detail.message, detail });
  });

  it("retains a plain legacy error without inventing a conflict code", async () => {
    const error = await throwJsonApiError(response({ detail: "An unspecified conflict" })).catch(error => error);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ message: "An unspecified conflict", status: 409, detail: "An unspecified conflict" });
  });
});
