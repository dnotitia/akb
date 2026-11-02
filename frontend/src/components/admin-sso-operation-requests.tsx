import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/ui/loading-state";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { Skeleton } from "@/components/ui/skeleton";
import { OPERATION_REQUESTS_QUERY_KEY, OWNERSHIP_QUERY_ROOT } from "@/lib/admin-sso-queries";
import {
  ApiError,
  approveAdminSsoOperationRequest,
  denyAdminSsoOperationRequest,
  getAdminSsoOperationRequests,
  type AdminSsoOperationRequest,
} from "@/lib/api";

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" }) : "-";
}

function errorCode(caught: unknown): string | null {
  const detail = caught instanceof ApiError ? (caught.detail as { code?: unknown } | null) : null;
  return typeof detail?.code === "string" ? detail.code : null;
}

/**
 * Requests an administrator service key proposed. A person approves or denies
 * exactly the request digest shown here. The operation credential never comes
 * to this page: only the requesting service account can collect it, sealed to
 * a key it generated for that request.
 */
export function SsoOperationRequests() {
  const queryClient = useQueryClient();
  const query = useQuery<AdminSsoOperationRequest[]>({
    queryKey: OPERATION_REQUESTS_QUERY_KEY,
    queryFn: getAdminSsoOperationRequests,
    staleTime: 10_000,
  });
  const [deciding, setDeciding] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState("");
  const [error, setError] = useState("");

  async function decide(request: AdminSsoOperationRequest, decision: "approve" | "deny") {
    setDeciding(request.request_id);
    setNotice("");
    setConflict("");
    setError("");
    try {
      const decided = await (decision === "approve" ? approveAdminSsoOperationRequest : denyAdminSsoOperationRequest)(
        request.request_id,
        request.request_sha256,
      );
      setNotice(decided.status === "denied"
        ? `The request for ${decided.request.provider_alias} was denied.`
        : decided.request.kind === "ownership_claim"
          ? `${decided.request.owner_id} now owns ${decided.request.provider_alias}.`
          : `The operation on ${decided.request.provider_alias} was approved. Only the requesting service account can collect its credential.`);
      await queryClient.invalidateQueries({ queryKey: OWNERSHIP_QUERY_ROOT });
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        const code = errorCode(caught);
        setConflict(`This request or its provider changed since this screen loaded, so it was not ${decision === "approve" ? "approved" : "denied"}.${code ? ` (${code})` : ""} The list below is current.`);
      } else {
        setError(caught instanceof Error ? caught.message : "The decision could not be recorded.");
      }
    } finally {
      setDeciding(null);
      await query.refetch();
    }
  }

  const accessDenied = [401, 403].includes((query.error as { status?: number } | null)?.status ?? 0);
  const requests = accessDenied ? undefined : query.data;
  const retry = (
    <Button variant="outline" size="sm" loading={query.isFetching} onClick={() => void query.refetch()}>
      Retry requests
    </Button>
  );

  return (
    <section className="space-y-4" aria-labelledby="sso-operation-requests-heading" aria-busy={query.isFetching}>
      <div>
        <h2 id="sso-operation-requests-heading" className="font-display text-xl font-semibold tracking-tight">
          Operation requests
        </h2>
        <p className="mt-1 text-sm leading-6 text-foreground-muted">
          An administrator service key asks; you approve or deny the exact request shown. No operation
          credential appears here. Only the requesting service account can collect it, once, sealed to its own key.
        </p>
      </div>
      {notice && <Alert variant="success">{notice}</Alert>}
      {conflict && <Alert variant="warning" title="The screen was out of date">{conflict}</Alert>}
      {error && <Alert variant="destructive">{error}</Alert>}
      {query.isPending ? (
        <LoadingState label="Loading operation requests">
          <Skeleton className="h-24 w-full rounded-[var(--radius-lg)]" />
        </LoadingState>
      ) : !requests ? (
        <Alert variant="destructive" title="Operation requests could not be loaded">
          No request is shown because the list did not load. {retry}
        </Alert>
      ) : (
        <OperationRequestLists requests={requests} deciding={deciding} onDecide={decide} stale={query.isError} retry={retry} />
      )}
    </section>
  );
}

function OperationRequestLists({
  requests,
  deciding,
  onDecide,
  stale,
  retry,
}: {
  requests: AdminSsoOperationRequest[];
  deciding: string | null;
  onDecide: (request: AdminSsoOperationRequest, decision: "approve" | "deny") => void;
  stale: boolean;
  retry: React.ReactNode;
}) {
  const pending = requests.filter((request) => request.status === "pending");
  const awaiting = requests.filter((request) => request.status === "approved" && request.request.kind === "provider_grant");
  return (
    <div className="space-y-4">
      {stale && <Alert variant="warning">Could not refresh the requests. The previous list is shown. {retry}</Alert>}
      {pending.length === 0 ? (
        <Alert variant="info">No operation request is waiting for a decision.</Alert>
      ) : (
        <Panel flush>
          <PanelHeader label="Waiting for a decision" count={pending.length} />
          <ul className="divide-y divide-border">
            {pending.map((request) => (
              <PendingRequest key={request.request_id} request={request} deciding={deciding} onDecide={onDecide} />
            ))}
          </ul>
        </Panel>
      )}
      {awaiting.length > 0 && (
        <Panel flush>
          <PanelHeader label="Approved, not yet collected" count={awaiting.length} />
          <ul className="divide-y divide-border">
            {awaiting.map((request) => (
              <li key={request.request_id} className="p-4 text-sm">
                <p className="font-medium">{request.request.provider_alias} · {request.request.grant?.verbs.join(", ")}</p>
                <p className="mt-1 text-xs leading-5 text-foreground-muted">
                  Approved by {request.decider_username ?? request.decided_by}. The requesting service account can collect
                  the credential until {when(request.operation_expires_at)}.
                </p>
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  );
}

function Term({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-foreground-muted">{label}</dt>
      <dd className="mt-0.5 break-all text-sm">{children}</dd>
    </div>
  );
}

function PendingRequest({
  request,
  deciding,
  onDecide,
}: {
  request: AdminSsoOperationRequest;
  deciding: string | null;
  onDecide: (request: AdminSsoOperationRequest, decision: "approve" | "deny") => void;
}) {
  const { claim, grant } = request.request;
  const summary = grant?.configure_summary ?? null;
  const label = request.request.kind === "ownership_claim" ? "Ownership claim" : "Provider operation";
  return (
    <li className="space-y-3 p-4" aria-label={`${label} for ${request.request.provider_alias}`}>
      <div className="flex flex-wrap items-center gap-2">
        <p className="font-medium">{label}</p>
        <Badge variant="pending">Pending</Badge>
      </div>
      <dl className="grid gap-3 sm:grid-cols-2">
        <Term label="Requested by">{request.requester_username ?? request.request.requested_by}</Term>
        <Term label="Provider">{request.request.provider_alias}</Term>
        <Term label="Owner">{request.request.owner_id}</Term>
        {claim && <Term label="Expected revision">{claim.expected_revision}</Term>}
        {grant && <Term label="Verbs">{grant.verbs.join(", ")}</Term>}
        {grant && <Term label="Expected revision">{grant.expected_provider_revision}</Term>}
        {grant && <Term label="Credential lifetime">{grant.ttl_seconds} seconds after approval</Term>}
        {grant?.provider_secret_revision && <Term label="Secret revision">{grant.provider_secret_revision}</Term>}
        {summary && <Term label="Display name">{summary.display_name}</Term>}
        {summary && <Term label="Client ID">{summary.client_id}</Term>}
        {summary && <Term label="Issuer">{summary.issuer}</Term>}
        {summary && <Term label="Discovery URL">{summary.discovery_url}</Term>}
        {summary && <Term label="Provider type">{summary.provider_type}</Term>}
        {summary && (
          <Term label="Client secret">
            {summary.client_secret_supplied ? "Secret held by the requester, not shown" : "Not changed by this request"}
          </Term>
        )}
        <Term label="Request expires">{when(request.expires_at)}</Term>
      </dl>
      <p className="text-xs leading-5 text-foreground-muted">
        Request digest <span className="coord break-all">{request.request_sha256}</span>
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          disabled={deciding !== null}
          loading={deciding === request.request_id}
          onClick={() => onDecide(request, "approve")}
        >
          Approve
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={deciding !== null}
          onClick={() => onDecide(request, "deny")}
        >
          Deny
        </Button>
      </div>
    </li>
  );
}
