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
            <li key={h.slug} style={{ paddingLeft: `${indent * 10}px` }}>
              <a
                href={`#${h.slug}`}
                aria-current={isActive ? "true" : undefined}
                className={`block py-[3px] leading-snug border-l-2 pl-2 -ml-[2px] transition-colors ${
                  isActive
                    ? "border-accent text-accent font-medium"
                    : "border-transparent text-foreground-muted hover:text-foreground hover:border-border-strong"
                }`}
              >
                <span className="truncate block text-[12px]">{h.text}</span>
              </a>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
