import { readingDocumentKey, type ReadingDocument } from "./reading-workspace";
import { READING_CLOSED_LIMIT, READING_TAB_LIMIT, type ReadingSession } from "./reading-session";

export type ReadingTabCloseScope = "all" | "others" | "left" | "right";

export function readingTabCloseKeys(tabs: ReadingDocument[], scope: ReadingTabCloseScope, target: string): string[] {
  const index = tabs.findIndex(doc => readingDocumentKey(doc) === target);
  if (scope !== "all" && index < 0) return [];
  return tabs.filter((_doc, position) => scope === "all" || (scope === "others" ? position !== index : scope === "left" ? position < index : position > index)).map(readingDocumentKey);
}

export function closeReadingTabs(session: ReadingSession, keys: string[], id: string, proof: string, all = false): ReadingSession {
  const selected = new Set(keys);
  const removed = session.tabs.filter(doc => selected.has(readingDocumentKey(doc)));
  const clearSide = all || !!session.companion && selected.has(readingDocumentKey(session.companion));
  if (!removed.length && !(clearSide && session.companion)) return session;
  const history = [...removed].sort((a, b) => Number(readingDocumentKey(b) === session.active) - Number(readingDocumentKey(a) === session.active));
  return { ...session,
    tabs: session.tabs.filter(doc => !selected.has(readingDocumentKey(doc))),
    active: session.active && selected.has(session.active) ? null : session.active,
    companion: clearSide ? null : session.companion,
    closed: [...history, ...session.closed.filter(doc => !selected.has(readingDocumentKey(doc)))].slice(0, READING_CLOSED_LIMIT),
    undo: { id, proof, tabs: session.tabs, keys: removed.map(readingDocumentKey), active: session.active,
      companion: clearSide ? session.companion : null, positions: new Map(session.positions) },
  };
}

/** Preserve newly opened tabs. A full workspace must make room before undo. */
export function restoreReadingTabs(session: ReadingSession): ReadingSession | null {
  const batch = session.undo;
  if (!batch) return session;
  const closed = new Set(batch.keys);
  const current = new Map(session.tabs.map(doc => [readingDocumentKey(doc), doc]));
  const restored = batch.tabs.filter(doc => closed.has(readingDocumentKey(doc)) && !current.has(readingDocumentKey(doc)));
  if (current.size + restored.length > READING_TAB_LIMIT) return null;
  const positions = new Map(session.positions);
  for (const doc of restored) {
    const key = readingDocumentKey(doc);
    current.set(key, doc);
    for (const mode of ["rendered", "raw", "side"]) {
      const position = batch.positions.get(`${key}:${mode}`);
      if (position !== undefined) positions.set(`${key}:${mode}`, position);
    }
  }
  const original = new Set(batch.tabs.map(readingDocumentKey));
  const tabs = [...batch.tabs.flatMap(doc => current.get(readingDocumentKey(doc)) ?? []), ...session.tabs.filter(doc => !original.has(readingDocumentKey(doc)))];
  return { ...session, tabs, positions, undo: null,
    closed: session.closed.filter(doc => !closed.has(readingDocumentKey(doc))),
    active: batch.active && current.has(batch.active) ? batch.active : session.active,
    companion: session.companion ?? batch.companion,
  };
}
