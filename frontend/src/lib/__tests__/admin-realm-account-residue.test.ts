import { readFileSync } from "node:fs";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type AdminRealmAccountResidue,
  getAdminRealmAccountResidue,
  parseAdminRealmAccountResidue,
} from "../api";


// The backend model test validates this same file, so both sides agree on it.
const available = JSON.parse(readFileSync(
  "../backend/tests/fixtures/admin_sso_realm_account_residue_v1.json",
  "utf8",
)) as Extract<AdminRealmAccountResidue, { state: "available" }>;
const [residueAccount, , pendingAccount] = available.accounts;

const unavailable = {
  schema_version: 1,
  state: "unavailable",
  unavailable_code: "realm_accounts_unavailable",
  scan_limit: 1000,
  truncated: null,
  realm_account_count: null,
  bound_count: null,
  pending_arrival_count: null,
  residue_count: null,
  accounts: null,
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("realm account residue API", () => {
  it("reads the versioned payload from the product-admin surface", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(response(available)));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getAdminRealmAccountResidue()).toEqual(available);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/admin/sso/realm-account-residue");
    expect(init.method ?? "GET").toBe("GET");
    expect(init.credentials).toBe("same-origin");
  });

  it("keeps an unavailable read free of any count", () => {
    const parsed = parseAdminRealmAccountResidue(unavailable);
    expect(parsed.state).toBe("unavailable");
    expect(parsed.residue_count).toBeNull();
    expect(parsed.accounts).toBeNull();
  });

  it("surfaces an HTTP refusal as an error, not as an empty payload", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(
      response({ detail: "Admin session required" }, 401),
    )));
    await expect(getAdminRealmAccountResidue()).rejects.toMatchObject({ status: 401 });
  });

  it.each([
    ["extra root field", { ...available, arbitrary: true }],
    ["missing root field", (({ truncated: _truncated, ...rest }) => rest)(available)],
    ["future schema", { ...available, schema_version: 2 }],
    ["unknown state", { ...available, state: "partial" }],
    ["unavailable with a zero count", { ...unavailable, residue_count: 0 }],
    ["unavailable with an empty list", { ...unavailable, accounts: [] }],
    ["unavailable without a code", { ...unavailable, unavailable_code: null }],
    ["available with a code", { ...available, unavailable_code: "realm_accounts_unavailable" }],
    ["partitions that do not add up", { ...available, residue_count: available.residue_count + 1 }],
    ["more accounts read than the scan bound", { ...available, scan_limit: 5 }],
    ["negative count", { ...available, bound_count: -1, realm_account_count: available.realm_account_count - 10 }],
    ["fractional count", { ...available, bound_count: 8.5, residue_count: 2.5 }],
    ["truncation that is not a boolean", { ...available, truncated: "no" }],
    ["more listed than unbound", {
      ...available,
      residue_count: 1,
      bound_count: available.bound_count + 1,
    }],
    ["more listed residue than counted", {
      ...available,
      accounts: [residueAccount, { ...pendingAccount, arrival: null }, available.accounts[1]],
      residue_count: 2,
    }],
    ["duplicate realm account", { ...available, accounts: [residueAccount, residueAccount] }],
    ["extra account field", { ...available, accounts: [{ ...residueAccount, email: "x@example.com" }] }],
    ["unsafe username", { ...available, accounts: [{ ...residueAccount, username: "two\nlines" }] }],
    ["empty realm user id", { ...available, accounts: [{ ...residueAccount, realm_user_id: "" }] }],
    ["unparseable creation time", { ...available, accounts: [{ ...residueAccount, created_at: "yesterday" }] }],
    ["arrival with an extra field", {
      ...available,
      accounts: [{ ...pendingAccount, arrival: { ...pendingAccount.arrival, subject: "s" } }],
    }],
    ["arrival id that is not a UUID", {
      ...available,
      accounts: [{ ...pendingAccount, arrival: { ...pendingAccount.arrival, admission_id: "1" } }],
    }],
  ])("refuses a payload with %s", (_name, payload) => {
    expect(() => parseAdminRealmAccountResidue(payload)).toThrow("Invalid realm account residue response");
  });

  it("accepts a bounded list shorter than the unbound count", () => {
    const parsed = parseAdminRealmAccountResidue({ ...available, accounts: [residueAccount] });
    expect(parsed.accounts).toHaveLength(1);
    expect(parsed.residue_count).toBe(2);
  });
});
