import { useEffect, useRef } from "react";
import {
  Copy,
  ExternalLink,
  EyeOff,
  FileText,
  Network,
  Pin,
  PinOff,
  Target,
} from "lucide-react";
import type { GraphNode } from "./graph-types";

export interface GraphMenuState {
  node: GraphNode;
  /** Viewport coordinates of the right-click. */
  x: number;
  y: number;
}

interface Props {
  state: GraphMenuState;
  pinned: boolean;
  onClose: () => void;
  onOpen: (newTab: boolean) => void;
  onExpand: () => void;
  onTogglePin: () => void;
  onHide: () => void;
  onFocus: () => void;
  onCopyUri: () => void;
}

const MENU_W = 208;
const MENU_H = 300;

/**
 * Floating right-click menu for a graph node — the actions the old canvas left
 * as a no-op TODO. Closes on Esc / outside click; each action runs then closes.
 * role=menu / menuitem with first-item autofocus for keyboard use.
 */
export function GraphContextMenu({
  state,
  pinned,
  onClose,
  onOpen,
  onExpand,
  onTogglePin,
  onHide,
  onFocus,
  onCopyUri,
}: Props) {
  const firstRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    firstRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Clamp into the viewport so an edge right-click doesn't push the menu
  // off-screen.
  const left = Math.min(state.x, window.innerWidth - MENU_W - 8);
  const top = Math.min(state.y, window.innerHeight - MENU_H - 8);

  const run = (fn: () => void) => () => {
    fn();
    onClose();
  };

  return (
    <>
      {/* Backdrop: swallow the next click to dismiss. */}
      <div className="fixed inset-0 z-[var(--z-popover)]" onClick={onClose} aria-hidden />
      <div
        role="menu"
        aria-label={`Actions for ${state.node.name}`}
        style={{ left, top }}
        className="fixed z-[var(--z-popover)] w-52 rounded-[var(--radius-md)] border border-border bg-surface shadow-md py-1 text-sm"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-3 py-1.5 text-[11px] text-foreground-muted truncate border-b border-border mb-1">
          {state.node.name}
        </div>
        <MenuItem ref={firstRef} icon={FileText} label="Open" onClick={run(() => onOpen(false))} />
        <MenuItem icon={ExternalLink} label="Open in new tab" onClick={run(() => onOpen(true))} />
        <MenuItem icon={Network} label="Expand neighbors" onClick={run(onExpand)} />
        <MenuItem icon={Target} label="Focus here" onClick={run(onFocus)} />
        <div className="my-1 border-t border-border" />
        <MenuItem
          icon={pinned ? PinOff : Pin}
          label={pinned ? "Unpin" : "Pin"}
          onClick={run(onTogglePin)}
        />
        <MenuItem icon={EyeOff} label="Hide" onClick={run(onHide)} />
        <MenuItem icon={Copy} label="Copy URI" onClick={run(onCopyUri)} />
      </div>
    </>
  );
}

const MenuItem = ({
  ref,
  icon: Icon,
  label,
  onClick,
}: {
  ref?: React.Ref<HTMLButtonElement>;
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  label: string;
  onClick: () => void;
}) => (
  <button
    ref={ref}
    type="button"
    role="menuitem"
    onClick={onClick}
    className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-foreground hover:bg-surface-hover focus:bg-surface-hover focus:outline-none cursor-pointer transition-colors"
  >
    <Icon className="h-3.5 w-3.5 text-foreground-muted" aria-hidden />
    {label}
  </button>
);
