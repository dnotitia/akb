import { useLayoutEffect, useRef } from "react";

interface HistoryEntry {
  index: number;
  key: unknown;
}

interface ExitAttempt {
  destination: HistoryEntry;
  cancelled: boolean;
}

const guards = new Set<(event: PopStateEvent) => void>();
const corrections = new Set<(event: PopStateEvent) => void>();
let installations = 0;

function dispatchPop(event: PopStateEvent) {
  for (const listener of [...corrections, ...[...guards].reverse()]) {
    listener(event);
    if (event.cancelBubble) return;
  }
}

/** Install before BrowserRouter mounts; window-targeted POPs run in registration order. */
export function installDocumentHistoryGuard() {
  if (installations++ === 0) window.addEventListener("popstate", dispatchPop, true);
  let disposed = false;
  return () => {
    if (disposed) return;
    disposed = true;
    if (--installations === 0) window.removeEventListener("popstate", dispatchPop, true);
  };
}

function currentEntry(state: unknown = window.history.state): HistoryEntry | null {
  if (!state || typeof state !== "object" || !("idx" in state)) return null;
  if (typeof state.idx !== "number" || !Number.isInteger(state.idx)) return null;
  return { index: state.idx, key: "key" in state ? state.key : undefined };
}

function sameEntry(left: HistoryEntry | null, right: HistoryEntry | null) {
  return left?.index === right?.index && left?.key === right?.key;
}

// Native traversals cannot be cancelled. If the owner unmounts while its
// correction is queued, swallow only that exact return event, then detach.
function drainCorrection(expected: HistoryEntry) {
  const stop = () => {
    corrections.delete(drain);
    window.clearTimeout(timeout);
  };
  const drain = (event: PopStateEvent) => {
    stop();
    if (sameEntry(currentEntry(event.state), expected)) event.stopImmediatePropagation();
  };
  const timeout = window.setTimeout(stop, 1_000);
  corrections.add(drain);
}

/**
 * Guard BrowserRouter's indexed, same-document Back/Forward entries. A blocked
 * POP and its correction stay invisible to the router; confirmation replays
 * the first requested delta once. Cross-document exits still use beforeunload.
 */
export function useDocumentHistoryGuard(
  enabled: boolean,
  requestExit: (proceed: () => void) => void,
) {
  const latest = useRef({ enabled, requestExit });
  const sync = useRef<(() => void) | null>(null);

  useLayoutEffect(() => {
    latest.current = { enabled, requestExit };
    sync.current?.();
  });

  useLayoutEffect(() => {
    let anchor = currentEntry();
    let lastObserved = anchor;
    let phase: "idle" | "restoring" | "confirming" | "replaying" = "idle";
    let attempt: ExitAttempt | null = null;
    let correctionTimer: number | null = null;
    let correctionInFlight = false;
    let mounted = true;

    function clearCorrectionTimer() {
      if (correctionTimer !== null) window.clearTimeout(correctionTimer);
      correctionTimer = null;
    }

    function refresh() {
      if (!latest.current.enabled && attempt) {
        attempt.cancelled = true;
        if (phase === "confirming") {
          attempt = null;
          phase = "idle";
        }
      }
      // PUSH/REPLACE can keep the document mounted. Observe the committed
      // entry after its render, without intercepting or patching those calls.
      if (phase === "idle" || phase === "confirming") {
        const entry = currentEntry();
        if (!sameEntry(entry, anchor)) {
          anchor = entry;
          attempt = null;
          phase = "idle";
        }
      }
    }

    function scheduleCorrection() {
      clearCorrectionTimer();
      // Coalesce POPs already queued by repeated Back into one return trip.
      correctionTimer = window.setTimeout(() => {
        correctionTimer = null;
        const entry = currentEntry();
        if (!mounted || phase !== "restoring" || !anchor || !entry) return;
        const delta = anchor.index - entry.index;
        if (delta !== 0) {
          correctionInFlight = true;
          window.history.go(delta);
        }
      }, 0);
    }

    function finishRestoring() {
      clearCorrectionTimer();
      correctionInFlight = false;
      const pending = attempt;
      if (!pending || pending.cancelled || !latest.current.enabled || !anchor) {
        phase = "idle";
        attempt = null;
        return;
      }
      phase = "confirming";
      const origin = anchor;
      latest.current.requestExit(() => {
        if (!mounted || phase !== "confirming" || attempt !== pending) return;
        if (!sameEntry(currentEntry(), origin)) return;
        phase = "replaying";
        window.history.go(pending.destination.index - origin.index);
      });
    }

    function onPop(event: PopStateEvent) {
      const entry = currentEntry(event.state);
      lastObserved = entry;
      if (!entry || !anchor) {
        clearCorrectionTimer();
        anchor = entry;
        attempt = null;
        phase = "idle";
        return;
      }

      if (phase === "restoring") {
        event.stopImmediatePropagation();
        if (sameEntry(entry, anchor)) finishRestoring();
        else scheduleCorrection();
        return;
      }

      if (phase === "replaying" && sameEntry(entry, attempt?.destination ?? null)) {
        anchor = entry;
        attempt = null;
        phase = "idle";
        return;
      }

      if (!latest.current.enabled || sameEntry(entry, anchor)) {
        anchor = entry;
        attempt = null;
        phase = "idle";
        return;
      }

      event.stopImmediatePropagation();
      attempt = { destination: entry, cancelled: false };
      phase = "restoring";
      scheduleCorrection();
    }

    sync.current = refresh;
    guards.add(onPop);
    return () => {
      mounted = false;
      sync.current = null;
      clearCorrectionTimer();
      guards.delete(onPop);
      const entry = currentEntry();
      if (phase === "restoring" && anchor && entry && sameEntry(entry, lastObserved) && !sameEntry(entry, anchor)) {
        drainCorrection(anchor);
        if (!correctionInFlight) window.history.go(anchor.index - entry.index);
      }
      attempt = null;
    };
  }, []);
}
