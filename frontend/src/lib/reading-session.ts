import { docUri, parseUri } from "@/lib/uri";
import { readingDocumentHref, readingDocumentKey, type ReadingDocument } from "@/lib/reading-workspace";

export const READING_SESSION_PREFIX = "akb:reading-session:v1:";
export const READING_TAB_LIMIT = 30;
export const READING_CLOSED_LIMIT = 20;
const MAX_BYTES = 128 * 1024;
type StoredDocument = { uri: string; version?: string; raw?: boolean; top: number; rawTop: number; sideTop: number };
export interface ReadingSession {
  tabs: ReadingDocument[];
  closed: ReadingDocument[];
  active: string | null;
  companion: ReadingDocument | null;
  positions: Map<string, number>;
  /** One in-memory close operation. Never serialize titles or editor state. */
  undo?: ClosedReadingTabs | null;
}
export interface ClosedReadingTabs {
  id: string;
  proof: string;
  tabs: ReadingDocument[];
  keys: string[];
  active: string | null;
  companion: ReadingDocument | null;
  positions: Map<string, number>;
}
export const emptyReadingSession = (): ReadingSession => ({ tabs: [], closed: [], active: null, companion: null, positions: new Map() });
function safeTop(value: unknown): number { return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(10_000_000, value)) : 0; }
function decodeDocument(value: unknown, positions: Map<string, number>): ReadingDocument | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Partial<StoredDocument>;
  if (typeof row.uri !== "string" || row.uri.length > 4096 || [...row.uri].some(character => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127)) return null;
  const parsed = parseUri(row.uri);
  if (!parsed || parsed.kind !== "doc" || !parsed.vault || !parsed.id || parsed.id.startsWith("/") || parsed.id.split("/").some(part => !part || part === "." || part === "..")) return null;
  if (row.version !== undefined && (typeof row.version !== "string" || !/^[a-f0-9]{7,64}$/.test(row.version))) return null;
  const params = new URLSearchParams();
  if (row.version) params.set("commit", row.version);
  if (row.raw === true) params.set("view", "raw");
  const doc: ReadingDocument = { vault: parsed.vault, path: parsed.id, title: "", version: row.version,
    href: readingDocumentHref(parsed.vault, parsed.id, params.size ? `?${params}` : "") };
  const key = readingDocumentKey(doc);
  positions.set(key + ":rendered", safeTop(row.top));
  positions.set(key + ":raw", safeTop(row.rawTop));
  positions.set(key + ":side", safeTop(row.sideTop));
  return doc;
}
export function readReadingSession(owner: string | null): ReadingSession {
  const empty = emptyReadingSession();
  if (!owner) return empty;
  try {
    const text = localStorage.getItem(READING_SESSION_PREFIX + encodeURIComponent(owner));
    if (!text || text.length > MAX_BYTES) return empty;
    const data = JSON.parse(text);
    if (data?.schema !== 1 || !Array.isArray(data.tabs) || !Array.isArray(data.closed)) return empty;
    const seen = new Set<string>();
    const decode = (values: unknown[], limit: number) => values.slice(0, limit).flatMap(value => {
      const doc = decodeDocument(value, empty.positions);
      if (!doc || seen.has(readingDocumentKey(doc))) return [];
      seen.add(readingDocumentKey(doc)); return [doc];
    });
    empty.tabs = decode(data.tabs, READING_TAB_LIMIT);
    empty.closed = decode(data.closed, READING_CLOSED_LIMIT);
    const active = decodeDocument(data.active, empty.positions);
    empty.active = active && empty.tabs.some(doc => readingDocumentKey(doc) === readingDocumentKey(active)) ? readingDocumentKey(active) : null;
    empty.companion = decodeDocument(data.companion, empty.positions);
    return empty;
  } catch { return empty; }
}
export function writeReadingSession(owner: string, session: ReadingSession): void {
  const encode = (doc: ReadingDocument): StoredDocument => ({ uri: docUri(doc.vault, doc.path), version: doc.version,
    raw: new URL(doc.href, window.location.origin).searchParams.get("view") === "raw",
    top: safeTop(session.positions.get(readingDocumentKey(doc) + ":rendered")),
    rawTop: safeTop(session.positions.get(readingDocumentKey(doc) + ":raw")),
    sideTop: safeTop(session.positions.get(readingDocumentKey(doc) + ":side")) });
  const active = session.tabs.find(doc => readingDocumentKey(doc) === session.active);
  try {
    const text = JSON.stringify({ schema: 1, tabs: session.tabs.slice(-READING_TAB_LIMIT).map(encode), closed: session.closed.slice(0, READING_CLOSED_LIMIT).map(encode), active: active ? encode(active) : null, companion: session.companion ? encode(session.companion) : null });
    if (text.length <= MAX_BYTES) localStorage.setItem(READING_SESSION_PREFIX + encodeURIComponent(owner), text);
  } catch { /* A denied/full storage area never interrupts reading or navigation. */ }
}
export function clearReadingSessions(): void {
  window.dispatchEvent(new Event("akb:clear-reading-session"));
  try { for (const key of Object.keys(localStorage)) if (key.startsWith(READING_SESSION_PREFIX)) localStorage.removeItem(key); } catch { /* Storage is optional. */ }
}
