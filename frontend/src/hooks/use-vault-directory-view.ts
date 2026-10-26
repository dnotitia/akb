import { useState } from "react";

type VaultDirectoryView = "grid" | "list";
const KEY = "akb.vaultDirectoryView.v1";

/** Appearance only; a browser preference shared by the directory and its loader. */
export function useVaultDirectoryView() {
  const [view, setView] = useState<VaultDirectoryView>(() => {
    try { return localStorage.getItem(KEY) === "list" ? "list" : "grid"; }
    catch { return "grid"; }
  });
  function changeView(next: VaultDirectoryView) {
    setView(next);
    try { localStorage.setItem(KEY, next); }
    catch { /* Keep the selected view usable when browser storage is unavailable. */ }
  }
  return { view, changeView };
}
