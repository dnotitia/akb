import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Pause, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

/** Read-only metadata beside the authored title, never inserted into Markdown. */
export function DocumentSummary({ summary }: { summary?: string | null }) {
  if (!summary?.trim()) return null;
  return <SummaryLine key={summary} summary={summary} />;
}

function SummaryLine({ summary }: { summary: string }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const textRef = useRef<HTMLParagraphElement>(null);
  const [distance, setDistance] = useState(0);
  const [paused, setPaused] = useState(false);
  const [visible, setVisible] = useState(true);
  const [foreground, setForeground] = useState(true);

  useEffect(() => {
    const viewport = viewportRef.current;
    const text = textRef.current;
    if (!viewport || !text) return;
    let disposed = false;
    const measure = () => {
      if (disposed || viewport.clientWidth <= 0) return;
      const overflow = Math.max(0, text.scrollWidth - viewport.clientWidth);
      const clearance = parseFloat(getComputedStyle(viewport).getPropertyValue("--summary-edge-clearance")) || 0;
      // At the end, every character must clear the faded edge.
      setDistance(overflow > 1 ? overflow + clearance : 0);
    };
    measure();
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    resize?.observe(viewport);
    resize?.observe(text);
    document.fonts?.ready.then(measure);
    const intersection = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(entries => {
      if (!disposed) setVisible(entries[0]?.isIntersecting ?? true);
    });
    intersection?.observe(viewport);
    const onVisibility = () => setForeground(document.visibilityState !== "hidden");
    onVisibility();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disposed = true;
      resize?.disconnect();
      intersection?.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [summary]);

  return <div className="document-summary-line" data-overflow={distance > 1} data-paused={paused || !visible || !foreground}
    style={{ "--summary-distance": `${-distance}px`, "--summary-duration": `${Math.max(18, distance / 24 + 8)}s` } as CSSProperties}>
    <div ref={viewportRef} className="document-summary-viewport" role="region" aria-label="Document summary" tabIndex={distance > 1 ? 0 : undefined}>
      <p ref={textRef} className="document-summary-text">{summary}</p>
    </div>
    {distance > 1 && <TooltipProvider delayDuration={250}><Tooltip>
      <TooltipTrigger asChild><Button type="button" variant="ghost" size="icon" className="document-summary-motion"
        aria-label={paused ? "Resume summary scrolling" : "Pause summary scrolling"} onClick={() => setPaused(value => !value)}>
        {paused ? <Play className="h-3 w-3" aria-hidden /> : <Pause className="h-3 w-3" aria-hidden />}
      </Button></TooltipTrigger>
      <TooltipContent side="bottom">{paused ? "Resume summary" : "Pause summary"}</TooltipContent>
    </Tooltip></TooltipProvider>}
  </div>;
}
