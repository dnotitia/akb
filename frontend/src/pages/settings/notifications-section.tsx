import { useState } from "react";
import { Link } from "react-router-dom";
import { FileText } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCurrentUser } from "@/contexts/current-user-context";
import { documentSubscription, notificationSubscriptions, NotificationsUnavailable } from "@/lib/api-notifications";
import { parseDocUri } from "@/lib/uri";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { InlineLoadingState } from "@/components/ui/loading-state";

export function NotificationsSection() {
  const user = useCurrentUser();
  const client = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const query = useQuery({ queryKey: ["notification-subscriptions", user?.user_id, "list"], queryFn: ({ signal }) => notificationSubscriptions(signal), enabled: !!user, retry: false });
  async function unwatch(uri: string) {
    setBusy(uri); setError(null);
    try { await documentSubscription(uri, "DELETE"); await client.invalidateQueries({ queryKey: ["notification-subscriptions"] }); }
    catch { setError("Could not stop watching this document. Try its Unwatch button again."); }
    finally { setBusy(null); }
  }
  return (
    <section aria-labelledby="watched-documents-heading" className="max-w-5xl">
      <div className="border-b border-border pb-4">
        <h2 id="watched-documents-heading" className="text-base font-semibold text-foreground">Watched documents</h2>
        <p className="mt-1 text-sm text-foreground-muted">Get notified when others update these documents. Choose Watch in a document to add it here.</p>
      </div>
      {query.isPending && <InlineLoadingState label="Loading watched documents" className="py-5" />}
      {query.error && <Alert variant="info" className="mt-4">{query.error instanceof NotificationsUnavailable ? query.error.message : "Could not load watched documents."}<Button variant="ghost" size="sm" onClick={() => void query.refetch()}>Retry</Button></Alert>}
      {error && <Alert variant="destructive" className="mt-4">{error}<Button variant="ghost" size="sm" onClick={() => setError(null)}>Dismiss</Button></Alert>}
      {query.data?.items.length === 0 && <p className="py-6 text-sm text-foreground-muted">You aren’t watching any documents yet.</p>}
      <ul className="divide-y divide-border" aria-busy={query.isFetching || busy !== null}>
        {query.data?.items.map(item => {
          const parsed = parseDocUri(item.uri);
          const href = parsed && parsed.vault === item.vault ? `/vault/${encodeURIComponent(parsed.vault)}/doc/${encodeURIComponent(parsed.id)}` : null;
          return (
            <li key={item.resource_id} className="flex items-center gap-3 py-3">
              <FileText className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden="true" />
              <div className="min-w-0 flex-1">
                {href ? <Link to={href} className="block w-fit max-w-full break-words rounded-[var(--radius-sm)] text-sm font-medium text-link hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2">{item.title}</Link> : <p className="break-words text-sm font-medium text-foreground">{item.title}</p>}
                <p className="mt-0.5 truncate text-xs text-foreground-muted">{item.vault}</p>
              </div>
              <Button variant="outline" size="sm" className="shrink-0" aria-label={`Unwatch ${item.title}`} disabled={busy !== null} loading={busy === item.uri} onClick={() => void unwatch(item.uri)}>Unwatch</Button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
