import { useSyncExternalStore } from "react";

export interface ReaderPreferences { fontSize: number; width: "compact" | "comfortable" | "wide"; tableFit: boolean; foldCode: boolean }
export const DEFAULT_READER_PREFERENCES: ReaderPreferences = { fontSize: 16, width: "comfortable", tableFit: false, foldCode: false };
export const READER_PREFERENCES_KEY = "akb:reader-preferences:v1";
export function readReaderPreferences(): ReaderPreferences {
  try {
    const value = JSON.parse(localStorage.getItem(READER_PREFERENCES_KEY) || "null");
    return { fontSize: [14, 15, 16, 18, 20, 22].includes(value?.fontSize) ? value.fontSize : 16,
      width: ["compact", "comfortable", "wide"].includes(value?.width) ? value.width : "comfortable",
      tableFit: value?.tableFit === true, foldCode: value?.foldCode === true };
  } catch { return DEFAULT_READER_PREFERENCES; }
}


let snapshot: ReaderPreferences | undefined;
let stored: string | null | undefined;
const preferenceEvent = "akb:reader-preferences-change";
function getSnapshot(): ReaderPreferences {
  try {
    const current = localStorage.getItem(READER_PREFERENCES_KEY);
    if (!snapshot || current !== stored) { stored = current; snapshot = readReaderPreferences(); }
  } catch { snapshot ??= DEFAULT_READER_PREFERENCES; }
  return snapshot!;
}
function subscribe(notify: () => void) {
  const storage = (event: StorageEvent) => { if (event.key === READER_PREFERENCES_KEY || event.key === null) { stored = undefined; notify(); } };
  window.addEventListener(preferenceEvent, notify); window.addEventListener("storage", storage);
  return () => { window.removeEventListener(preferenceEvent, notify); window.removeEventListener("storage", storage); };
}
function setPreferences(value: ReaderPreferences) {
  snapshot = value;
  try { localStorage.setItem(READER_PREFERENCES_KEY, JSON.stringify(value)); stored = localStorage.getItem(READER_PREFERENCES_KEY); } catch { /* Use the in-memory preference when storage is denied. */ }
  window.dispatchEvent(new Event(preferenceEvent));
}
export function useReaderPreferences() {
  return { preferences: useSyncExternalStore(subscribe, getSnapshot, () => DEFAULT_READER_PREFERENCES), setPreferences };
}
