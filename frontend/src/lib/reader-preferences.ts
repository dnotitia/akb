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
