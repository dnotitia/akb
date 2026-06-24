/**
 * Whether two git commit refs point at the SAME commit, tolerating
 * abbreviated hashes.
 *
 * AKB surfaces commit refs at different widths: the vault commit log links
 * 12-char short hashes, while a document's `current_commit` is the full
 * 40-char SHA. A git short hash is always a prefix of its full SHA, so exact
 * string equality would wrongly compare the newest commit (short) against HEAD
 * (full) and flag a live document as "historical". Prefix matching fixes that.
 *
 * Returns false when either ref is missing (e.g. HEAD still loading) so callers
 * can stay conservative (treat as historical / read-only until HEAD is known).
 */
export function sameCommitRef(a?: string | null, b?: string | null): boolean {
  if (!a || !b) return false;
  return a.startsWith(b) || b.startsWith(a);
}
