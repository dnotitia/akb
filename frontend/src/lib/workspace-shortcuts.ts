import { useEffect, useState } from "react";

export interface WorkspaceShortcut {
  kind: "document" | "collection";
  vault: string;
  path: string;
  title: string;
  documentId?: string;
}
export const WORKSPACE_SHORTCUTS_EVENT = "akb:workspace-shortcuts";
const PREFIX = "akb.workspaceShortcuts.v1:";
const LIMIT = 30;
const key = (userId: string) => `${PREFIX}${userId}`;
function valid(value: unknown): value is WorkspaceShortcut {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (item.kind === "document" || item.kind === "collection") &&
    [item.vault, item.path, item.title].every(v => typeof v === "string" && v.trim().length > 0 && v.length <= 2048) &&
    (item.documentId === undefined || typeof item.documentId === "string");
}
export function workspaceShortcutKey(item: WorkspaceShortcut): string {
  return JSON.stringify([item.kind, item.vault, item.path]);
}
export function workspaceShortcutHref(item: WorkspaceShortcut): string {
  const base = `/vault/${encodeURIComponent(item.vault)}`;
  return item.kind === "document" ? `${base}/doc/${encodeURIComponent(item.path)}` : `${base}?collection=${encodeURIComponent(item.path)}`;
}
export function readWorkspaceShortcuts(userId: string): WorkspaceShortcut[] {
  if (!userId || typeof window === "undefined") return [];
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(key(userId)) || "[]");
    if (!Array.isArray(raw)) return [];
    const seen = new Set<string>();
    return raw.filter(valid).filter(item => {
      const id = workspaceShortcutKey(item);
      if (seen.has(id)) return false;
      seen.add(id); return true;
    }).slice(0, LIMIT).map(({ kind, vault, path, title, documentId }) => ({ kind, vault, path, title, ...(documentId ? { documentId } : {}) }));
  } catch { return []; }
}
function write(userId: string, items: WorkspaceShortcut[]): boolean {
  if (!userId || typeof window === "undefined") return false;
  try {
    localStorage.setItem(key(userId), JSON.stringify(items));
    window.dispatchEvent(new CustomEvent(WORKSPACE_SHORTCUTS_EVENT, { detail: userId }));
    return true;
  } catch { return false; }
}
export function removeWorkspaceShortcut(userId: string, item: WorkspaceShortcut): boolean {
  return write(userId, readWorkspaceShortcuts(userId).filter(row => workspaceShortcutKey(row) !== workspaceShortcutKey(item)));
}
export function toggleWorkspaceShortcut(userId: string, item: WorkspaceShortcut): boolean {
  if (!valid(item)) return false;
  const current = readWorkspaceShortcuts(userId);
  if (current.some(row => workspaceShortcutKey(row) === workspaceShortcutKey(item))) return removeWorkspaceShortcut(userId, item);
  if (current.length >= LIMIT) return false;
  const { kind, vault, path, title, documentId } = item;
  return write(userId, [...current, { kind, vault, path, title, ...(documentId ? { documentId } : {}) }]);
}
export function useWorkspaceShortcuts(userId: string | undefined): WorkspaceShortcut[] {
  const [, refresh] = useState(0);
  useEffect(() => {
    const onChange = () => refresh(value => value + 1);
    const onStorage = (event: StorageEvent) => { if (event.key === null || event.key === key(userId || "")) onChange(); };
    window.addEventListener(WORKSPACE_SHORTCUTS_EVENT, onChange);
    window.addEventListener("storage", onStorage);
    return () => { window.removeEventListener(WORKSPACE_SHORTCUTS_EVENT, onChange); window.removeEventListener("storage", onStorage); };
  }, [userId]);
  return readWorkspaceShortcuts(userId || "");
}
