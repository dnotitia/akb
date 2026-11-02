import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { parseAdminSsoOperationRequests, parseAdminSsoOwnership } from "@/lib/api";

// The same document the backend validates against its declared response model.
const fixture = JSON.parse(readFileSync(
  "../backend/tests/fixtures/admin_sso_operation_requests_v1.json",
  "utf8",
)) as { schema_version: 1; requests: Record<string, unknown>[] };

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

describe("parseAdminSsoOperationRequests", () => {
  it("accepts the shared backend fixture", () => {
    const requests = parseAdminSsoOperationRequests(fixture);
    expect(requests.map((request) => [request.request.kind, request.status])).toEqual([
      ["ownership_claim", "pending"],
      ["provider_grant", "pending"],
      ["provider_grant", "approved"],
    ]);
    expect(requests[1].request.grant?.configure_summary?.client_secret_supplied).toBe(true);
  });

  it("refuses a response that carries anything beyond the secret-free shape", () => {
    for (const [path, key] of [[[], "credential"], [["request"], "credential"], [["request", "grant"], "sealed_credential"]] as const) {
      const changed = clone(fixture);
      let target: Record<string, unknown> = changed.requests[1];
      for (const step of path) target = target[step] as Record<string, unknown>;
      target[key] = "akb_sso_operation_never_rendered";
      expect(() => parseAdminSsoOperationRequests(changed)).toThrow();
    }
  });

  it("refuses inconsistent requests", () => {
    const kindMismatch = clone(fixture);
    (kindMismatch.requests[0].request as Record<string, unknown>).kind = "provider_grant";
    const idMismatch = clone(fixture);
    (idMismatch.requests[1].request as Record<string, unknown>).request_id = fixture.requests[0].request_id;
    const configureWithoutSummary = clone(fixture);
    ((configureWithoutSummary.requests[1].request as Record<string, Record<string, unknown>>).grant).configure_summary = null;
    const hashesForOtherVerbs = clone(fixture);
    ((hashesForOtherVerbs.requests[2].request as Record<string, Record<string, unknown>>).grant).provider_payload_sha256 = { enable: "a".repeat(64) };
    const undecidedApproval = clone(fixture);
    undecidedApproval.requests[2].decided_by = null;
    const duplicate = { ...fixture, requests: [fixture.requests[0], fixture.requests[0]] };
    for (const value of [kindMismatch, idMismatch, configureWithoutSummary, hashesForOtherVerbs, undecidedApproval, duplicate]) {
      expect(() => parseAdminSsoOperationRequests(value)).toThrow();
    }
  });
});

describe("parseAdminSsoOwnership", () => {
  const ownership = {
    schema_version: 1,
    target_id: "0b7e1a33-58f1-4f2f-8f3e-2b9c6d7e8a02",
    provider_alias: "workforce",
    owner_id: "controller-instance",
    revision: 3,
    fingerprint: "b".repeat(64),
    provider_secret_revision: "secret-v2", // pragma: allowlist secret
    drifted: false,
    emergency_disabled: true,
  };

  it("accepts an owned, latched provider", () => {
    expect(parseAdminSsoOwnership(ownership)).toEqual(ownership);
  });

  it("refuses unknown keys and malformed values", () => {
    for (const value of [
      { ...ownership, extra: true },
      { ...ownership, fingerprint: "B".repeat(64) },
      { ...ownership, revision: -1 },
      { ...ownership, owner_id: "line\nbreak" },
      { ...ownership, emergency_disabled: "yes" },
    ]) {
      expect(() => parseAdminSsoOwnership(value)).toThrow();
    }
  });
});
