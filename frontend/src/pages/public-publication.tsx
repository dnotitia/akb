import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ArrowLeft, FileQuestion } from "lucide-react";
import { MarkdownRender } from "@/components/markdown-render";
import {
  getPublication,
  type PublicationResponse,
  type PublicationError,
} from "@/lib/api";
import { formatDate } from "@/lib/utils";
import { PasswordGate } from "@/components/password-gate";
import { SummaryFold } from "@/components/summary-fold";
import { TableViewer } from "@/components/table-viewer";
import { FileViewer } from "@/components/file-viewer";
import { ThemeToggle } from "@/components/theme-toggle";
import { Logo } from "@/components/logo";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export default function PublicationPage() {
  const { slug } = useParams<{ slug: string }>();
  const [data, setData] = useState<PublicationResponse | null>(null);
  const [error, setError] = useState<PublicationError | null>(null);
  const [needsPassword, setNeedsPassword] = useState(false);

  async function load() {
    if (!slug) return;
    // Reset stale state from previous param before re-fetch resolves.
    setData(null);
    setNeedsPassword(false);
    setError(null);
    try {
      const urlParams = new URLSearchParams(window.location.search);
      const params: Record<string, string> = {};
      urlParams.forEach((v, k) => {
        if (k !== "token" && k !== "password" && k !== "format") params[k] = v;
      });
      const result = await getPublication(slug, params);
      setData(result);
      setNeedsPassword(false);
    } catch (e) {
      const err = e as PublicationError;
      if (err.password_required) {
        setNeedsPassword(true);
      } else {
        setError(err);
      }
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  // Per-publication document title (WCAG 2.4.2). The password gate sets its
  // own neutral title (it must not leak a sealed doc's subject), so skip it
  // here while gated.
  useEffect(() => {
    if (needsPassword) return;
    if (data?.title) document.title = `${data.title} · AKB`;
    else if (error) document.title = "Unavailable · AKB";
  }, [data, error, needsPassword]);

  if (!slug) return null;

  if (needsPassword) {
    return <PasswordGate slug={slug} onSuccess={load} />;
  }

  if (error) {
    return <ErrorPage error={error} />;
  }

  if (!data) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background text-foreground">
        <div className="text-sm text-foreground-muted" role="status" aria-live="polite">
          Loading…
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Masthead */}
      <header className="border-b border-border">
        <div className="mx-auto max-w-[1200px] px-6 py-3 flex items-center justify-between gap-4">
          <a
            href="/"
            className="rounded-[var(--radius-md)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            aria-label="AKB home"
          >
            <Logo size={26} subtitle />
          </a>
          <div className="flex items-center gap-3">
            <Badge variant="secondary">Public</Badge>
            <ThemeToggle />
          </div>
        </div>
      </header>

      {/* Body */}
      <main className="mx-auto max-w-[1200px] px-6 py-12 fade-up">
        {data.resource_type === "document" && <DocumentBody data={data} />}
        {data.resource_type === "table_query" && (
          <TableViewer slug={slug} initialData={data} />
        )}
        {data.resource_type === "file" && <FileViewer slug={slug} data={data} />}
      </main>

      {/* Footer */}
      <footer className="border-t border-border mt-16">
        <div className="mx-auto max-w-[1200px] px-6 py-5 flex items-center justify-between flex-wrap gap-2">
          <div className="text-xs text-foreground-muted">© Dnotitia · Seahorse</div>
          <a
            href="/"
            className="inline-flex items-center gap-1.5 text-xs text-foreground-muted hover:text-link rounded-[var(--radius-sm)] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
            Back to AKB
          </a>
        </div>
      </footer>
    </div>
  );
}

function DocumentBody({ data }: { data: PublicationResponse }) {
  return (
    <article className="grid grid-cols-1 lg:grid-cols-[180px_1fr] gap-8">
      {/* Left rail — metadata */}
      <aside className="lg:sticky lg:top-8 lg:self-start">
        <div className="space-y-5">
          <MetaField label="Type" value={data.type || "document"} />
          {data.domain && <MetaField label="Domain" value={data.domain} />}
          {(() => {
            // Prefer the resolved author name; never surface a raw user UUID
            // on the public page. Fall back to a non-UUID created_by string.
            const isUuid = (s: string) =>
              /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s);
            const author =
              data.created_by_name ||
              (data.created_by && !isUuid(data.created_by) ? data.created_by : null);
            return author ? <MetaField label="Author" value={author} /> : null;
          })()}
          {data.updated_at && (
            <MetaField label="Updated" value={formatDate(data.updated_at)} tabular />
          )}
          {data.tags && data.tags.length > 0 && (
            <div>
              <div className="text-xs font-medium text-foreground-muted mb-2">Tags</div>
              <div className="flex flex-wrap gap-1">
                {data.tags.map((t) => (
                  <Badge key={t} variant="outline">
                    {t}
                  </Badge>
                ))}
              </div>
            </div>
          )}
          {data.section_filter && !data.section_not_found && (
            <div>
              <div className="text-xs font-medium text-foreground-muted mb-1">Section</div>
              <div className="text-sm font-medium border-l-2 border-primary pl-2 text-foreground">
                {data.section_filter}
              </div>
            </div>
          )}
        </div>
      </aside>

      {/* Main column */}
      <div className="min-w-0">
        <h1 className="font-display text-3xl lg:text-4xl font-semibold text-foreground leading-tight tracking-tight mb-6">
          {data.title}
        </h1>

        <SummaryFold summary={data.summary} prominent className="mt-4 mb-10" />

        {data.content_unavailable && (
          <Alert variant="destructive" title="Content unavailable" className="mb-8">
            The underlying document is no longer accessible from the base.
          </Alert>
        )}

        {data.section_not_found && (
          <Alert variant="warning" title="Section not found" className="mb-8">
            Section <code className="font-mono">{data.section_filter}</code> wasn't
            matched. Showing the full document.
          </Alert>
        )}

        <MarkdownRender markdown={data.content || ""} className="text-[15px]" />

      </div>
    </article>
  );
}

function MetaField({
  label,
  value,
  tabular,
}: {
  label: string;
  value: string;
  tabular?: boolean;
}) {
  return (
    <div>
      <div className="text-xs font-medium text-foreground-muted mb-1">{label}</div>
      <div className={`text-sm font-medium text-foreground ${tabular ? "tabular-nums" : ""}`}>
        {value}
      </div>
    </div>
  );
}

function ErrorPage({ error }: { error: PublicationError }) {
  let code = "Error";
  let title = "Something went wrong";
  let message = error.message;
  if (error.expired) {
    code = "410 · Expired";
    title = "This publication has expired";
    message =
      "The author set an expiry on this link and it has now passed. Ask them for a fresh one.";
  } else if (error.view_limit_reached) {
    code = "410 · View limit";
    title = "View limit reached";
    message =
      "This publication had a maximum number of views, and that quota has been spent.";
  } else if (error.not_found) {
    code = "404 · Not found";
    title = "Nothing here";
    message =
      "This publication doesn't exist or has been removed by its author.";
  }
  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-6 text-foreground">
      <div className="w-full max-w-lg fade-up">
        <div className="rounded-[var(--radius-lg)] border border-border bg-surface shadow-md p-10 text-center">
          <span
            className="inline-flex h-12 w-12 items-center justify-center rounded-[var(--radius-lg)] bg-surface-muted text-foreground-muted mx-auto"
            aria-hidden
          >
            <FileQuestion className="h-5 w-5" />
          </span>
          <div className="mt-4 text-xs font-medium text-foreground-muted">{code}</div>
          <h1 className="mt-1 font-display text-2xl font-semibold tracking-tight text-foreground">
            {title}
          </h1>
          <p className="mt-3 text-sm text-foreground-muted leading-relaxed max-w-sm mx-auto">
            {message}
          </p>
          <Button asChild variant="default" className="mt-6">
            <a href="/">
              <ArrowLeft className="h-4 w-4" aria-hidden />
              Back to AKB
            </a>
          </Button>
        </div>
      </div>
    </div>
  );
}
