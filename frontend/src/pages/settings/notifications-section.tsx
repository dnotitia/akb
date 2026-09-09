import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCurrentUser } from "@/contexts/current-user-context";
import { documentSubscription, notificationSubscriptions, NotificationsUnavailable } from "@/lib/api-notifications";
import { Panel, PanelHeader } from "@/components/ui/panel";
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
    catch { setError("Could not stop watching this document. Try again."); }
    finally { setBusy(null); }
  }
  return <Panel variant="workspace">
    <PanelHeader label="Watched documents" right={<Button variant="ghost" size="sm" asChild><Link to="/notifications">Open inbox</Link></Button>} />
    <p className="border-b border-border p-4 text-sm text-foreground-muted">Watch a document from its reader to receive updates. Your own edits stay quiet. Access changes appear automatically.</p>
    {query.isPending && <InlineLoadingState label="Loading watched documents" className="p-4" />}
    {(query.error || error) && <Alert variant="info" className="m-4">{error || (query.error instanceof NotificationsUnavailable ? query.error.message : "Could not load watched documents.")}<Button variant="ghost" size="sm" onClick={() => void query.refetch()}>Retry</Button></Alert>}
    {query.data?.items.length === 0 && <p className="p-6 text-sm text-foreground-muted">You aren’t watching any documents. Open a document and choose Watch to start.</p>}
    <ul className="divide-y divide-border">{query.data?.items.map(item => <li key={item.resource_id} className="flex items-center justify-between gap-3 p-4">
      <div className="min-w-0"><p className="truncate text-sm font-medium">{item.title}</p><p className="truncate text-xs text-foreground-muted">{item.vault}</p></div>
      <Button variant="outline" size="sm" disabled={busy !== null} loading={busy === item.uri} onClick={() => void unwatch(item.uri)}>Unwatch</Button>
    </li>)}</ul>
  </Panel>;
}
