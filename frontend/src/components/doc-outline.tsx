import { useDocOutline } from "@/hooks/use-doc-outline";

export function DocumentOutline({
  markdown,
  articleEl,
}: {
  markdown: string;
  articleEl: HTMLElement | null;
}) {
  const { headings, activeSlug } = useDocOutline(markdown, { root: articleEl });
  if (headings.length === 0) return null;

  // Normalize so the shallowest level sits flush-left, even if the doc starts
  // at H2 or uses only H3/H4.
  const minLevel = Math.min(...headings.map((h) => h.level));

  return (
    <nav aria-label="Document outline" className="text-sm">
      <ol>
        {headings.map((h) => {
          const indent = h.level - minLevel;
          const isActive = activeSlug === h.slug;
          return (
            <li key={h.slug} style={{ marginLeft: `${indent * 12}px` }}>
              <a
                href={`#${h.slug}`}
                aria-current={isActive ? "true" : undefined}
                className={`block py-1 px-2 rounded-[var(--radius-sm)] leading-snug transition-colors ${
                  isActive
                    ? "bg-accent/10 text-accent font-medium"
                    : "text-foreground-muted hover:text-foreground hover:bg-surface-muted"
                }`}
              >
                <span title={h.text} className="truncate block text-[12px]">{h.text}</span>
              </a>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
