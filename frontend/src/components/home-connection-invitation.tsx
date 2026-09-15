import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { ArrowRight, ArrowUpRight, PlugZap, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { QuickstartDialog } from "@/components/quickstart-dialog";
import { useModalOpen } from "@/lib/modal-visibility";

const SEEN_KEY = "akb.homeConnectionInvitationSeen";
const LEGACY_KEY = "akb.homeConnectionGuideDismissed";
const seenInSession = new Set<string>();
const DESKTOP_QUERY = "(min-width: 1024px) and (min-height: 600px)";

function wasSeen(userId: string | null) {
  if (!userId) return true;
  if (seenInSession.has(userId)) return true;
  try {
    return localStorage.getItem(`${SEEN_KEY}:${userId}`) === "1"
      || localStorage.getItem(`${LEGACY_KEY}:${userId}`) === "1";
  } catch { return false; }
}

function remember(userId: string | null) {
  if (!userId) return;
  seenInSession.add(userId);
  try { localStorage.setItem(`${SEEN_KEY}:${userId}`, "1"); } catch { /* Browser storage is optional. */ }
}

interface HomeConnectionInvitationProps {
  userId: string | null;
  /** Verified PAT-empty + available setup + at least one accessible Vault. */
  eligible: boolean;
  oauthEnabled: boolean;
  onTokenCreated?: () => void;
}

/** Home reserves 80px + safe-area bottom clearance for the compact launcher. */
export function HomeConnectionInvitation(props: HomeConnectionInvitationProps) {
  // Identity changes also dispose any open setup and its in-memory secret.
  return <ConnectionInvitation key={props.userId ?? "anonymous"} {...props} />;
}

function ConnectionInvitation({ userId, eligible, oauthEnabled, onTokenCreated }: HomeConnectionInvitationProps) {
  const [canIntroduce] = useState(() => !wasSeen(userId));
  const [minimized, setMinimized] = useState(false);
  const [quickstartOpen, setQuickstartOpen] = useState(false);
  const [overlapsFocus, setOverlapsFocus] = useState(false);
  const [viewportConstrained, setViewportConstrained] = useState(false);
  const modalOpen = useModalOpen();
  const regionRef = useRef<HTMLElement>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const media = useMemo(() => window.matchMedia(DESKTOP_QUERY), []);
  const mediaSubscribe = useMemo(() => (listener: () => void) => {
    media.addEventListener("change", listener);
    return () => media.removeEventListener("change", listener);
  }, [media]);
  const desktop = useSyncExternalStore(mediaSubscribe, () => media.matches, () => false);
  const expanded = canIntroduce && eligible && !minimized && desktop;
  const suspended = modalOpen || overlapsFocus || viewportConstrained;

  useEffect(() => {
    if (expanded && !suspended) remember(userId);
  }, [expanded, suspended, userId]);

  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.newValue === "1" && (event.key === `${SEEN_KEY}:${userId}` || event.key === `${LEGACY_KEY}:${userId}`)) {
        setMinimized(true);
      }
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, [userId]);

  useLayoutEffect(() => {
    let frame = 0;
    const check = () => {
      const region = regionRef.current;
      const active = document.activeElement;
      const viewport = window.visualViewport;
      setViewportConstrained(Boolean(viewport && (viewport.scale > 1 || viewport.height < window.innerHeight * 0.7)));
      // A route/skip-link focus target covers the whole page, not one obscured
      // control. Keep the launcher available; actual child controls still opt
      // into overlap protection below.
      if (!region || !(active instanceof HTMLElement) || active === document.body || active.matches("main, [role='main']") || region.contains(active) || modalOpen) {
        setOverlapsFocus(false);
        return;
      }
      const floating = region.getBoundingClientRect();
      const focused = active.getBoundingClientRect();
      const overlaps = focused.width > 0 && focused.height > 0 && floating.width > 0
        && focused.left < floating.right + 4 && focused.right > floating.left - 4
        && focused.top < floating.bottom + 4 && focused.bottom > floating.top - 4;
      if (overlaps && expanded) setMinimized(true);
      // Keep geometry measurable while hidden, so focus/scroll can restore it
      // without polling or shifting the focused control.
      setOverlapsFocus(overlaps);
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(check);
    };
    check();
    document.addEventListener("focusin", check);
    document.addEventListener("focusout", schedule);
    window.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    window.visualViewport?.addEventListener("resize", schedule);
    window.visualViewport?.addEventListener("scroll", schedule);
    const observer = new ResizeObserver(schedule);
    if (regionRef.current) observer.observe(regionRef.current);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("focusin", check);
      document.removeEventListener("focusout", schedule);
      window.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
      window.visualViewport?.removeEventListener("resize", schedule);
      window.visualViewport?.removeEventListener("scroll", schedule);
      observer.disconnect();
    };
  }, [expanded, modalOpen]);

  function openSetup() {
    remember(userId);
    setMinimized(true);
    setQuickstartOpen(true);
  }

  function minimize() {
    remember(userId);
    setMinimized(true);
    requestAnimationFrame(() => launcherRef.current?.focus({ preventScroll: true }));
  }

  return <>
    {/* Animated route ancestors can establish a fixed-position containing block.
        Keep the floating UI viewport-owned without changing page animations. */}
    {createPortal(<aside ref={regionRef} aria-label="Connect your AI tools" aria-hidden={suspended || undefined} inert={suspended}
      data-testid="home-connection-invitation" data-expanded={expanded}
      // Visibility is an accessibility boundary, not an animation. Even a 1ms
      // reduced-motion transition can swallow focus restoration on dialog close.
      className="fixed bottom-[calc(1rem+env(safe-area-inset-bottom))] right-[calc(1rem+env(safe-area-inset-right))] z-[var(--z-sticky)] max-w-[calc(100vw-2rem)] transition-none sm:bottom-[calc(1.5rem+env(safe-area-inset-bottom))] sm:right-[calc(1.5rem+env(safe-area-inset-right))]"
      style={{ visibility: suspended ? "hidden" : undefined }}>
      {expanded ? <div className="w-76 max-w-full rounded-[var(--radius-md)] border border-border-strong bg-surface p-4 text-foreground shadow-md">
        <div className="flex items-start gap-2">
          <PlugZap className="mt-1 h-4 w-4 shrink-0 text-link" aria-hidden />
          <h2 id={titleId} className="min-w-0 flex-1 pt-0.5 text-sm font-semibold leading-5">Use AKB in your AI tools</h2>
          <Button variant="ghost" size="icon" className="-mr-2 -mt-2 h-11 w-11 shrink-0" aria-label="Minimize connection guide" onClick={minimize}>
            <X className="h-4 w-4" aria-hidden />
          </Button>
        </div>
        <p className="mt-1 text-sm leading-5 text-foreground-muted">Search your vaults from your AI tool.</p>
        <Button className="mt-4 h-11 w-full text-sm" onClick={openSetup} aria-haspopup="dialog">Connect an agent <ArrowRight className="h-4 w-4" aria-hidden /></Button>
      </div> : <Button ref={launcherRef} variant="outline" className="h-11 border-border-strong text-sm text-link shadow-md" onClick={openSetup} aria-haspopup="dialog">
        <PlugZap className="h-4 w-4" aria-hidden /> Connect an agent <ArrowUpRight className="h-4 w-4" aria-hidden />
      </Button>}
    </aside>, document.body)}
    <QuickstartDialog open={quickstartOpen} onOpenChange={setQuickstartOpen} mcpOauthEnabled={oauthEnabled} returnFocusRef={launcherRef}
      onTokenCreated={() => { remember(userId); setMinimized(true); onTokenCreated?.(); }} />
  </>;
}
