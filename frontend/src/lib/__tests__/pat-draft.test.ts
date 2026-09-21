import { describe, expect, it } from "vitest";
import { defaultPatDraft, draftSummary, parseLocalExpiration, replacementDraft, validatePatDraft } from "../pat-draft";

describe("PAT draft authority", () => {
  it("retains name-only defaults and normalizes Unicode before validating codepoints", () => {
    const draft = { ...defaultPatDraft(), name: " e\u0301🪸 " };
    expect(validatePatDraft(draft, 2).intent).toEqual({ name: "é🪸", scopes: ["read", "write"], vault_scope: null });
    expect(draftSummary(defaultPatDraft())).toContain("No expiration · Read and write · No additional Vault write restriction");
  });
  it("never turns an empty selected scope into unrestricted issuance", () => {
    const result = validatePatDraft({ ...defaultPatDraft(), name: "x", restricted: true });
    expect(result.intent).toBeUndefined(); expect(result.errors.vault_scope).toBeTruthy();
  });
  it("keeps Vault restrictions when permissions become read-only and canonicalizes selections", () => {
    const result = validatePatDraft({ ...defaultPatDraft(), name: "x", restricted: true, permissions: "read", vaults: ["z", "a", "z"], prefixes: ["team-", "team-"] });
    expect(result.intent).toEqual({ name: "x", scopes: ["read"], vault_scope: { prefixes: ["team-"], extra_vaults: ["a", "z"] } });
  });
  it.each(["team-*", "UPPER", "/team", "-start"])("rejects invalid scope %s", value => {
    expect(validatePatDraft({ ...defaultPatDraft(), name: "x", restricted: true, prefixes: [value] }).errors.vault_scope).toBeTruthy();
  });
  it("preserves exact replacement expiration including microseconds", () => {
    const expiry = "2030-11-03T01:30:05.123456-04:00";
    const draft = replacementDraft({ token_id: "a", name: "x", prefix: "akb_", key_class: "pat", scopes: ["read"], vault_scope: { prefixes: ["team-"], extra_vaults: [] }, expires_at: expiry })!;
    expect(validatePatDraft(draft, 255, Date.parse("2026-01-01")).intent?.expires_at).toBe(expiry);
    expect(validatePatDraft({ ...draft, permissions: "read-write" }, 255, Date.parse("2026-01-01")).intent?.vault_scope).toEqual({ prefixes: ["team-"], extra_vaults: [] });
  });
  it("does not extend an expired original or broaden unfamiliar scopes", () => {
    const draft = replacementDraft({ token_id: "a", name: "x", prefix: "akb_", key_class: "pat", scopes: ["write"], vault_scope: null, expires_at: "2020-01-01T00:00:00Z" })!;
    const checked = validatePatDraft(draft);
    expect(checked.errors.scopes).toBeTruthy(); expect(checked.errors.expires_at).toBeTruthy(); expect(checked.intent).toBeUndefined();
  });
  it("requires complete PAT metadata for replacement", () => {
    expect(replacementDraft({ token_id: "a", name: "x", prefix: "akb_", scopes: ["read"], expires_at: null })).toBeNull();
  });
  it("rejects non-calendar custom dates rather than Date overflow", () => { expect(parseLocalExpiration("2030-02-31T10:00")).toBeNull(); });
  it.each(["0", "-1", "1.5", "true"])("rejects invalid relative expiry %s", expiration => {
    expect(validatePatDraft({ ...defaultPatDraft(), name: "x", expiration }).intent).toBeUndefined();
  });
});


it("resolves local times under the configured timezone, rejecting repeated and skipped DST times", () => {
  if (Intl.DateTimeFormat().resolvedOptions().timeZone === "America/New_York") {
    expect(parseLocalExpiration("2030-03-10T02:30")).toBeNull();
    expect(parseLocalExpiration("2030-11-03T01:30")).toBeNull();
    expect(parseLocalExpiration("2030-11-03T02:30")).toBe("2030-11-03T07:30:00.000Z");
  } else {
    expect(parseLocalExpiration("2030-01-15T12:30")).toBe(new Date("2030-01-15T12:30").toISOString());
  }
});
