import { useMemo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Loader2 } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { getDocument, getVaultSkillPreview } from "@/lib/api";
import { sanitizeLinkUrl } from "@/lib/utils";
import { parseHeadings, slugify } from "@/lib/markdown";
import { SkillBanner } from "@/components/skill/skill-banner";
import { Skeleton } from "@/components/ui/skeleton";

type ViewMode = "rendered" | "raw" | "agent";

interface DocumentViewProps {
  vault: string;
  docId: string;
  /** Controlled view — provide this + onViewChange to sync with URL params */
  view?: ViewMode;
  onViewChange?: (next: ViewMode) => void;
  /**
   * Optional extra segmented-control tab appended after RENDERED/RAW/AGENT.
   * The parent owns the click handler — DocumentView does not switch
   * its own view state when the extra tab is clicked. Used by
   * DocumentPage to inject the body-editor entry point without
   * folding the editor into this read-focused component.
   */
  extraTab?: { label: string; onClick: () => void };
  /**
   * Optional git commit hash. When set, the body is fetched at that
   * commit via getDocument(..., version) and the queryKey carries the
   * hash so commit-log / history selections render the historical body
   * instead of HEAD. Parent (DocumentPage) reads it from ?commit= URL
   * state; uncontrolled callers (VaultSkillPage) omit it and get HEAD.
   */
  version?: string;
}

/**
 * Self-sufficient doc body: fetches the document, renders the
 * rendered/raw segmented control, and shows the markdown content.
 *
 * Query key is ["document", vault, docId, version] — matches DocumentPage
 * exactly so TanStack Query dedupes when both are mounted. Without
 * `version` in the key, historical-view URLs would render HEAD because
 * the un-versioned key collides with DocumentPage's versioned fetch
 * and serves whichever landed first.
 *
 * view/onViewChange are optional: when omitted, DocumentView manages
 * its own local toggle state (useful for VaultSkillPage and future
 * embeds). When provided, the caller drives view state (DocumentPage
 * uses this to sync ?view= URL params).
 *
 * T6 NOTE: The segmented control block below is where the AGENT
 * segment will land. When T6 adds it, extend the `ViewMode` union and
 * add a third tab here alongside the doc.type === "skill" guard.
 */
export function DocumentView({ vault, docId, view: viewProp, onViewChange, extraTab, version }: DocumentViewProps) {
  const [localView, setLocalView] = useState<ViewMode>("rendered");

  // Controlled vs. uncontrolled view mode
  const view = viewProp ?? localView;
  const setView = (next: ViewMode) => {
    if (onViewChange) {
      onViewChange(next);
    } else {
      setLocalView(next);
    }
  };

  const { data: doc, isLoading, error } = useQuery({
    queryKey: ["document", vault, docId, version],
    queryFn: () => getDocument(vault, docId, version),
    enabled: !!vault && !!docId,
    retry: false,
  });

  const [copiedRaw, setCopiedRaw] = useState(false);

  async function copyRaw() {
    try {
      await navigator.clipboard.writeText(doc?.content || "");
      setCopiedRaw(true);
      setTimeout(() => setCopiedRaw(false), 1500);
    } catch {
      // clipboard API may be unavailable; silently no-op
    }
  }

  const markdownComponents = useMemo(
    () => buildHeadingComponents(doc?.content || ""),
    [doc?.content],
  );

  if (isLoading) {
    return (
      <div className="py-8 coord">
        <Loader2 className="h-4 w-4 inline animate-spin mr-2" aria-hidden />
        Loading…
      </div>
    );
  }

  if (error || !doc) {
    return null;
  }

  // If the parent passes "agent" but this isn't a skill doc, fall back to "rendered"
  const isSkill = doc.type === "skill";
  const effectiveView: ViewMode = view === "agent" && !isSkill ? "rendered" : view;

  return (
    <>
      {/* ── Skill banner (skill docs only) ──────────────────────── */}
      {isSkill && <SkillBanner vault={vault} docId={docId} />}

      {/* ── Rendered/Raw/Agent segmented control ──────────────────
         WAI-ARIA tabs pattern: ArrowLeft/ArrowRight (and Home/End)
         move focus between tabs; Enter/Space activates. Each tab
         points at its panel via aria-controls so screen readers
         announce the relationship. The extra tab (e.g. EDIT) is
         a navigation trigger, not a panel, so it owns no panel id. */}
      <TabStrip
        view={effectiveView}
        isSkill={isSkill}
        onSelect={setView}
        extraTab={extraTab}
      />

      {/* ── Doc body ──────────────────────────────────────────────── */}
      {effectiveView === "agent" ? (
        <div id="docview-panel-agent" role="tabpanel" aria-labelledby="docview-tab-agent">
          <AgentPreview vault={vault} />
        </div>
      ) : effectiveView === "rendered" ? (
        <div
          id="docview-panel-rendered"
          role="tabpanel"
          aria-labelledby="docview-tab-rendered"
          className="prose dark:prose-invert min-w-0"
          style={{ maxWidth: "100%" }}
        >
          <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {doc.content || ""}
          </Markdown>
        </div>
      ) : (
        <div
          id="docview-panel-raw"
          role="tabpanel"
          aria-labelledby="docview-tab-raw"
          className="relative"
        >
          <button
            type="button"
            onClick={copyRaw}
            aria-label="Copy markdown"
            className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-1 text-[11px] font-mono uppercase tracking-wider text-foreground-muted hover:text-accent border border-border bg-surface transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            {copiedRaw ? "COPIED" : "COPY"}
          </button>
          <pre
            data-testid="doc-raw"
            className="font-mono text-[13px] leading-[1.55] whitespace-pre-wrap overflow-x-auto bg-surface-muted p-4 border border-border"
          >
            {doc.content || ""}
          </pre>
        </div>
      )}
    </>
  );
}

// ── Segmented control with WAI-ARIA tabs keyboard handling ──────
interface TabStripProps {
  view: ViewMode;
  isSkill: boolean;
  onSelect: (next: ViewMode) => void;
  extraTab?: { label: string; onClick: () => void };
}

function TabStrip({ view, isSkill, onSelect, extraTab }: TabStripProps) {
  const tabs: Array<{ key: ViewMode | "extra"; label: string; selected: boolean; onActivate: () => void }> = [
    { key: "rendered", label: "RENDERED", selected: view === "rendered", onActivate: () => onSelect("rendered") },
    { key: "raw", label: "RAW", selected: view === "raw", onActivate: () => onSelect("raw") },
  ];
  if (isSkill) {
    tabs.push({ key: "agent", label: "AGENT", selected: view === "agent", onActivate: () => onSelect("agent") });
  }
  if (extraTab) {
    tabs.push({ key: "extra", label: extraTab.label, selected: false, onActivate: extraTab.onClick });
  }

  function onKey(e: React.KeyboardEvent<HTMLButtonElement>, idx: number) {
    let next: number;
    if (e.key === "ArrowRight") next = (idx + 1) % tabs.length;
    else if (e.key === "ArrowLeft") next = (idx - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabs.length - 1;
    else return;
    e.preventDefault();
    const target = e.currentTarget.parentElement?.children[next] as HTMLElement | undefined;
    target?.focus();
  }

  return (
    <div className="flex items-center justify-end mb-3">
      <div
        role="tablist"
        aria-label="Document view"
        className="inline-flex border border-border"
      >
        {tabs.map((t, i) => {
          const isPanelTab = t.key !== "extra";
          const cls = `px-2.5 py-1 text-[11px] font-mono uppercase tracking-wider transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background ${
            i > 0 ? "border-l border-border" : ""
          } ${
            t.selected
              ? "bg-foreground text-background"
              : "text-foreground-muted hover:text-foreground hover:bg-surface-muted"
          }`;
          return (
            <button
              key={t.key}
              role="tab"
              id={isPanelTab ? `docview-tab-${t.key}` : undefined}
              aria-selected={t.selected}
              aria-controls={isPanelTab ? `docview-panel-${t.key}` : undefined}
              tabIndex={t.selected || (!tabs.some((x) => x.selected) && i === 0) ? 0 : -1}
              onClick={t.onActivate}
              onKeyDown={(e) => onKey(e, i)}
              className={cls}
            >
              {t.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── Heading renderer helpers ─────────────────────────────────────

function buildHeadingComponents(markdown: string) {
  const slugQueue = parseHeadings(markdown).map((h) => h.slug);
  let cursor = 0;
  const make = (level: 1 | 2 | 3 | 4 | 5 | 6) => (props: any) => {
    const id = slugQueue[cursor++] ?? slugify(flattenText(props.children)) ?? `heading-${level}`;
    const Tag = `h${level}` as any;
    return <Tag id={id} {...props} />;
  };
  // Strip `javascript:` / `data:` / other non-navigation schemes from
  // markdown link targets — react-markdown otherwise hands the raw href
  // straight to <a>, so a malicious doc could ship a clickable XSS.
  const SafeLink = ({ href, ...props }: any) => (
    <a {...props} href={sanitizeLinkUrl(href)} rel="noopener noreferrer" />
  );
  return {
    h1: make(1), h2: make(2), h3: make(3), h4: make(4), h5: make(5), h6: make(6),
    a: SafeLink,
  };
}

function flattenText(children: any): string {
  if (typeof children === "string") return children;
  if (Array.isArray(children)) return children.map(flattenText).join("");
  if (children?.props?.children) return flattenText(children.props.children);
  return "";
}

// ── Agent preview (skill docs only) ─────────────────────────────

function AgentPreview({ vault }: { vault: string }) {
  const helpQuery = useQuery({
    queryKey: ["vault-skill-preview", vault],
    queryFn: () => getVaultSkillPreview(vault),
    retry: false,
  });
  if (helpQuery.isLoading) return <div className="p-4"><Skeleton className="h-64 w-full" /></div>;
  if (helpQuery.isError) return <p className="coord text-destructive p-4">Failed to load agent preview.</p>;
  return (
    <pre className="font-mono text-[11px] leading-snug whitespace-pre-wrap bg-background border border-border p-4">
      {helpQuery.data}
    </pre>
  );
}
