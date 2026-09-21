import { ApiError, authenticatedFetch, isCurrentAuthSession, type AuthSessionSnapshot } from "./api";

export type VaultWriteScope = { prefixes: string[]; extra_vaults: string[] };
export interface PatMetadata {
  token_id: string; name: string; prefix: string; created_at?: string; last_used_at?: string | null;
  key_class?: string; scopes?: string[]; vault_scope?: VaultWriteScope | null; expires_at?: string | null;
}
export interface PatCapabilities {
  contract_version: 1; user_id: string; name_max_length: number;
  permission_presets: string[][]; expiration_modes: string[];
  vault_scope_semantics: "write_restriction_sql_read_write";
}
export interface PatIntent {
  name: string; scopes: string[]; vault_scope: VaultWriteScope | null;
  expires_days?: number; expires_at?: string;
}
export interface PatReceipt extends PatMetadata {
  token: string; user_id?: string; contract_version?: 1; issued_at?: string;
  verified: boolean;
}
export class UnverifiedPatReceipt extends Error {
  constructor(public tokenId: string | null) { super("Creation result needs checking. Review your tokens before trying again; a token may have been created."); }
}
const record = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(x => typeof x === "string");
const instant = (v: unknown): v is string => typeof v === "string" && /(?:Z|[+-]\d{2}:\d{2})$/i.test(v) && Number.isFinite(Date.parse(v));
const uuid = (v: unknown): v is string => typeof v === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v);
function preciseInstant(value: string): bigint {
  const fraction = value.match(/\.(\d+)(?=Z|[+-]\d{2}:\d{2}$)/i)?.[1] ?? "";
  const seconds = value.replace(/\.\d+(?=Z|[+-]\d{2}:\d{2}$)/i, "");
  return BigInt(Date.parse(seconds)) * 1_000_000n + BigInt(fraction.padEnd(9, "0").slice(0, 9));
}
export function canonicalScope(v: unknown): VaultWriteScope | null | undefined {
  if (v === null) return null;
  if (!record(v) || Object.keys(v).sort().join(",") !== "extra_vaults,prefixes" || !strings(v.prefixes) || !strings(v.extra_vaults) || ![...v.prefixes, ...v.extra_vaults].length ||
      ![...v.prefixes, ...v.extra_vaults].every(x => /^[a-z0-9][a-z0-9-]*$/.test(x))) return undefined;
  return { prefixes: [...new Set(v.prefixes)].sort(), extra_vaults: [...new Set(v.extra_vaults)].sort() };
}
export function sameScope(a: unknown, b: unknown): boolean {
  const left = canonicalScope(a), right = canonicalScope(b);
  return left !== undefined && right !== undefined && JSON.stringify(left) === JSON.stringify(right);
}
function current(snapshot: AuthSessionSnapshot) {
  if (!isCurrentAuthSession(snapshot)) throw new ApiError("Your sign-in session changed. Review token creation again.", 409, { code: "token_issuer_identity_changed" });
}
async function request(path: string, snapshot: AuthSessionSnapshot, body?: unknown, method?: string): Promise<unknown> {
  current(snapshot);
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 30_000);
  try {
    const response = await authenticatedFetch(`/api/v1${path}`, {
      method: method ?? (body === undefined ? "GET" : "POST"), cache: "no-store", signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(snapshot.token ? { Authorization: `Bearer ${snapshot.token}` } : {}),
        ...(snapshot.mode === "sso" && snapshot.csrfToken ? { "X-AKB-CSRF": snapshot.csrfToken } : {}) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }, { unauthorized: "preserve-session" });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = record(payload) && record(payload.detail) ? payload.detail : payload;
      const message = record(detail) ? [detail.message, detail.error, detail.detail].find(value => typeof value === "string") : detail;
      throw new ApiError(typeof message === "string" ? message : `Request failed (${response.status})`, response.status, detail);
    }
    current(snapshot);
    return payload;
  } finally { window.clearTimeout(timeout); }
}
export async function getPatCapabilities(snapshot: AuthSessionSnapshot): Promise<PatCapabilities | "legacy"> {
  let p: unknown;
  try { p = await request("/auth/tokens/capabilities", snapshot); }
  catch (e) { if (e instanceof ApiError && [404, 405, 501].includes(e.status)) return "legacy"; throw e; }
  if (!record(p) || p.contract_version !== 1 || !uuid(p.user_id) || !Number.isInteger(p.name_max_length) || (p.name_max_length as number) < 1 ||
      JSON.stringify(p.permission_presets) !== JSON.stringify([["read"], ["read", "write"]]) ||
      !strings(p.expiration_modes) || !["none", "days", "absolute"].every(x => (p.expiration_modes as string[]).includes(x)) ||
      p.vault_scope_semantics !== "write_restriction_sql_read_write") throw new Error("Token issuance capabilities could not be verified. Retry after checking the server.");
  return p as unknown as PatCapabilities;
}
export async function issuePat(snapshot: AuthSessionSnapshot, capabilities: PatCapabilities, intent: PatIntent): Promise<PatReceipt> {
  const p = await request("/auth/tokens/issuance", snapshot, { contract_version: 1, expected_user_id: capabilities.user_id, ...intent });
  const positiveId = record(p) && p.contract_version === 1 && p.user_id === capabilities.user_id && uuid(p.token_id) ? p.token_id : null;
  const expiresMatches = record(p) && (intent.expires_days !== undefined
    ? instant(p.expires_at) && instant(p.issued_at) && preciseInstant(p.expires_at) - preciseInstant(p.issued_at) === BigInt(intent.expires_days) * 86_400_000_000_000n
    : intent.expires_at !== undefined ? instant(p.expires_at) && preciseInstant(p.expires_at) === preciseInstant(intent.expires_at) : p.expires_at === null);
  if (!record(p) || !positiveId || p.key_class !== "pat" || p.name !== intent.name.normalize("NFC") ||
      typeof p.token !== "string" || !/^akb_[A-Za-z0-9_-]+$/.test(p.token) || typeof p.prefix !== "string" || !p.prefix || !p.token.startsWith(p.prefix) ||
      !instant(p.issued_at) || !strings(p.scopes) || JSON.stringify([...p.scopes].sort()) !== JSON.stringify([...intent.scopes].sort()) ||
      !sameScope(p.vault_scope, intent.vault_scope) || !expiresMatches) throw new UnverifiedPatReceipt(positiveId);
  return { ...p, verified: true } as unknown as PatReceipt;
}
export async function issueLegacyPat(snapshot: AuthSessionSnapshot, name: string): Promise<PatReceipt> {
  const p = await request("/auth/tokens", snapshot, { name });
  if (!record(p) || !uuid(p.token_id) || typeof p.token !== "string" || !/^akb_[A-Za-z0-9_-]+$/.test(p.token) || typeof p.prefix !== "string") throw new UnverifiedPatReceipt(null);
  return { ...p, name, verified: false } as unknown as PatReceipt;
}
export async function listIssuanceVaults(snapshot: AuthSessionSnapshot): Promise<string[]> {
  const p = await request("/vaults", snapshot);
  if (!record(p) || !Array.isArray(p.vaults) || !p.vaults.every(v => record(v) && typeof v.name === "string")) throw new Error("Could not load accessible Vaults.");
  return p.vaults.map(v => (v as { name: string }).name);
}
export async function revokeIssuedPat(snapshot: AuthSessionSnapshot, tokenId: string): Promise<void> {
  await request(`/auth/tokens/${encodeURIComponent(tokenId)}`, snapshot, undefined, "DELETE");
}
