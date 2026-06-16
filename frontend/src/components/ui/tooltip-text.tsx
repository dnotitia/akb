import { useEffect, useRef, useState } from "react";
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
  TooltipProvider,
} from "@/components/ui/tooltip";

/**
 * Design-system primitive: truncated text that reveals its full value on
 * hover/focus — but ONLY when it actually overflows. Replaces the scattered
 * `truncate` + native `title=` pattern (delayed, touch/keyboard-hostile,
 * unstyled) with the project's styled <Tooltip>.
 *
 * Pass the same truncating className you'd normally use (e.g. "truncate", or
 * "truncate text-xs …"). Overflow is measured with a ResizeObserver so the
 * tooltip is suppressed when the text fits — no useless tooltips. When the
 * rendered children differ from the text to surface (a "→ " prefix, an icon),
 * pass the bare string via `tip`.
 */
type TooltipTextProps = Omit<React.HTMLAttributes<HTMLElement>, "title"> & {
  /** Full text to reveal. Defaults to the string children. */
  tip?: string;
  /** Element to render. Defaults to a span. */
  as?: "span" | "div" | "p";
  /**
   * Decorate an arbitrary single child element (e.g. a `<td>` or a router
   * `<Link>`) instead of rendering our own tag — for truncating elements that
   * can't be a span/div/p. The child keeps its props/layout; we only attach the
   * overflow measurement + tooltip. Requires `tip` (children aren't a string).
   */
  asChild?: boolean;
  side?: "top" | "right" | "bottom" | "left";
  children: React.ReactNode;
};

export function TooltipText({
  tip,
  as: Tag = "span",
  asChild = false,
  side = "top",
  className,
  children,
  ...rest
}: TooltipTextProps) {
  const ref = useRef<HTMLElement>(null);
  const [overflow, setOverflow] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setOverflow(el.scrollWidth - el.clientWidth > 1);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [children, tip]);

  const full = tip ?? (typeof children === "string" ? children : undefined);
  const content =
    overflow && full ? (
      <TooltipContent side={side} className="max-w-[min(90vw,28rem)] break-words">
        {full}
      </TooltipContent>
    ) : null;

  // asChild: clone the caller's element as the trigger (Radix forwards our ref
  // to it for measurement). Radix Root/Trigger render no DOM wrapper, so the
  // child stays where it was in the tree (e.g. a direct <td> of a <tr>).
  if (asChild) {
    return (
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          <TooltipTrigger asChild ref={ref as React.Ref<HTMLButtonElement>}>
            {children}
          </TooltipTrigger>
          {content}
        </Tooltip>
      </TooltipProvider>
    );
  }

  // Stable tree: always a Trigger so the measured node never remounts; the
  // Content only mounts when the text overflows and we have a string to show.
  // Self-providing so the primitive works anywhere (tests, standalone) without
  // a global TooltipProvider ancestor.
  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Tag ref={ref as React.Ref<never>} className={className} {...rest}>
            {children}
          </Tag>
        </TooltipTrigger>
        {content}
      </Tooltip>
    </TooltipProvider>
  );
}
