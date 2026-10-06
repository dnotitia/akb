import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

const HeaderActionsContext = createContext<{
  target: HTMLDivElement | null;
  setTarget: (target: HTMLDivElement | null) => void;
} | null>(null);

/** Pages retain ownership of their actions; the shell supplies only placement. */
export function VaultHeaderActionsProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<HTMLDivElement | null>(null);
  const value = useMemo(() => ({ target, setTarget }), [target]);
  return <HeaderActionsContext.Provider value={value}>{children}</HeaderActionsContext.Provider>;
}

export function VaultHeaderActionsSlot() {
  const context = useContext(HeaderActionsContext);
  return context ? <div ref={context.setTarget} className="flex shrink-0 items-center border-l border-border pl-3 empty:hidden" /> : null;
}

export function VaultHeaderAction({ children }: { children: ReactNode }) {
  const context = useContext(HeaderActionsContext);
  if (context) return context.target ? createPortal(children, context.target) : null;
  // Standalone readers (for example embedded previews) need no shell contract.
  return <div className="flex shrink-0 justify-end border-b border-border px-3 py-1">{children}</div>;
}
