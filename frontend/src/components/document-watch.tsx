import { useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, BellOff } from "lucide-react";
import { useCurrentUser } from "@/contexts/current-user-context";
import { documentSubscription, NotificationsUnavailable } from "@/lib/api-notifications";
import { Button } from "@/components/ui/button";

export function DocumentWatch({ uri, presentation = "button" }: { uri: string; presentation?: "button" | "menu" }) {
  const user = useCurrentUser();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = ["notification-subscriptions", user?.user_id, uri];
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => documentSubscription(uri, "GET", signal), enabled: !!user, retry: false });
  const unavailable = query.error instanceof NotificationsUnavailable;
  const Icon = query.data?.subscribed ? BellOff : Bell;
  async function toggle() {
    setBusy(true); setError(null);
    try {
      const data = await documentSubscription(uri, query.data?.subscribed ? "DELETE" : "PUT");
      client.setQueryData(key, data);
      await client.invalidateQueries({ queryKey: ["notification-subscriptions", user?.user_id, "list"] });
    } catch { setError("Could not change Watch. Try again."); }
    finally { setBusy(false); }
  }
  if (!user) return null;
  if (presentation === "menu") return <>
    <DropdownMenu.CheckboxItem checked={query.data?.subscribed ?? false} disabled={unavailable || busy || query.isPending}
      onSelect={event => { event.preventDefault(); if (query.error) void query.refetch(); else void toggle(); }}
      className="flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2.5 py-2 text-sm outline-none data-[highlighted]:bg-surface-hover data-[disabled]:opacity-50">
      <Icon className="h-4 w-4 text-foreground-muted" aria-hidden />
      {unavailable ? "Watch unavailable" : busy ? "Updating Watch…" : query.error ? "Retry Watch" : query.data?.subscribed ? "Unwatch" : "Watch"}
    </DropdownMenu.CheckboxItem>
    {error && <p role="alert" className="max-w-64 px-2.5 py-2 text-xs text-destructive">{error}</p>}
  </>;
  return <div className="flex flex-wrap items-center gap-2">
    <Button variant="ghost" size="sm" className="h-9 gap-1.5 text-xs" loading={busy || query.isPending}
      disabled={unavailable} aria-pressed={query.data?.subscribed ?? false}
      onClick={() => query.error ? void query.refetch() : void toggle()}>
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {unavailable ? "Watch unavailable" : query.error ? "Retry Watch" : query.data?.subscribed ? "Unwatch" : "Watch"}
    </Button>
    {error && <span role="alert" className="text-xs text-destructive">{error}</span>}
  </div>;
}
