import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, BellOff } from "lucide-react";
import { useCurrentUser } from "@/contexts/current-user-context";
import { documentSubscription, NotificationsUnavailable } from "@/lib/api-notifications";
import { Button } from "@/components/ui/button";

export function DocumentWatch({ uri }: { uri: string }) {
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
