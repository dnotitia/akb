import { useRef, useState, type ReactNode } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Bell, Check, CheckCheck, ChevronRight, Loader2, Mail, MoreHorizontal, Settings2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Panel } from "@/components/ui/panel";
import { TonalIcon } from "@/components/ui/tonal-icon";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { groupNotifications, notificationPresentation } from "@/lib/notification-presentation";
import { useNotificationInbox } from "@/hooks/use-notifications";
import { markNotification, markNotificationSnapshot, NotificationConflict, NotificationsUnavailable, NotificationCategoriesUnavailable, type NotificationItem, type NotificationCategory } from "@/lib/api-notifications";
import { parseUri } from "@/lib/uri";
import { documentPreviewState } from "@/lib/document-preview-navigation";
import { cn, timeAgo } from "@/lib/utils";
import { useSettingsDialog } from "@/contexts/settings-dialog-context";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { notificationReadOwner, reportNotificationReadFailure } from "@/lib/notification-read-feedback";

const categories = [
  { value: "all", label: "All" },
  { value: "documents", label: "Documents" },
  { value: "access", label: "Vault access" },
] as const;

const menuItemClass = "flex min-h-11 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm outline-none focus:bg-surface-hover focus:text-link data-[disabled]:pointer-events-none data-[disabled]:opacity-50 sm:min-h-9";
const compactIconButtonClass = "h-11 w-11 text-foreground-muted sm:pointer-fine:h-8 sm:pointer-fine:w-8";

export function NotificationInbox({ compact = false, heading, onClose, onNavigate, state, onStateChange, category = "all", onCategoryChange, renderSurface }: {
  compact?: boolean;
  heading?: ReactNode;
  onClose?: () => void;
  onNavigate?: (action: () => void) => void;
  state: "all" | "unread";
  onStateChange: (state: "all" | "unread") => void;
  category?: NotificationCategory;
  onCategoryChange?: (category: NotificationCategory) => void;
  /** Keep query/mutation ownership mounted when the responsive shell changes. */
  renderSurface?: (content: ReactNode) => ReactNode;
}) {
  const queryCategory = compact ? "all" : category;
  const activeTab = compact ? state : category;
  const inbox = useNotificationInbox(state, queryCategory);
  const user = useCurrentUser();
  const { revision } = useAccessVerification();
  const client = useQueryClient();
  const navigate = useNavigate();
  const settings = useSettingsDialog();
  const location = useLocation();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const opening = useRef(false);
  const unreadTab = useRef<HTMLButtonElement>(null);
  const summary = inbox.data?.pages[0];
  const items = [...new Map(inbox.data?.pages.flatMap(page => page.items).map(item => [item.id, item]) || []).values()];
  const unavailable = inbox.error instanceof NotificationsUnavailable;
  const viewAllUrl = `/notifications?${new URLSearchParams({ category: queryCategory, state })}`;
  const openWatchedDocuments = (opener: HTMLElement | null) => {
    const action = () => settings ? settings.openSettings("notifications", opener) : navigate("/settings?tab=notifications");
    if (onNavigate) onNavigate(action); else action();
  };

  async function change(action: () => Promise<unknown>, id: string) {
    setBusy(id);
    setError(null);
    try {
      await action();
      await client.invalidateQueries({ queryKey: ["notifications"] });
      return true;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update notifications. Try again.");
      if (caught instanceof NotificationConflict) await client.invalidateQueries({ queryKey: ["notifications"] });
      return false;
    } finally { setBusy(null); }
  }

  async function toggleRead(item: NotificationItem, trigger: HTMLButtonElement) {
    if (busy !== null) return;
    const hadFocus = document.activeElement === trigger;
    const actions = [...(trigger.closest('[role="tabpanel"]')?.querySelectorAll<HTMLButtonElement>("[data-notification-read]") ?? [])];
    const index = actions.indexOf(trigger);
    const neighbor = actions[index + 1] ?? actions[index - 1];
    const changed = await change(() => markNotification(item, !item.read), item.id);
    if (changed && hadFocus && state === "unread" && !item.read) window.requestAnimationFrame(() => {
      // In Unread, the updated row leaves the list. Continue at its neighbor,
      // or return to the filter rather than losing keyboard focus to the body.
      if (document.activeElement !== document.body && document.activeElement !== trigger) return;
      if (neighbor?.isConnected) neighbor.focus(); else unreadTab.current?.focus();
    });
  }

  async function open(item: NotificationItem) {
    if (busy !== null || opening.current) return;
    const target = parseUri(item.target?.uri);
    if (!target || target.vault !== item.target?.vault) return;
    const route = target.kind === "vault" ? `/vault/${encodeURIComponent(target.vault)}`
      : target.kind === "doc" ? `/vault/${encodeURIComponent(target.vault)}/doc/${encodeURIComponent(target.id)}` : null;
    if (!route) return;
    opening.current = true;
    const owner = notificationReadOwner(user?.user_id ?? "", revision);
    // Read state must not gate access to the selected resource.
    if (!item.read) {
      setBusy(item.id); setError(null);
      void markNotification(item, true).then(() => client.invalidateQueries({ queryKey: ["notifications", owner.userId] })).catch(failure => {
        // Report to the shell immediately, without waiting for another GET or
        // depending on this route still being mounted.
        reportNotificationReadFailure(owner, item, failure);
        setError(failure instanceof Error ? failure.message : "Could not update notifications. Try again.");
        if (failure instanceof NotificationConflict) void client.invalidateQueries({ queryKey: ["notifications", owner.userId] });
      }).finally(() => { opening.current = false; setBusy(null); });
    }
    else opening.current = false;
    const action = () => navigate(route, target.kind === "doc" ? {
      state: documentPreviewState(location, compact ? "notifications-trigger" : `notification-${item.id}`),
    } : undefined);
    if (onNavigate) onNavigate(action); else action();
  }

  const cannotMarkAll = !summary || !summary.unread_count || busy !== null || !!inbox.error;
  const markAllHint = inbox.error ? "Unread count unavailable" : inbox.isPending ? "Loading notifications" : summary?.unread_count === 0 ? "No unread notifications" : "Mark all notifications read";
  const header = <div className={cn("flex shrink-0 items-center justify-between gap-2", compact ? "min-h-14 flex-nowrap px-4 py-2" : "flex-wrap pb-4")}>
      <div className={cn("flex min-w-0 items-center gap-3", compact && "flex-1 gap-2")}>
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          {heading ?? <h1 className="text-xl font-semibold">Notifications</h1>}
          {summary && !inbox.error && <span className="text-xs tabular-nums text-foreground-muted" aria-label={`${summary.unread_count} unread across all categories`}>{summary.unread_count} unread</span>}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
      {!compact && <Button variant="outline" size="sm" className="h-11 text-sm sm:h-9" onClick={event => openWatchedDocuments(event.currentTarget)}><Bell className="h-4 w-4" aria-hidden />Watched documents</Button>}
      {compact ? <Tooltip><TooltipTrigger asChild>
        <Button variant="ghost" size="icon" className={cn(compactIconButtonClass, "aria-disabled:cursor-default aria-disabled:opacity-50")} aria-label="Mark all notifications read" aria-disabled={cannotMarkAll} aria-busy={busy === "all" || undefined}
          onClick={() => { if (!cannotMarkAll && summary) void change(() => markNotificationSnapshot(summary.snapshot), "all"); }}>
          {busy === "all" ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden /> : <CheckCheck className="h-4 w-4" aria-hidden />}
        </Button>
      </TooltipTrigger><TooltipContent>{markAllHint}</TooltipContent></Tooltip> : <DropdownMenu.Root modal={false}>
        <DropdownMenu.Trigger asChild><Button variant="ghost" size="icon" className="h-11 w-11 sm:h-8 sm:w-8" aria-label="Notification actions" aria-busy={busy === "all" || undefined}>
          {busy === "all" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <MoreHorizontal className="h-4 w-4" aria-hidden />}
        </Button></DropdownMenu.Trigger>
        <DropdownMenu.Portal><DropdownMenu.Content align="end" sideOffset={4}
          className="z-[var(--z-popover)] min-w-56 rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-lg"
          >
          <DropdownMenu.Item disabled={!summary || !summary.unread_count || busy !== null || !!inbox.error}
            className={menuItemClass}
            onSelect={() => summary && void change(() => markNotificationSnapshot(summary.snapshot), "all")}><CheckCheck className="h-4 w-4" aria-hidden />Mark all notifications read</DropdownMenu.Item>
        </DropdownMenu.Content></DropdownMenu.Portal>
      </DropdownMenu.Root>}
      {onClose && <Button variant="ghost" size="icon" className={compactIconButtonClass} aria-label="Close notifications" onClick={onClose}><X className="h-4 w-4" aria-hidden /></Button>}
      </div>
    </div>;
  const tabClass = "relative min-h-11 gap-1.5 rounded-none px-2 py-2 text-sm after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 data-[state=active]:after:bg-link data-[state=active]:bg-transparent data-[state=active]:text-link data-[state=active]:shadow-none sm:min-h-10 sm:px-3";
  const filters = <div className="flex shrink-0 flex-wrap items-center justify-between gap-x-2 border-b border-border px-3">
    <TabsList aria-label={compact ? "Notification state" : "Notification category"} className="flex justify-start gap-2 rounded-none bg-transparent p-0">
      {compact ? <>
        <TabsTrigger value="all" className={tabClass}>All</TabsTrigger>
        <TabsTrigger ref={unreadTab} value="unread" className={tabClass}>Unread</TabsTrigger>
      </> : categories.map(({ value, label }) => <TabsTrigger key={value} value={value} className={tabClass}>
        {label}
      </TabsTrigger>)}
    </TabsList>
      {!compact && <label className="flex min-h-11 cursor-pointer items-center gap-2 text-xs font-medium text-foreground-muted sm:min-h-9">
        <input type="checkbox" aria-label="Unread only" checked={state === "unread"} onChange={event => { setError(null); onStateChange(event.target.checked ? "unread" : "all"); }}
          className="h-4 w-4 accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2" />Unread only
      </label>}
  </div>;
  const ledger = <TabsContent key={activeTab} value={activeTab} className={cn("min-w-0 bg-surface pt-0 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", compact ? "min-h-0 flex-1 overflow-y-auto overscroll-contain rail-scroll rail-scroll-auto" : "min-h-60")} aria-busy={inbox.isFetching}>
      {error && <Alert variant="destructive" className="m-4">{error}</Alert>}
      {inbox.error && <div className="p-4"><Alert variant={unavailable ? "info" : "destructive"}>
        {unavailable ? inbox.error.message : "Could not load notifications. Your inbox may be out of date."}
        {inbox.error instanceof NotificationCategoriesUnavailable && !compact && <Button variant="outline" size="sm" className="mt-2" onClick={() => onCategoryChange?.("all")}>Show all categories</Button>}
        {!unavailable && <Button variant="outline" size="sm" className="mt-2" onClick={() => void inbox.refetch()}>Retry</Button>}
      </Alert></div>}
      {inbox.isPending && !inbox.error && <LoadingState label="Loading notifications" className="space-y-3 p-4">
        {[1, 2, 3].map(n => <div key={n} className={cn("flex items-center gap-3 p-3", !compact && "rounded-[var(--radius-md)] border border-border")}><Skeleton className={compact ? "h-4 w-4 shrink-0" : "h-8 w-8 shrink-0"} /><div className="flex-1 space-y-2"><Skeleton className="h-4 w-3/4" /><Skeleton className="h-3 w-1/2" /></div></div>)}
      </LoadingState>}
      {summary && !items.length && !inbox.error && <div className={cn("flex flex-col items-center justify-center px-6 py-10 text-center", compact ? "min-h-40" : "min-h-64")}>
        <span className={cn("mb-3 inline-flex items-center justify-center text-foreground-muted", !compact && "mb-4 h-12 w-12 rounded-full bg-background")}>{state === "unread" ? <CheckCheck className={compact ? "h-6 w-6" : "h-5 w-5"} aria-hidden /> : <Bell className={compact ? "h-6 w-6" : "h-5 w-5"} aria-hidden />}</span>
        <p className="text-sm font-semibold">{state === "unread" ? "You’re all caught up" : category === "documents" ? "No document updates yet" : category === "access" ? "No vault access updates yet" : "No notifications yet"}</p>
        {!compact && <p className="mt-2 max-w-64 text-sm leading-6 text-foreground-muted">{state === "unread" ? "There are no unread notifications in this category." : category === "access" ? "Changes to your vault access appear here." : "Watch a document to hear about changes."}</p>}
        {!compact && state === "unread" && <Button variant="outline" size="sm" className="mt-4" onClick={() => onStateChange("all")}>Include read notifications</Button>}
      </div>}
      {(compact ? items.length ? [{ label: state === "unread" ? "Unread" : "All", items }] : [] : groupNotifications(items)).map((group, index) => <section key={`${group.label}-${index}`} aria-label={`${group.label} notifications`}>
      {!compact && <h3 className="px-4 pb-2 pt-4 text-xs font-medium text-foreground-muted">{group.label}</h3>}
      <ul aria-label={`${group.label} notifications`} className={compact ? "divide-y divide-border" : "space-y-2 px-3 pb-2"}>
        {group.items.map(item => {
          const target = parseUri(item.target?.uri);
          const canOpen = target && target.vault === item.target?.vault && ["doc", "vault"].includes(target.kind);
          const presentation = notificationPresentation(item.kind);
          const Icon = presentation.icon;
          const label = presentation.label ?? (item.message !== item.title ? item.message : null);
          // An access notice already uses its Vault as the title. Repeat context
          // only for a document, and never invent context for a redacted target.
          const vault = item.target?.vault !== item.title ? item.target?.vault : null;
          const actionLabel = canOpen ? target.kind === "vault" ? "Open vault" : "View document" : "Notice only. No destination is available.";
          const metadataId = `notification-${compact ? "panel" : "page"}-meta-${item.id}`;
          const content = <>
            {compact ? <span className="mt-0.5 h-4 w-4 shrink-0 text-foreground-muted" aria-hidden><Icon className="h-4 w-4" /></span> : <span className="relative shrink-0" aria-hidden>
              <TonalIcon tone={presentation.tone}><Icon /></TonalIcon>
              {!item.read && <span className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-info ring-2 ring-surface" />}
            </span>}
            <span className="min-w-0 flex-1">
              <span className={cn("block text-sm leading-5 [overflow-wrap:anywhere]", item.read ? "font-medium" : "font-semibold", canOpen ? "group-hover/notification:text-link group-focus-visible/notification:text-link" : "text-foreground-muted")}>
                {item.title}{!compact && canOpen && <ChevronRight className="ml-1 inline-block h-3 w-3 text-foreground-muted" aria-hidden />}
              </span>
              <span id={metadataId} className="sr-only">{`${item.read ? "Read" : "Unread"}. ${[label, vault].filter(Boolean).join(" · ")}. ${actionLabel}`}</span>
              <span className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs leading-4 text-foreground-muted">
              <span aria-hidden className="contents">
                {label && <span>{label}</span>}
                {vault && <><span aria-hidden>·</span><span className="[overflow-wrap:anywhere]">{vault}</span></>}
                {!canOpen && <><span aria-hidden>·</span><span>Notice only</span></>}
              </span>
                <span aria-hidden>·</span><time className="tabular-nums" dateTime={item.updated_at} title={new Date(item.updated_at).toLocaleString()}>{timeAgo(item.updated_at)}</time>
              </span>
            </span>
          </>;
          return <li key={item.id} data-read={item.read} data-actionable={!!canOpen} className={cn("group/notification-row relative flex items-start gap-2 transition-token", compact ? "px-4 py-3" : "rounded-[var(--radius-md)] border p-3", !compact && (item.read ? "border-border bg-surface" : "border-info bg-info-soft"), canOpen && (compact ? "hover:bg-surface-hover" : "hover:border-border-strong hover:bg-surface-hover"))}>
            {canOpen ? <Link id={compact ? `notification-panel-${item.id}` : `notification-${item.id}`} aria-label={item.title} aria-describedby={metadataId}
              to={target.kind === "vault" ? `/vault/${encodeURIComponent(target.vault)}` : `/vault/${encodeURIComponent(target.vault)}/doc/${encodeURIComponent(target.id)}`}
              state={target.kind === "doc" ? documentPreviewState(location, compact ? "notifications-trigger" : `notification-${item.id}`) : undefined}
              onClick={event => {
                if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                event.preventDefault(); void open(item);
              }} className="group/notification flex min-h-10 min-w-0 flex-1 cursor-pointer items-start gap-3 rounded-[var(--radius-sm)] text-left text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">{content}</Link>
              : <div className="flex min-w-0 flex-1 gap-3 text-foreground">{content}</div>}
            {compact ? <Tooltip><TooltipTrigger asChild>
              <Button variant="ghost" size="icon" data-notification-read className="group/read-toggle mt-px h-11 w-11 shrink-0 text-foreground-muted aria-disabled:cursor-default sm:pointer-fine:h-6 sm:pointer-fine:w-6"
                aria-label={`Mark ${item.read ? "unread" : "read"}: ${item.title}`} aria-disabled={busy !== null} aria-busy={busy === item.id || undefined}
                onClick={event => { void toggleRead(item, event.currentTarget); }}>
                {busy === item.id ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden /> : item.read
                  ? <Check className="h-4 w-4 opacity-0 group-hover/notification-row:opacity-100 group-focus-visible/read-toggle:opacity-100 max-sm:opacity-100 pointer-coarse:opacity-100" aria-hidden />
                  : <span className="h-1.5 w-1.5 rounded-full bg-link" aria-hidden />}
              </Button>
            </TooltipTrigger><TooltipContent side="left">Mark {item.read ? "unread" : "read"}</TooltipContent></Tooltip> : <DropdownMenu.Root modal={false}>
              <DropdownMenu.Trigger asChild><Button variant="ghost" size="icon" className="h-11 w-11 shrink-0 text-foreground-muted sm:h-8 sm:w-8" aria-label={`Actions for notification: ${item.title}`} aria-busy={busy === item.id || undefined}>
                {busy === item.id ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden /> : <MoreHorizontal className="h-4 w-4" aria-hidden />}
              </Button></DropdownMenu.Trigger>
              <DropdownMenu.Portal><DropdownMenu.Content align="end" sideOffset={4} className="z-[var(--z-popover)] min-w-44 rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md">
                <DropdownMenu.Item disabled={busy !== null} className={menuItemClass} onSelect={() => void change(() => markNotification(item, !item.read), item.id)}>
                  {item.read ? <Mail className="h-4 w-4" aria-hidden /> : <CheckCheck className="h-4 w-4" aria-hidden />}{item.read ? "Mark unread" : "Mark read"}
                </DropdownMenu.Item>
              </DropdownMenu.Content></DropdownMenu.Portal>
            </DropdownMenu.Root>}
          </li>;
        })}
      </ul></section>)}
      {inbox.hasNextPage && <div className="border-t border-border p-4"><Button variant="outline" loading={inbox.isFetchingNextPage} onClick={() => void inbox.fetchNextPage()}>Load more</Button></div>}
    </TabsContent>;
  const retention = !compact && summary && <p className="border-t border-border px-4 py-3 text-xs text-foreground-muted">Notifications are kept for {summary.retention_days} days.</p>;
  const footer = compact && <div className="flex min-h-12 shrink-0 items-center justify-between gap-2 border-t border-border bg-surface px-4">
    <Link to={viewAllUrl} onClick={event => { if (onNavigate && event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); onNavigate(() => navigate(viewAllUrl)); } }} className="inline-flex min-h-11 min-w-0 items-center text-sm font-medium text-link hover:text-link-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">View all notifications</Link>
    <Tooltip><TooltipTrigger asChild><Button variant="ghost" size="icon" className={compactIconButtonClass} aria-label="Watched documents" onClick={() => openWatchedDocuments(document.getElementById("notifications-trigger"))}><Settings2 className="h-4 w-4" aria-hidden /></Button></TooltipTrigger><TooltipContent>Manage watched documents</TooltipContent></Tooltip>
  </div>;
  const content = <TooltipProvider delayDuration={300}><Tabs value={activeTab} onValueChange={value => { setError(null); if (compact) onStateChange(value as "all" | "unread"); else onCategoryChange?.(value as NotificationCategory); }} className={cn("flex min-h-0 flex-1 flex-col", compact && "[&_svg]:stroke-[1.7]")}>
    {header}{compact ? <>{filters}{ledger}{footer}</> : <Panel variant="workspace">{filters}{ledger}{retention}</Panel>}
  </Tabs></TooltipProvider>;
  return renderSurface ? renderSurface(content) : content;
}
