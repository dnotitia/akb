import { useRef, useState, type ReactNode } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Bell, CheckCheck, Mail, MoreHorizontal, Settings2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { groupNotifications, notificationPresentation } from "@/lib/notification-presentation";
import { useNotificationInbox } from "@/hooks/use-notifications";
import { markNotification, markNotificationSnapshot, NotificationConflict, NotificationsUnavailable, NotificationCategoriesUnavailable, type NotificationItem, type NotificationCategory } from "@/lib/api-notifications";
import { parseUri } from "@/lib/uri";
import { documentPreviewState } from "@/lib/document-preview-navigation";
import { cn, timeAgo } from "@/lib/utils";

const categories = [
  { value: "all", label: "All" },
  { value: "documents", label: "Documents" },
  { value: "access", label: "Access" },
] as const;

export function NotificationInbox({ compact = false, heading, onNavigate, state, onStateChange, category, onCategoryChange }: {
  compact?: boolean;
  heading?: ReactNode;
  onNavigate?: (action: () => void) => void;
  state: "all" | "unread";
  onStateChange: (state: "all" | "unread") => void;
  category: NotificationCategory;
  onCategoryChange: (category: NotificationCategory) => void;
}) {
  const inbox = useNotificationInbox(state, category);
  const menuNavigation = useRef(false);
  const client = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const summary = inbox.data?.pages[0];
  const items = [...new Map(inbox.data?.pages.flatMap(page => page.items).map(item => [item.id, item]) || []).values()];
  const unavailable = inbox.error instanceof NotificationsUnavailable;
  const viewAllUrl = `/notifications?${new URLSearchParams({ category, state })}`;

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

  async function open(item: NotificationItem) {
    if (busy !== null) return;
    const target = parseUri(item.target?.uri);
    if (!target || target.vault !== item.target?.vault) return;
    const route = target.kind === "vault" ? `/vault/${encodeURIComponent(target.vault)}`
      : target.kind === "doc" ? `/vault/${encodeURIComponent(target.vault)}/doc/${encodeURIComponent(target.id)}` : null;
    if (!route) return;
    if (!item.read && !await change(() => markNotification(item, true), item.id)) return;
    const action = () => navigate(route, target.kind === "doc" ? {
      state: documentPreviewState(location, compact ? "notifications-trigger" : `notification-${item.id}`),
    } : undefined);
    if (onNavigate) onNavigate(action); else action();
  }

  return <TooltipProvider delayDuration={300}><Tabs value={category} onValueChange={value => { setError(null); onCategoryChange(value as NotificationCategory); }} className="flex min-h-0 flex-1 flex-col">
    <div className={cn("flex min-h-12 shrink-0 items-center justify-between gap-3 px-4 py-1", compact && "pr-12")}>
      <div>{heading ?? <h2 className="text-sm font-semibold">Inbox</h2>}</div>
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild><Button variant="ghost" size="icon" aria-label="Notification actions" loading={busy === "all"}><MoreHorizontal className="h-4 w-4" aria-hidden /></Button></DropdownMenu.Trigger>
        <DropdownMenu.Portal><DropdownMenu.Content align="end" sideOffset={4}
          className="z-[var(--z-popover)] min-w-56 rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-lg"
          onCloseAutoFocus={event => {
            if (!menuNavigation.current) return;
            event.preventDefault(); menuNavigation.current = false;
            window.requestAnimationFrame(() => {
              const action = () => navigate("/settings?tab=notifications");
              if (onNavigate) onNavigate(action); else action();
            });
          }}>
          <DropdownMenu.Item disabled={!summary || !summary.unread_count || busy !== null || !!inbox.error}
            className="flex min-h-9 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm outline-none focus:bg-surface-hover focus:text-link data-[disabled]:pointer-events-none data-[disabled]:opacity-50"
            onSelect={() => summary && void change(() => markNotificationSnapshot(summary.snapshot), "all")}><CheckCheck className="h-4 w-4" aria-hidden />Mark all notifications read</DropdownMenu.Item>
          <DropdownMenu.Separator className="my-1 h-px bg-border" />
          <DropdownMenu.Item className="flex min-h-9 cursor-pointer items-center gap-2 rounded-[var(--radius-sm)] px-3 py-2 text-sm outline-none focus:bg-surface-hover focus:text-link" onSelect={() => { menuNavigation.current = true; }}><Settings2 className="h-4 w-4" aria-hidden />Notification settings</DropdownMenu.Item>
        </DropdownMenu.Content></DropdownMenu.Portal>
      </DropdownMenu.Root>
    </div>
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-x-2 border-b border-border px-3">
    <TabsList aria-label="Notification category" className="flex justify-start gap-1 rounded-none bg-transparent p-0">
      {categories.map(({ value, label }) => <TabsTrigger key={value} value={value}
        className="relative min-h-9 gap-1.5 rounded-none px-2 py-2 text-sm after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 data-[state=active]:after:bg-link data-[state=active]:bg-transparent data-[state=active]:text-link data-[state=active]:shadow-none sm:px-3">
        {label}
      </TabsTrigger>)}
    </TabsList>
      <label className="flex min-h-9 cursor-pointer items-center gap-2 text-xs font-medium text-foreground-muted">
        <input type="checkbox" checked={state === "unread"} onChange={event => { setError(null); onStateChange(event.target.checked ? "unread" : "all"); }}
          className="h-4 w-4 accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2" />Unread only
      </label>
    </div>
    <TabsContent key={category} value={category} className={cn("pt-0 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", compact ? "min-h-0 flex-1 overflow-y-auto rail-scroll" : "min-h-60")} aria-busy={inbox.isFetching}>
      {error && <Alert variant="destructive" className="m-4">{error}</Alert>}
      {inbox.error && <div className="p-4"><Alert variant={unavailable ? "info" : "destructive"}>
        {unavailable ? inbox.error.message : "Could not load notifications. Your inbox may be out of date."}
        {inbox.error instanceof NotificationCategoriesUnavailable && <Button variant="outline" size="sm" className="mt-2" onClick={() => onCategoryChange("all")}>Show all categories</Button>}
        {!unavailable && <Button variant="outline" size="sm" className="mt-2" onClick={() => void inbox.refetch()}>Retry</Button>}
      </Alert></div>}
      {inbox.isPending && !inbox.error && <LoadingState label="Loading notifications" className="space-y-5 p-4">
        {[1, 2, 3].map(n => <div key={n} className="space-y-2"><Skeleton className="h-4 w-3/4" /><Skeleton className="h-3 w-1/2" /></div>)}
      </LoadingState>}
      {summary && !items.length && !inbox.error && <div className="px-6 py-12 text-center">
        <Bell className="mx-auto mb-3 h-6 w-6 text-foreground-muted" aria-hidden />
        <p className="text-sm font-medium">{state === "unread" ? "You’re all caught up" : category === "documents" ? "No document updates yet" : category === "access" ? "No invites or access updates yet" : "No notifications yet"}</p>
        <p className="mt-2 text-sm text-foreground-muted">{state === "unread" ? "There are no unread notifications in this category." : category === "access" ? "Vault invitations and changes to your access appear here." : "Watch a document to hear about changes."}</p>
        {state === "unread" && <Button variant="outline" size="sm" className="mt-4" onClick={() => onStateChange("all")}>Include read notifications</Button>}
      </div>}
      {groupNotifications(items).map((group, index) => <section key={`${group.label}-${index}`} aria-label={`${group.label} notifications`}>
      <h3 className="px-4 pb-1 pt-3 text-xs font-medium text-foreground-muted">{group.label}</h3>
      <ul aria-label={`${group.label} notifications`}>
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
          const metadataId = `notification-meta-${item.id}`;
          const content = <>
            <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", presentation.tone === "knowledge" ? "text-cat-1" : presentation.tone === "people" ? "text-cat-2" : "text-foreground-muted")} aria-hidden />
            <span className="min-w-0 flex-1">
              <span className={cn("block text-sm leading-5 [overflow-wrap:anywhere]", item.read ? "font-medium" : "font-semibold", canOpen && "text-link group-hover/notification:underline group-focus-visible/notification:underline underline-offset-2")}>
                {item.title}{canOpen && <ArrowRight className="ml-1 inline-block h-3 w-3 align-middle" aria-hidden />}
              </span>
              <span id={metadataId} className="sr-only">{`${item.read ? "Read" : "Unread"}. ${[label, vault].filter(Boolean).join(" · ")}. ${actionLabel}`}</span>
              <span aria-hidden className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs leading-4 text-foreground-muted">
                {label && <span>{label}</span>}
                {vault && <><span aria-hidden>·</span><span className="[overflow-wrap:anywhere]">{vault}</span></>}
                {!canOpen && <><span aria-hidden>·</span><span>Notice only</span></>}
              </span>
            </span>
          </>;
          return <li key={item.id} data-read={item.read} data-actionable={!!canOpen} className={cn("relative flex items-start gap-2 px-3 py-2 transition-token sm:px-4", canOpen && "hover:bg-surface-hover", !item.read && "before:absolute before:inset-y-2 before:left-0 before:w-0.5 before:rounded-full before:bg-link")}>
            {canOpen ? <button id={`notification-${item.id}`} aria-label={item.title} aria-describedby={metadataId} disabled={busy !== null} onClick={() => void open(item)} className="group/notification flex min-w-0 flex-1 cursor-pointer gap-3 rounded-[var(--radius-sm)] text-left text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50">{content}</button>
              : <div className="flex min-w-0 flex-1 gap-3 text-foreground">{content}</div>}
            <div className="flex shrink-0 items-center gap-1">
              <time className="text-xs leading-5 tabular-nums text-foreground-muted" dateTime={item.updated_at} title={new Date(item.updated_at).toLocaleString()}>{timeAgo(item.updated_at)}</time>
              <Tooltip>
                <TooltipTrigger asChild><Button variant="ghost" size="icon" className="h-9 w-9 text-foreground-muted hover:text-link" aria-label={item.read ? "Mark unread" : "Mark read"} disabled={busy !== null} loading={busy === item.id}
                  onClick={() => void change(() => markNotification(item, !item.read), item.id)}>
                  {item.read ? <Mail className="h-4 w-4" aria-hidden /> : <CheckCheck className="h-4 w-4" aria-hidden />}
                </Button></TooltipTrigger>
                <TooltipContent side="left">{item.read ? "Mark unread" : "Mark read"}</TooltipContent>
              </Tooltip>
            </div>
          </li>;
        })}
      </ul></section>)}
      {inbox.hasNextPage && <div className="border-t border-border p-4"><Button variant="outline" loading={inbox.isFetchingNextPage} onClick={() => void inbox.fetchNextPage()}>Load more</Button></div>}
    </TabsContent>
    {!compact && summary && <p className="border-t border-border px-4 py-3 text-xs text-foreground-muted">Notifications are kept for {summary.retention_days} days.</p>}
    {compact && <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border px-4 pt-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]">
      {summary && <span className="text-xs text-foreground-muted">Kept for {summary.retention_days} days</span>}
      <Link to={viewAllUrl} onClick={event => { if (onNavigate) { event.preventDefault(); onNavigate(() => navigate(viewAllUrl)); } }} className="inline-flex min-h-9 items-center gap-2 rounded-[var(--radius-sm)] text-sm font-medium text-link hover:text-link-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">View all notifications<ArrowRight className="h-3.5 w-3.5" aria-hidden /></Link>
    </div>}
  </Tabs></TooltipProvider>;
}
