import { useId, useLayoutEffect, useRef, useState } from "react";
import { Bell } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useNotificationCount } from "@/hooks/use-notifications";
import { NotificationInbox } from "@/components/notification-inbox";
import { NotificationReadFeedback } from "@/components/notification-read-feedback";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

export function NotificationBell() {
  const count = useNotificationCount();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<"all" | "unread">("all");
  const pendingNavigation = useRef<(() => void) | null>(null);
  const panel = useRef<HTMLDivElement>(null);
  const latestOpen = useRef(open);
  const titleId = useId();
  const descriptionId = useId();
  useLayoutEffect(() => { latestOpen.current = open; }, [open]);
  const unread = count.error ? undefined : count.data?.unread_count;
  const countLabel = unread === undefined ? "Notifications, unread count unavailable" : `Notifications, ${unread} unread`;
  const trigger = <Button id="notifications-trigger" variant="ghost" size="icon" className="relative h-9 w-9 shrink-0 rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover hover:text-foreground data-[state=open]:bg-surface-hover"
        aria-label={countLabel}>
        <Bell className="h-4 w-4 stroke-[1.7]" aria-hidden />
        {unread !== undefined && unread > 0 && <span data-notification-indicator aria-hidden className="absolute right-[7px] top-[5px] h-[5px] w-[5px] rounded-full bg-link" />}
      </Button>;
  const onCloseAutoFocus = (event: Event) => {
    // Do not steal focus if the user already reopened the panel.
    if (latestOpen.current) { event.preventDefault(); return; }
    const action = pendingNavigation.current;
    if (action) {
      event.preventDefault();
      pendingNavigation.current = null;
      window.requestAnimationFrame(action);
    }
  };
  return <><span className="sr-only" role="status" aria-live="polite" aria-atomic="true">{countLabel}</span>
    <Popover open={open} onOpenChange={setOpen} modal={false}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      {open && <NotificationInbox compact onClose={() => setOpen(false)}
        heading={<><h2 id={titleId} className="text-base font-semibold">Notifications</h2><p id={descriptionId} className="sr-only">Updates for you and the documents you watch.</p></>}
        state={state} onStateChange={setState} onNavigate={action => {
        pendingNavigation.current = action;
        setOpen(false);
      }} renderSurface={content => <PopoverContent ref={panel} data-testid="notification-panel" aria-labelledby={titleId} aria-describedby={descriptionId}
          className="flex w-[25.25rem] max-w-[min(calc(100vw-1rem),var(--radix-popover-content-available-width))] max-h-[min(40rem,var(--radix-popover-content-available-height))] flex-col gap-0 overflow-hidden rounded-[var(--radius-lg)] border-border bg-surface p-0 text-foreground shadow-lg"
          collisionPadding={8} onOpenAutoFocus={event => {
            const activeTab = panel.current?.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]');
            if (activeTab) { event.preventDefault(); activeTab.focus(); }
          }} onCloseAutoFocus={onCloseAutoFocus}>{content}</PopoverContent>} />}
    </Popover>
  <NotificationReadFeedback inboxOpen={open} onReviewCurrent={() => {
    setState("all"); setOpen(true);
  }} /></>;
}
