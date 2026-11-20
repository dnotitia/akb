import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { parseAdminSsoOperationRequest, parseAdminSsoOperationRequests, parseAdminSsoOwnership } from "@/lib/api";

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

  it("accepts the optional service-request UUIDs independently and on claims and grants", () => {
    const fields = {
      request_nonce: "12345678-1234-4567-89ab-123456789abc",
      expected_realm_id: "abcdefab-1234-4567-89ab-123456789abc",
    };
    for (const key of Object.keys(fields) as (keyof typeof fields)[]) {
      const changed = clone(fixture);
      (changed.requests[0].request as Record<string, unknown>)[key] = fields[key];
      expect(parseAdminSsoOperationRequests(changed)[0].request[key]).toBe(fields[key]);
    }
    const changed = clone(fixture);
    for (const [index, entry] of changed.requests.entries()) {
      Object.assign(entry.request as object, {
        ...fields,
        request_nonce: `12345678-1234-4567-89ab-123456789ab${index}`,
      });
    }
    const parsed = parseAdminSsoOperationRequests(changed);
    expect(parsed[0].request).toMatchObject({ ...fields, request_nonce: "12345678-1234-4567-89ab-123456789ab0" });
    expect(parsed[1].request).toMatchObject({ ...fields, request_nonce: "12345678-1234-4567-89ab-123456789ab1" });
    expect(parseAdminSsoOperationRequest(changed.requests[0])).toEqual(parsed[0]);
    expect(parseAdminSsoOperationRequest(changed.requests[1])).toEqual(parsed[1]);
    expect(parseAdminSsoOperationRequests(fixture)[0].request).not.toHaveProperty("request_nonce");
    expect(parseAdminSsoOperationRequests(fixture)[0].request).not.toHaveProperty("expected_realm_id");
  });

  it("rejects malformed optional UUIDs and unknown keys even with the new fields", () => {
    const fields = ["request_nonce", "expected_realm_id"] as const;
    for (const key of fields) {
      for (const value of [null, undefined, 42, {}, "", "not-a-uuid", "ABCDEFAB-1234-4567-89AB-123456789ABC"]) {
        for (const index of [0, 1]) {
          const changed = clone(fixture);
          (changed.requests[index].request as Record<string, unknown>)[key] = value;
          expect(() => parseAdminSsoOperationRequests(changed)).toThrow();
          expect(() => parseAdminSsoOperationRequest(changed.requests[index])).toThrow();
        }
      }
    }
    for (const extra of ["credential", "unexpected_field"]) {
      const changed = clone(fixture);
      Object.assign(changed.requests[1].request as object, {
        request_nonce: "12345678-1234-4567-89ab-123456789abc",
        expected_realm_id: "abcdefab-1234-4567-89ab-123456789abc",
        [extra]: "never accepted",
      });
      expect(() => parseAdminSsoOperationRequests(changed)).toThrow();
    }
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
