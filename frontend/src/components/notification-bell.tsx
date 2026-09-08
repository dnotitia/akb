import { useRef, useState } from "react";
import { Bell } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle, DialogDescription, DialogTrigger } from "@/components/ui/dialog";
import { useNotificationCount } from "@/hooks/use-notifications";
import { NotificationInbox } from "@/components/notification-inbox";
import type { NotificationCategory } from "@/lib/api-notifications";

export function NotificationBell() {
  const count = useNotificationCount();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<"all" | "unread">("all");
  const [category, setCategory] = useState<NotificationCategory>("all");
  const pendingNavigation = useRef<(() => void) | null>(null);
  const unread = count.error ? undefined : count.data?.unread_count;
  const countLabel = unread === undefined ? "Notifications, unread count unavailable" : `Notifications, ${unread} unread`;
  return <Dialog open={open} onOpenChange={setOpen}>
    <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">{countLabel}</span>
    <DialogTrigger asChild>
      <Button id="notifications-trigger" variant="ghost" size="icon" className="relative h-9 w-9 shrink-0"
        aria-label={countLabel}>
        <Bell className="h-4 w-4" aria-hidden />
        {unread !== undefined && unread > 0 && <span aria-hidden className="absolute -right-1 -top-1 min-w-4 rounded-full bg-primary px-1 text-xs font-medium tabular-nums text-primary-foreground">{unread > 99 ? "99+" : unread}</span>}
      </Button>
    </DialogTrigger>
    <DialogContent data-testid="notification-panel" className="left-0 right-0 top-0 flex h-dvh max-h-dvh w-screen max-w-none translate-x-0 translate-y-0 flex-col gap-0 overflow-hidden rounded-none p-0 sm:left-auto sm:right-3 sm:top-16 sm:h-auto sm:max-h-[calc(100dvh-5rem)] sm:w-[30rem] sm:rounded-[var(--radius-xl)]"
      overlayProps={{ className: "sm:bg-transparent" }}
      onCloseAutoFocus={event => {
        const action = pendingNavigation.current;
        if (action) {
          event.preventDefault();
          pendingNavigation.current = null;
          window.requestAnimationFrame(action);
        }
      }}>
      <NotificationInbox compact heading={<><DialogTitle>Notifications</DialogTitle><DialogDescription className="sr-only">Updates for you and the documents you watch.</DialogDescription></>} state={state} onStateChange={setState} category={category} onCategoryChange={setCategory} onNavigate={action => {
        pendingNavigation.current = action;
        setOpen(false);
      }} />
    </DialogContent>
  </Dialog>;
}
