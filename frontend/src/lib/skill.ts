// Frontend twin of backend app/services/skill_policy.py — keep in sync.
// The backend enforces; these exist so forms reject early with a friendly
// message instead of a server round-trip, and so tree/routing special-casing
// keys off one predicate instead of scattered literals.
export const RESERVED_COLLECTION = "overview";
export const VAULT_SKILL_PATH = "overview/vault-skill.md";

export function isReservedCollection(path: string): boolean {
  return (
    path === RESERVED_COLLECTION || path.startsWith(RESERVED_COLLECTION + "/")
  );
}
