import { ApiError, authenticatedFetch, isCurrentAuthSession, type AuthSessionSnapshot } from "./api";

export interface AccountLifecycle {
  schema_version: 1;
  user_id: string;
  username: string;
  revoke_sessions: { supported: boolean; scope: string; includes_current: boolean; affects_pats: boolean; reason: string | null };
  deletion: {
    supported: boolean; allowed: boolean; reason: string | null; confirmation: string;
    blockers: { code: string; count?: number }[];
    effects: { active_pats_revoked?: number; shared_publications_preserved?: number };
  };
}
export interface DeletionBlockers {
  user_id: string;
  owned_vaults: { id: string; name: string }[];
  total: number;
  next_cursor: string | null;
}
const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const count = (value: unknown) => value === undefined || (typeof value === "number" && Number.isSafeInteger(value) && value >= 0);

async function request(path: string, snapshot: AuthSessionSnapshot, body?: unknown): Promise<unknown> {
  if (!isCurrentAuthSession(snapshot)) throw new ApiError("Your session changed. Review this action again.", 409, { code: "account_identity_changed" });
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 30_000);
  try {
    const response = await authenticatedFetch(`/api/v1/my/account/${path}`, {
      signal: controller.signal,
      method: body === undefined ? "GET" : "POST", cache: "no-store",
      headers: { "Content-Type": "application/json", ...(snapshot.token ? { Authorization: `Bearer ${snapshot.token}` } : {}), ...(body !== undefined && snapshot.mode === "sso" && snapshot.csrfToken ? { "X-AKB-CSRF": snapshot.csrfToken } : {}) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }, { unauthorized: "preserve-session" });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = object(payload) ? payload.detail : null;
      throw new ApiError(object(detail) && typeof detail.message === "string" ? detail.message : typeof detail === "string" ? detail : `Request failed (${response.status})`, response.status, detail);
    }
    return payload;
  } finally { window.clearTimeout(timeout); }
}

export async function getAccountLifecycle(snapshot: AuthSessionSnapshot): Promise<AccountLifecycle> {
  const p = await request("lifecycle", snapshot);
  if (!object(p) || p.schema_version !== 1 || typeof p.user_id !== "string" || !p.user_id || typeof p.username !== "string" || !p.username ||
      !object(p.revoke_sessions) || typeof p.revoke_sessions.supported !== "boolean" || typeof p.revoke_sessions.scope !== "string" ||
      typeof p.revoke_sessions.includes_current !== "boolean" || typeof p.revoke_sessions.affects_pats !== "boolean" ||
      (p.revoke_sessions.reason !== null && typeof p.revoke_sessions.reason !== "string") ||
      !object(p.deletion) || (p.deletion.reason !== null && typeof p.deletion.reason !== "string") || typeof p.deletion.supported !== "boolean" || typeof p.deletion.allowed !== "boolean" ||
      typeof p.deletion.confirmation !== "string" || !Array.isArray(p.deletion.blockers) ||
      !p.deletion.blockers.every(b => object(b) && typeof b.code === "string" && count(b.count)) || !object(p.deletion.effects) ||
      !count(p.deletion.effects.active_pats_revoked) || !count(p.deletion.effects.shared_publications_preserved)) {
    throw new Error("This server's account safety contract is unavailable.");
  }
  return p as unknown as AccountLifecycle;
}
export async function getDeletionBlockers(snapshot: AuthSessionSnapshot, cursor?: string): Promise<DeletionBlockers> {
  const p = await request(`deletion-blockers?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, snapshot);
  if (!object(p) || typeof p.user_id !== "string" || !Array.isArray(p.owned_vaults) || !p.owned_vaults.every(v => object(v) && typeof v.id === "string" && typeof v.name === "string") ||
      typeof p.total !== "number" || !count(p.total) || (p.next_cursor !== null && typeof p.next_cursor !== "string")) throw new Error("Could not verify owned vaults.");
  return p as unknown as DeletionBlockers;
}
export async function revokeAccountSessions(snapshot: AuthSessionSnapshot, userId: string): Promise<void> {
  const p = await request("session-revocations", snapshot, { expected_user_id: userId });
  if (!object(p) || p.user_id !== userId || typeof p.revoked_before !== "string" || !Number.isFinite(Date.parse(p.revoked_before))) throw new Error("The server response could not be verified. The result is unknown.");
}
export async function deleteAccount(snapshot: AuthSessionSnapshot, userId: string, username: string, password: string): Promise<void> {
  const p = await request("deletion", snapshot, { expected_user_id: userId, confirm_username: username, current_password: password });
  if (!object(p) || p.deleted !== true || p.user_id !== userId) throw new Error("The server response could not be verified. The result is unknown.");
}
