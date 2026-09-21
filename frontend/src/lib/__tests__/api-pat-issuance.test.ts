import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authSessionSnapshot, configureAuthTransport, setToken } from "../api";
import { getPatCapabilities, issuePat, UnverifiedPatReceipt, type PatIntent } from "../api-pat-issuance";

const uid = "00000000-0000-0000-0000-000000000001", tid = "00000000-0000-0000-0000-000000000002";
const caps = { contract_version: 1 as const, user_id: uid, name_max_length: 255, permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"], vault_scope_semantics: "write_restriction_sql_read_write" as const };
const intent: PatIntent = { name: "laptop", scopes: ["read"], vault_scope: { prefixes: ["team-"], extra_vaults: [] }, expires_days: 30 };
const receipt = { contract_version: 1, user_id: uid, token_id: tid, name: "laptop", token: "akb_fixture_secret", prefix: "akb_fixture", issued_at: "2030-01-01T12:03:04.123456Z", key_class: "pat", scopes: ["read"], vault_scope: intent.vault_scope, expires_at: "2030-01-31T12:03:04.123456Z" };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
beforeEach(() => { configureAuthTransport("local"); setToken("fixture-session"); });
afterEach(() => { vi.unstubAllGlobals(); document.cookie = "akb_dev_sso_csrf=; Max-Age=0; path=/"; setToken(null); configureAuthTransport(null); });
it.each([404, 405, 501])("only missing capability HTTP %s permits explicit legacy mode", async status => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("missing", { status })));
  expect(await getPatCapabilities(authSessionSnapshot())).toBe("legacy");
});
it.each([401, 403, 500])("never downgrades discovery failure HTTP %s", async status => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({}, status)));
  await expect(getPatCapabilities(authSessionSnapshot())).rejects.toMatchObject({ status });
});
it("validates capabilities and exact receipt restrictions using the server issuance clock", async () => {
  const fetch = vi.fn().mockResolvedValueOnce(json(caps)).mockResolvedValueOnce(json(receipt)); vi.stubGlobal("fetch", fetch);
  const snapshot = authSessionSnapshot();
  expect(await getPatCapabilities(snapshot)).toEqual(caps);
  expect((await issuePat(snapshot, caps, intent)).verified).toBe(true);
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ contract_version: 1, expected_user_id: uid, ...intent });
});
it.each([{ vault_scope: null }, { scopes: ["read", "write"] }, { expires_at: null }, { key_class: "service" }, { expires_at: "2030-01-31T12:03:04.123457Z" }])("rejects silent widening or contradictory receipt %j without retry", async patch => {
  const fetch = vi.fn().mockResolvedValue(json({ ...receipt, ...patch })); vi.stubGlobal("fetch", fetch);
  await expect(issuePat(authSessionSnapshot(), caps, intent)).rejects.toMatchObject({ tokenId: tid });
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("does not offer revocation for an unverified account identity", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ ...receipt, user_id: tid })));
  await expect(issuePat(authSessionSnapshot(), caps, intent)).rejects.toMatchObject({ tokenId: null });
});
it("accepts custom expiration at the same UTC instant and keeps microseconds", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(receipt)));
  expect((await issuePat(authSessionSnapshot(), caps, { ...intent, expires_days: undefined, expires_at: "2030-01-31T07:03:04.123456-05:00" })).verified).toBe(true);
});
it("preserves stable validation fields from the common error envelope", async () => {
  const detail = { code: "token_issuance_validation", message: "Invalid request", details: { fields: [{ field: "vault_scope.prefixes.0", code: "invalid_prefix", message: "Invalid prefix" }] } };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(detail, 422)));
  await expect(issuePat(authSessionSnapshot(), caps, intent)).rejects.toMatchObject({ status: 422, detail });
});
it("pins SSO CSRF and rejects late results after same-account cookie replacement", async () => {
  configureAuthTransport("sso"); document.cookie = "akb_dev_sso_csrf=first-session; path=/";
  const snapshot = authSessionSnapshot();
  let resolve!: (response: Response) => void;
  const fetch = vi.fn().mockReturnValue(new Promise<Response>(r => { resolve = r; })); vi.stubGlobal("fetch", fetch);
  const promise = issuePat(snapshot, caps, intent);
  document.cookie = "akb_dev_sso_csrf=second-session; path=/";
  resolve(json(receipt));
  await expect(promise).rejects.toMatchObject({ status: 409 });
  expect(new Headers(fetch.mock.calls[0][1].headers).get("X-AKB-CSRF")).toBe("first-session");
});
it("treats malformed success as unconfirmed and never exposes its secret", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ token: "akb_fixture_secret" })));
  const error = await issuePat(authSessionSnapshot(), caps, intent).catch(error => error);
  expect(error).toBeInstanceOf(UnverifiedPatReceipt);
  expect(JSON.stringify(error)).not.toContain("akb_fixture_secret");
});


it("bounds an unanswered mint request without automatically creating another token", async () => {
  vi.useFakeTimers();
  try {
    const fetch = vi.fn((_url, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    }));
    vi.stubGlobal("fetch", fetch);
    const rejected = expect(issuePat(authSessionSnapshot(), caps, intent)).rejects.toThrow("Aborted");
    await vi.advanceTimersByTimeAsync(30_000);
    await rejected;
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  } finally { vi.useRealTimers(); }
});
