import { useEffect, useRef, useState } from "react";
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
  TooltipPortal,
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
  /**
   * Receives the measured overflow state whenever it changes, for a caller
   * that needs it beyond the tooltip (e.g. a truncated table cell that becomes
   * keyboard-reachable). Reuses this measurement instead of adding a second
   * ResizeObserver; omitting it changes nothing. The first call follows the
   * first measurement: a newly mounted instance never reports a guess.
   */
  onOverflowChange?: (overflow: boolean) => void;
  /**
   * Which overflow means the text is cut. "x" (the default) measures only the
   * width, as single-line truncation needs. "both" also measures the height,
   * for text wrapped onto lines and clamped to a number of them.
   */
  overflowAxis?: "x" | "both";
  /**
   * Render the tooltip on document.body instead of beside the trigger. Use it
   * when the text sits inside another control (e.g. a table cell button):
   * inline tooltip content would otherwise land inside that control and join
   * its accessible name. Defaults to false (inline, as before).
   */
  portal?: boolean;
  children: React.ReactNode;
};

export function TooltipText({
  tip,
  as: Tag = "span",
  asChild = false,
  side = "top",
  onOverflowChange,
  overflowAxis = "x",
  portal = false,
  className,
  children,
  ...rest
}: TooltipTextProps) {
  const ref = useRef<HTMLElement>(null);
  // null until the first measurement, so a new instance never reports an
  // overflow it has not measured. A caller that swaps the element around the
  // text on that report (a table cell's button and plain container) remounts
  // this component; reporting the initial false would flip it straight back.
  const [overflow, setOverflow] = useState<boolean | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () =>
      setOverflow(
        el.scrollWidth - el.clientWidth > 1 ||
          (overflowAxis === "both" && el.scrollHeight - el.clientHeight > 1),
      );
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [children, tip, overflowAxis]);

  useEffect(() => {
    if (overflow !== null) onOverflowChange?.(overflow);
  }, [onOverflowChange, overflow]);

  const full = tip ?? (typeof children === "string" ? children : undefined);
  const tooltip =
    overflow && full ? (
      <TooltipContent side={side} className="max-w-[min(90vw,28rem)] break-words">
        {full}
      </TooltipContent>
    ) : null;
  const content = tooltip && portal ? <TooltipPortal>{tooltip}</TooltipPortal> : tooltip;

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
