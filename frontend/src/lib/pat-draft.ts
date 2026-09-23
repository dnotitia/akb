import { canonicalScope, type PatIntent, type PatMetadata, type VaultWriteScope } from "./api-pat-issuance";

export interface PatDraft {
  name: string; permissions: "read" | "read-write" | "unsupported"; originalScopes?: string[];
  expiration: string; customTime: string; originalExpiration?: { iso: string; displayValue: string }; restricted: boolean; vaults: string[]; prefixes: string[];
}
export type PatFieldErrors = Record<string, string>;
export const defaultPatDraft = (): PatDraft => ({ name: "", permissions: "read-write", expiration: "none", customTime: "", restricted: false, vaults: [], prefixes: [] });
export function localDateTime(iso: string): string {
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return "";
  const pad = (n: number, size = 2) => String(n).padStart(size, "0");
  return `${pad(d.getFullYear(), 4)}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}
export function parseLocalExpiration(value: string): string | null {
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d{1,3})?)?$/.test(value)) return null;
  const candidate = new Date(value);
  if (!Number.isFinite(candidate.getTime())) return null;
  const [date, time] = value.split("T");
  const padded = `${date}T${time.length === 5 ? `${time}:00.000` : time.includes(".") ? `${time.split(".")[0]}.${time.split(".")[1].padEnd(3, "0")}` : `${time}.000`}`;
  if (localDateTime(candidate.toISOString()) !== padded) return null;
  // Test offsets around the local date, including both sides of a DST change.
  // An ambiguous wall time has two matching instants and needs another choice.
  const offsets = new Set<number>();
  for (let hours = -48; hours <= 48; hours += 6) offsets.add(new Date(candidate.getTime() + hours * 3_600_000).getTimezoneOffset());
  const matches = [...offsets].filter(offset => localDateTime(new Date(candidate.getTime() + (offset - candidate.getTimezoneOffset()) * 60_000).toISOString()) === padded);
  return matches.length === 1 ? candidate.toISOString() : null;
}
export function customExpiration(draft: PatDraft): string | null {
  return draft.originalExpiration?.displayValue === draft.customTime ? draft.originalExpiration.iso : parseLocalExpiration(draft.customTime);
}
export function replacementDraft(token: PatMetadata): PatDraft | null {
  if (token.key_class !== "pat" || !Array.isArray(token.scopes) || token.scopes.length === 0 ||
      canonicalScope(token.vault_scope) === undefined || token.expires_at === undefined || (token.expires_at !== null && !Number.isFinite(Date.parse(token.expires_at)))) return null;
  const scope = canonicalScope(token.vault_scope)!;
  const scopes = [...token.scopes].sort().join(",");
  return { name: token.name, permissions: scopes === "read" ? "read" : scopes === "read,write" ? "read-write" : "unsupported", originalScopes: token.scopes,
    expiration: token.expires_at === null ? "none" : "custom", customTime: token.expires_at ? localDateTime(token.expires_at) : "",
    ...(token.expires_at ? { originalExpiration: { iso: token.expires_at, displayValue: localDateTime(token.expires_at) } } : {}),
    restricted: scope !== null, vaults: scope?.extra_vaults ?? [], prefixes: scope?.prefixes ?? [] };
}
export function validatePatDraft(draft: PatDraft, maxName = 255, now = Date.now()): { intent?: PatIntent; errors: PatFieldErrors } {
  const errors: PatFieldErrors = {};
  const name = draft.name.trim().normalize("NFC");
  if (!name || [...name].length > maxName) errors.name = `Enter a name with 1–${maxName} characters.`;
  if (draft.permissions === "unsupported") errors.scopes = `Existing permissions (${draft.originalScopes?.join(", ")}) are unsupported. Explicitly choose a permission preset to change them.`;
  let vault_scope: VaultWriteScope | null = null;
  if (draft.restricted) {
    const scope = { prefixes: [...new Set(draft.prefixes.map(v => v.trim()))].sort(), extra_vaults: [...new Set(draft.vaults.map(v => v.trim()))].sort() };
    if (!scope.prefixes.length && !scope.extra_vaults.length) errors.vault_scope = "Add at least one exact Vault name or name prefix.";
    if ([...scope.prefixes, ...scope.extra_vaults].some(v => !/^[a-z0-9][a-z0-9-]*$/.test(v))) errors.vault_scope = "Use lowercase letters, digits and hyphens, starting with a letter or digit. Wildcards and slashes are not allowed.";
    vault_scope = scope;
  }
  const intent: PatIntent = { name, scopes: draft.permissions === "read" ? ["read"] : ["read", "write"], vault_scope };
  if (draft.expiration === "custom") {
    const expires = customExpiration(draft);
    if (!expires) errors.expires_at = "Choose a valid, unambiguous local date and time. Times skipped or repeated by a clock change cannot be used.";
    else if (Date.parse(expires) <= now) errors.expires_at = "Expiration must be in the future. Choose a new date and time.";
    else intent.expires_at = expires;
  } else if (draft.expiration !== "none") {
    if (!/^[1-9]\d*$/.test(draft.expiration) || !Number.isSafeInteger(Number(draft.expiration))) errors.expires_at = "Choose a positive expiration in days.";
    else intent.expires_days = Number(draft.expiration);
  }
  return { intent: Object.keys(errors).length ? undefined : intent, errors };
}
export function scopeSummary(scope: VaultWriteScope | null | undefined): string {
  if (scope === undefined) return "Vault write restriction unknown";
  if (scope === null) return "No additional Vault write restriction";
  return `Writes: ${[...scope.extra_vaults, ...scope.prefixes.map(p => `prefix ${p}`)].join(" OR ") || "Select at least one Vault or prefix"}`;
}
export function draftSummary(draft: PatDraft): string {
  const expiration = draft.expiration === "none" ? "No expiration" : draft.expiration === "custom" ? `Expires ${customExpiration(draft) ?? "at a date and time you choose"}` : `Expires ${draft.expiration} × 24 hours after issuance`;
  return `${expiration} · ${draft.permissions === "read" ? "Read only" : draft.permissions === "read-write" ? "Read and write" : `Unsupported permissions: ${draft.originalScopes?.join(", ")}`} · ${scopeSummary(draft.restricted ? { prefixes: draft.prefixes, extra_vaults: draft.vaults } : null)}`;
}
