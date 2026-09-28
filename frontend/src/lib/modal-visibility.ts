import { useSyncExternalStore } from "react";

// Shared by the Dialog primitive and nonmodal floating chrome. Tokens make
// nested dialogs and StrictMode cleanup independent; no DOM inspection needed.
const openModals = new Set<symbol>();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach(listener => listener());

export function registerModal() {
  const token = Symbol("modal");
  openModals.add(token);
  notify();
  return () => {
    if (openModals.delete(token)) notify();
  };
}

export const isModalOpen = () => openModals.size > 0;
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};

export function useModalOpen() {
  return useSyncExternalStore(subscribe, isModalOpen, () => false);
}

// Supporting inspectors need nested open/close transitions even when their
// parent reader is already a modal (the boolean snapshot stays true there).
const modalCount = () => openModals.size;
export function useModalCount() {
  return useSyncExternalStore(subscribe, modalCount, () => 0);
}
