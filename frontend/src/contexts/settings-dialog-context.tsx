import { createContext, lazy, Suspense, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, matchPath, useLocation, useNavigate, type LinkProps } from "react-router-dom";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { InlineLoadingState } from "@/components/ui/loading-state";
import { ErrorBoundary } from "@/components/error-boundary";
import { Skeleton } from "@/components/ui/skeleton";

const SettingsDialog = lazy(() => import("@/pages/settings/settings-dialog"));
type SettingsControls = {
  openSettings: (section?: string, trigger?: HTMLElement | null) => void;
  closeSettings: () => void;
};
const SettingsContext = createContext<SettingsControls | null>(null);

// eslint-disable-next-line react-refresh/only-export-components
export function useSettingsDialog() { return useContext(SettingsContext); }

export function SettingsDialogProvider({ children, identity }: { children: ReactNode; identity: string }) {
  const location = useLocation();
  const navigate = useNavigate();
  const [request, setRequest] = useState<{ section: string; path: string; routeKey: string; identity: string } | null>(null);
  const currentRequest = useRef(request);
  currentRequest.current = request;
  const returnFocus = useRef<HTMLElement | null>(null);
  const path = location.pathname + location.search;
  const openSettings = useCallback((section = "profile", trigger?: HTMLElement | null) => {
    setRequest(current => {
      if (current?.identity === identity && current.routeKey === location.key) return current;
      returnFocus.current = trigger ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
      return { section, path, routeKey: location.key, identity };
    });
  }, [path, location.key, identity]);
  const closeSettings = useCallback(() => setRequest(null), []);
  const controls = useMemo(() => ({ openSettings, closeSettings }), [openSettings, closeSettings]);
  // Browser history and links inside settings cannot leave an orphan overlay.
  useEffect(() => { setRequest(current => current && (current.routeKey !== location.key || current.identity !== identity) ? null : current); }, [location.key, identity]);
  const dismiss = () => {
    closeSettings();
    if (matchPath("/settings", location.pathname)) navigate("/", { replace: true });
  };
  const restoreFocus = () => {
    const target = returnFocus.current;
    if (target?.isConnected && target !== document.body) target.focus({ preventScroll: true });
    else document.querySelector<HTMLElement>("#workspace-settings-trigger, #account-menu-trigger, #main")?.focus({ preventScroll: true });
  };
  return <SettingsContext.Provider value={controls}>
    {children}
    {request && request.identity === identity && <ErrorBoundary resetKeys={[request.path]}>
      <Suspense fallback={<Dialog open onOpenChange={open => { if (!open) dismiss(); }}><DialogContent className="settings-studio settings-studio-dialog" overlayProps={{ className: "settings-studio-overlay" }} onCloseAutoFocus={event => {
        event.preventDefault();
        // A lazy handoff is not a dismissal: the real dialog owns focus now.
        if (currentRequest.current === null) restoreFocus();
      }}><header className="settings-studio-header"><div className="settings-studio-brand"><DialogTitle>Settings</DialogTitle></div></header><DialogDescription className="sr-only">Loading your account settings.</DialogDescription><div className="settings-studio-body"><div className="settings-studio-sidebar"><div className="hidden space-y-3 p-5 md:block">{[0, 1, 2, 3].map(row => <Skeleton key={row} className="h-9 w-full" />)}</div></div><div className="settings-studio-content"><InlineLoadingState label="Loading settings" /><Skeleton className="mt-6 h-28 w-full" /><Skeleton className="mt-5 h-52 w-full" /></div></div></DialogContent></Dialog>}>
        <SettingsDialog key={`${request.identity}:${request.routeKey}`} initialTab={request.section} onClose={dismiss} onNavigate={closeSettings} onRestoreFocus={restoreFocus} />
      </Suspense>
    </ErrorBoundary>}
  </SettingsContext.Provider>;
}

/** Modified/new-tab clicks retain a deep link; ordinary clicks open in place. */
export function SettingsLink({ onClick, to = "/settings", ...props }: LinkProps) {
  const settings = useSettingsDialog();
  const search = typeof to === "string" ? to.split("?")[1] : to.search;
  return <Link {...props} to={to} aria-haspopup="dialog" onClick={event => {
    onClick?.(event);
    if (!settings || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || (props.target && props.target !== "_self")) return;
    event.preventDefault();
    settings.openSettings(new URLSearchParams(search).get("tab") ?? "profile", event.currentTarget);
  }} />;
}
