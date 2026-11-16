import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, Unplug } from "lucide-react";
import { ConnectionsUnavailable, listConnections, type AIConnection } from "@/lib/api-connections";
import { formatDate, timeAgo } from "@/lib/utils";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/empty-state";
import { InlineLoadingState, LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";

interface Props {
  /** Load when the view is shown; reload each time it is shown again. */
  active: boolean;
  /** Bumped by the owner after a revoke so the list re-reads the server. */
  revision?: number;
  /** Keys revoked in this session, hidden before the server confirms. */
  revokedTokenIds?: readonly string[];
  /** Whether a key-based connection can be disconnected from here (its key is listed). */
  canDisconnect: (connection: AIConnection) => boolean;
  onDisconnect: (connection: AIConnection) => void;
  busy?: boolean;
}

function connectionLabel(connection: AIConnection) {
  return connection.client_name ?? "Unknown app";
}

/** AI apps that have used this account through MCP, most recently active first. */
export function ConnectedApps({ active, revision = 0, revokedTokenIds = [], canDisconnect, onDisconnect, busy = false }: Props) {
  const [connections, setConnections] = useState<AIConnection[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const generation = useRef(0);
  const load = useCallback(async (signal?: AbortSignal) => {
    const request = ++generation.current;
    setLoading(true);
    try {
      const next = await listConnections(signal);
      if (request !== generation.current) return;
      setConnections(next); setError(null);
    } catch (caught) {
      if (request !== generation.current || signal?.aborted) return;
      setError(caught instanceof Error ? caught : new Error("Could not load connected apps. Try again."));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [active, revision, load]);

  const visible = connections?.filter(connection => !connection.token_id || !revokedTokenIds.includes(connection.token_id));
  const unavailable = error instanceof ConnectionsUnavailable;
  return <section aria-labelledby="connected-apps-heading" aria-busy={loading}>
    <header className="mb-3 flex items-start justify-between gap-4 border-b border-border pb-3">
      <div>
        <h2 id="connected-apps-heading" className="text-base font-semibold">Connected apps</h2>
        <p className="mt-1 text-sm text-foreground-muted">AI apps that have used AKB through your account. An app appears after its first request.</p>
      </div>
      <Badge variant="default">{visible?.length ?? "··"}</Badge>
    </header>
    {loading && connections && <InlineLoadingState label="Updating connected apps" className="py-3" />}
    {error && connections && !unavailable && <Alert variant="warning">Could not update connected apps. Previous results are shown.<Button variant="outline" loading={loading} onClick={() => void load()}>Retry</Button></Alert>}
    {error && !connections ? <EmptyState title={unavailable ? "Connected apps unavailable" : "Couldn't load connected apps"} description={error.message} action={unavailable ? undefined : <Button variant="outline" loading={loading} onClick={() => void load()}>Retry</Button>} />
      : !visible ? <LoadingState label="Loading connected apps" className="space-y-3 py-3">{[0, 1].map(row => <Skeleton key={row} className="h-20 w-full" />)}</LoadingState>
      : visible.length === 0 ? <EmptyState title="No AI apps connected yet" description="Connect an AI tool in Connection setup. It appears here after it first uses AKB." />
      : <ul aria-label="Connected apps" className="divide-y divide-border overflow-hidden rounded-[var(--radius-md)] border border-border">{visible.map(connection => {
        const label = connectionLabel(connection);
        const keyBased = connection.auth_kind === "pat";
        const disconnectable = keyBased && canDisconnect(connection);
        return <li key={connection.id} className="flex min-w-0 flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="flex min-w-0 flex-1 items-start gap-3"><Bot className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden /><div className="min-w-0 flex-1 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="break-all text-sm font-semibold">{label}</span>
              {connection.client_version && <code className="text-xs text-foreground-muted">{connection.client_version}</code>}
              <Badge variant="default">{keyBased ? "Connection key" : "Sign-in"}</Badge>
            </div>
            {keyBased && <p className="break-words text-xs text-foreground-muted">Key {connection.token_name ?? "unnamed"}{connection.token_prefix ? <> · <code>{connection.token_prefix}••••</code></> : null}</p>}
            <p className="text-xs text-foreground-muted">First connected {formatDate(connection.first_seen_at)} · Last active {timeAgo(connection.last_seen_at)}</p>
          </div></div>
          {keyBased && <Button variant="ghost" size="sm" disabled={!disconnectable || busy} aria-label={`Disconnect ${label}`} title={disconnectable ? `Revokes key ${connection.token_name ?? ""}`.trim() : "This key is not in your key list."} onClick={() => onDisconnect(connection)}><Unplug className="h-3 w-3" aria-hidden />Disconnect</Button>}
        </li>;
      })}</ul>}
    {visible && visible.length > 0 && <p className="mt-3 text-xs text-foreground-muted">Disconnecting a key-based app revokes its key, so every app using that key loses access. To disconnect a sign-in app, sign out of AKB in that app. Last active updates at most once a minute.</p>}
  </section>;
}
