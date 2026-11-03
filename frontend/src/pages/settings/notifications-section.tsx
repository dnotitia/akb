import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { FileText } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCurrentUser } from "@/contexts/current-user-context";
import { documentSubscription, notificationSubscriptions, NotificationsUnavailable } from "@/lib/api-notifications";
import { parseDocUri } from "@/lib/uri";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { LoadingState, InlineLoadingState } from "@/components/ui/loading-state";
import { SettingsHeading } from "./settings-ui";

export function NotificationsSection({ onBusyChange }: { onBusyChange?: (busy: boolean) => void }) {
  const user = useCurrentUser();
  const client = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { onBusyChange?.(busy !== null); }, [busy, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);
  const query = useQuery({ queryKey: ["notification-subscriptions", user?.user_id, "list"], queryFn: ({ signal }) => notificationSubscriptions(signal), enabled: !!user, retry: false });
  async function unwatch(uri: string) {
    setBusy(uri); setError(null);
    try { await documentSubscription(uri, "DELETE"); await client.invalidateQueries({ queryKey: ["notification-subscriptions"] }); }
    catch { setError("Could not stop watching this document. Try its Unwatch button again."); }
    finally { setBusy(null); }
  }
  return (
    <section aria-labelledby="watched-documents-heading">
      <SettingsHeading id="watched-documents-heading" title="Watched documents" description="Keep up with the documents that matter to your work." />
      <p className="settings-supporting-note">Choose Watch in any document to receive updates and manage it here.</p>
      {query.isPending && <LoadingState label="Loading watched documents" className="space-y-3 py-5">{[0, 1, 2].map(row => <Skeleton key={row} className="h-14 w-full" />)}</LoadingState>}
      {query.isFetching && query.data && <InlineLoadingState label="Updating watched documents" className="py-3" />}
      {query.error && <Alert variant="info" className="mt-4">{query.error instanceof NotificationsUnavailable ? query.error.message : "Could not load watched documents."}<Button variant="ghost" size="sm" loading={query.isFetching} onClick={() => void query.refetch()}>Retry</Button></Alert>}
      {error && <Alert variant="destructive" className="mt-4">{error}<Button variant="ghost" size="sm" onClick={() => setError(null)}>Dismiss</Button></Alert>}
      {query.data?.items.length === 0 && <div className="settings-empty"><FileText aria-hidden /><h3>No documents to follow yet</h3><p>You aren’t watching any documents yet.</p><p>Open a document and choose Watch to add it here.</p></div>}
      <ul className={query.data?.items.length ? "settings-watch-list" : ""} aria-busy={query.isFetching || busy !== null}>
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
