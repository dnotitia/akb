import { CheckCircle2, CircleAlert, CircleHelp, CircleSlash } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { outcomes, type FailureDetail, type McpOperation, type RecentCall } from "@/lib/mcp-monitoring";

function Status({ tone, label }: { tone: "ok" | "error" | "cancelled" | "unknown"; label: string }) {
  const Icon = { ok: CheckCircle2, error: CircleAlert, cancelled: CircleSlash, unknown: CircleHelp }[tone];
  const variant = { ok: "active", error: "error", cancelled: "pending", unknown: "default" } as const;
  return <Badge variant={variant[tone]} className="inline-flex items-center gap-1 whitespace-nowrap">
    <Icon size={13} aria-hidden />{label}
  </Badge>;
}

type CodeHistory = { codesSince?: string | null };

function ErrorReason({ error_code, error_reason, error_diagnostic, finished_at, codesSince }: FailureDetail & CodeHistory & { finished_at: string }) {
  const beforeCollection = codesSince && Date.parse(finished_at) < Date.parse(codesSince);
  return <div className="mt-1 max-w-72 whitespace-normal text-xs leading-5 text-foreground-muted">
    {error_code ? <><code className="break-all font-medium text-foreground">{error_code}</code>{(error_diagnostic || error_reason) && <p>{error_diagnostic || error_reason}</p>}</>
      : beforeCollection ? <>This call predates error-code collection, available from <time dateTime={codesSince}>{new Date(codesSince).toLocaleString()}</time>.</>
        : "AKB recorded the failure without an error code."}
  </div>;
}

export function CallOutcome({ call, codesSince }: CodeHistory & { call: Pick<RecentCall, "finished_at" | "outcome" | "error_code" | "error_reason" | "error_diagnostic"> }) {
  const failed = call.outcome === "tool_error" || call.outcome === "rpc_error";
  return <><Status tone={failed ? "error" : call.outcome === "ok" ? "ok" : call.outcome === "cancelled" ? "cancelled" : "unknown"}
    label={outcomes[call.outcome]} />{failed && <ErrorReason {...call} codesSince={codesSince} />}</>;
}

export function OperationOutcome({ op, codesSince }: CodeHistory & { op: McpOperation }) {
  if (op.succeeded == null) return <Status tone="unknown" label="Outcome unavailable" />;
  const unknown = op.calls - op.succeeded - op.cancelled - op.errors;
  if (op.succeeded === op.calls) return <Status tone="ok" label="Success" />;
  if (op.cancelled === op.calls) return <Status tone="cancelled" label="Cancelled" />;
  if (unknown === op.calls) return <Status tone="unknown" label="Unknown" />;
  return <><Status tone={op.errors ? "error" : "unknown"}
    label={op.errors === op.calls ? "Failed" : op.errors ? "Has errors" : "Mixed outcomes"} />
    {op.calls === 1 && op.errors > 0 && <ErrorReason {...op} codesSince={codesSince} />}
    {op.calls > 1 && <div className="mt-1 text-xs text-foreground-muted">
      {[op.errors && `${op.errors} failed`, op.succeeded && `${op.succeeded} succeeded`,
        op.cancelled && `${op.cancelled} cancelled`, unknown && `${unknown} unknown`].filter(Boolean).join(" · ")}
    </div>}
  </>;
}
