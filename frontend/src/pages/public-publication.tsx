import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowUpRight } from "lucide-react";
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
import { Badge } from "@/components/ui/badge";

export default function PublicationPage() {
  const { slug } = useParams<{ slug: string }>();
  const [data, setData] = useState<PublicationResponse | null>(null);
  const [error, setError] = useState<PublicationError | null>(null);
  const [needsPassword, setNeedsPassword] = useState(false);

  async function load() {
    if (!slug) return;
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
        <div className="coord">— Loading —</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Coordinate strip */}
      <div className="border-b border-border">
        <div className="mx-auto max-w-[1200px] px-6 py-1 flex items-center justify-between">
          <div className="coord">§ AKB · PUBLIC PUBLICATION</div>
          <div className="coord hidden md:block">SLUG · {slug}</div>
          <div className="coord">
            {data.resource_type.replace("_", " ").toUpperCase()}
          </div>
        </div>
      </div>

      {/* Masthead */}
      <header className="border-b border-border">
        <div className="mx-auto max-w-[1200px] px-6 py-4 flex items-center justify-between gap-4">
          <a
            href="/"
            className="group flex items-baseline gap-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <span className="font-display text-3xl leading-none tracking-tight group-hover:text-accent transition-colors">
              AKB
            </span>
            <span className="coord hidden sm:inline">/ knowledgebase</span>
          </a>
          <div className="flex items-center gap-3">
            <Badge variant="spark">PUBLIC</Badge>
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
        <div className="mx-auto max-w-[1200px] px-6 py-4 flex items-center justify-between flex-wrap gap-2">
          <div className="coord">© Dnotitia / Seahorse</div>
          <a
            href="/"
            className="coord inline-flex items-center gap-1 hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <ArrowUpRight className="h-3 w-3" aria-hidden />
            AKB.HOME
          </a>
          <div className="coord">END OF DOCUMENT</div>
        </div>
      </footer>
    </div>
  );
}

function DocumentBody({ data }: { data: PublicationResponse }) {
  return (
    <article className="grid grid-cols-1 lg:grid-cols-[180px_1fr] gap-8">
      {/* Left rail — marginalia */}
      <aside className="lg:sticky lg:top-8 lg:self-start">
        <div className="space-y-5">
          <div>
            <div className="coord mb-1">TYPE</div>
            <div className="text-sm font-medium text-foreground">
              {data.type || "document"}
            </div>
          </div>
          {data.domain && (
            <div>
              <div className="coord mb-1">DOMAIN</div>
              <div className="text-sm font-medium text-foreground">{data.domain}</div>
            </div>
          )}
          {data.created_by && (
            <div>
              <div className="coord mb-1">AUTHOR</div>
              <div className="text-sm font-medium text-foreground">
                {data.created_by}
              </div>
            </div>
          )}
          {data.updated_at && (
            <div>
              <div className="coord mb-1">UPDATED</div>
              <div className="text-sm font-medium text-foreground tabular-nums">
                {formatDate(data.updated_at)}
              </div>
            </div>
          )}
          {data.tags && data.tags.length > 0 && (
            <div>
              <div className="coord mb-2">TAGS</div>
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
              <div className="coord mb-1">SECTION</div>
              <div className="text-sm font-medium border-l-2 border-accent pl-2 text-foreground">
                § {data.section_filter}
              </div>
            </div>
          )}
        </div>
      </aside>

      {/* Main column — Fraunces editorial body */}
      <div className="min-w-0">
        <div className="coord-spark mb-4">§ DOCUMENT</div>
        <h1 className="font-display-tight text-5xl lg:text-7xl text-foreground leading-[0.95] tracking-tight mb-6">
          {data.title}
        </h1>

        <SummaryFold summary={data.summary} prominent className="mt-4 mb-10" />

        {data.content_unavailable && (
          <div
            role="alert"
            aria-live="polite"
            className="border border-destructive p-4 mb-8"
          >
            <div className="coord-spark mb-1 text-destructive">⚠ CONTENT UNAVAILABLE</div>
            <p className="text-sm text-foreground">
              The underlying document is no longer accessible from the base.
            </p>
          </div>
        )}

        {data.section_not_found && (
          <div
            role="alert"
            aria-live="polite"
            className="border border-destructive p-4 mb-8"
          >
            <div className="coord-spark mb-1 text-destructive">⚠ SECTION NOT FOUND</div>
            <p className="text-sm text-foreground">
              Section{" "}
              <code className="font-mono">{data.section_filter}</code> wasn't
              matched. Showing the full document.
            </p>
          </div>
        )}

        <div className="prose dark:prose-invert prose-lg max-w-none font-display-body">
          <Markdown remarkPlugins={[remarkGfm]}>{data.content || ""}</Markdown>
        </div>
      </div>
    </article>
  );
}

function ErrorPage({ error }: { error: PublicationError }) {
  let coord = "§ ERROR";
  let title = "Something broke.";
  let message = error.message;
  if (error.expired) {
    coord = "§ 410 — EXPIRED";
    title = "This publication has expired.";
    message =
      "The author set an expiry on this link and it has now passed. Ask them for a fresh one.";
  } else if (error.view_limit_reached) {
    coord = "§ 410 — VIEW LIMIT";
    title = "View limit reached.";
    message =
      "This publication had a maximum number of views, and that quota has been spent.";
  } else if (error.not_found) {
    coord = "§ 404 — NOT FOUND";
    title = "Nothing here.";
    message =
      "This publication doesn't exist or has been removed by its author.";
  }
  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-6 text-foreground">
      <div className="w-full max-w-2xl fade-up">
        <div className="coord-spark mb-3">{coord}</div>
        <div className="border border-border bg-surface p-10 relative">
          <h1 className="font-display-tight text-6xl lg:text-7xl text-foreground leading-none">
            {title.split(" ").slice(0, -1).join(" ")}
            <br />
            <span className="text-accent italic">
              {title.split(" ").slice(-1)[0]}
            </span>
          </h1>
          <p className="mt-6 text-base text-foreground-muted leading-relaxed max-w-md">
            {message}
          </p>
          <a
            href="/"
            className="mt-8 inline-flex items-baseline gap-2 border-b-2 border-border hover:border-accent transition-colors pb-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <ArrowUpRight className="h-4 w-4 text-accent" aria-hidden />
            <span className="font-medium hover:text-accent">Take me to AKB</span>
          </a>
        </div>
      </div>
    </div>
  );
}
