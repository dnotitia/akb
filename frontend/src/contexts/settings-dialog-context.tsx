import { createContext, lazy, Suspense, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, matchPath, useLocation, useNavigate, type LinkProps } from "react-router-dom";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { InlineLoadingState } from "@/components/ui/loading-state";
import { ErrorBoundary } from "@/components/error-boundary";

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
      <Suspense fallback={<Dialog open onOpenChange={open => { if (!open) dismiss(); }}><DialogContent onCloseAutoFocus={event => { event.preventDefault(); restoreFocus(); }}><DialogTitle>Settings</DialogTitle><DialogDescription className="sr-only">Loading your account settings.</DialogDescription><InlineLoadingState label="Loading settings" /></DialogContent></Dialog>}>
        <SettingsDialog key={request.routeKey} initialTab={request.section} onClose={dismiss} onRestoreFocus={restoreFocus} />
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
