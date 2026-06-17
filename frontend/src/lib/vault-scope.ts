import { canEdit } from "@/lib/roles";

// The vault-rail role filter, lifted out of the component so the predicate +
// persistence are unit-testable and the union/labels can't drift.

export const SCOPES = [
  { key: "all", label: "All" },
  { key: "owned", label: "Owned" },
  { key: "editable", label: "Can edit" },
] as const;

export type RoleScope = (typeof SCOPES)[number]["key"];

const SCOPE_KEY = "akb-vault-role-scope";

/** `editable` = write authority (owner/admin/writer); `owned` = owner only. */
export function inScope(role: string | undefined, scope: RoleScope): boolean {
  if (scope === "all") return true;
  if (scope === "owned") return role === "owner";
  return canEdit(role);
}

export function readScope(): RoleScope {
  try {
    const v = localStorage.getItem(SCOPE_KEY);
    return v === "owned" || v === "editable" ? v : "all";
  } catch {
    return "all";
  }
}

export function writeScope(s: RoleScope): void {
  try {
    localStorage.setItem(SCOPE_KEY, s);
  } catch {
    // storage disabled — keep the in-memory scope, never throw.
  }
}
