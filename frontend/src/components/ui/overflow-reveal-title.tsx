import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

type RevealPhase = "idle" | "waiting" | "running" | "settled";

/** Single-line title inside an existing link/button: only its text may move. */
export function OverflowRevealTitle({ title, className, side = "right" }: {
  title: string;
  className?: string;
  side?: "top" | "right" | "bottom" | "left";
}) {
  const viewport = useRef<HTMLSpanElement>(null);
  const text = useRef<HTMLSpanElement>(null);
  const [distance, setDistance] = useState(0);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [phase, setPhase] = useState<RevealPhase>("idle");
  const [fallbackOpen, setFallbackOpen] = useState(false);
  const [staticOnly, setStaticOnly] = useState(false);
  const [inView, setInView] = useState(() => typeof IntersectionObserver === "undefined");
  const [pageVisible, setPageVisible] = useState(() => document.visibilityState !== "hidden");
  const [blocked, setBlocked] = useState(false);
  const interacting = hovered || focused || fallbackOpen;
  const available = distance > 1 && inView && pageVisible && !blocked && !dismissed;
  const animate = available && !staticOnly && (hovered || focused);
  const staticOpen = available && staticOnly && fallbackOpen;
  const latest = useRef({ revealing: false });
  latest.current = { revealing: available && (phase !== "idle" || staticOpen) };

  useLayoutEffect(() => {
    const element = viewport.current;
    const label = text.current;
    if (!element || !label) return;
    let live = true;
    const measure = () => {
      if (live) setDistance(Math.max(0, Math.ceil(label.scrollWidth - element.clientWidth)));
    };
    measure();
    const resize = new ResizeObserver(measure);
    resize.observe(element);
    resize.observe(label);
    const intersection = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(entries => {
      const entry = entries.find(item => item.target === element);
      if (entry) setInView(entry.isIntersecting && entry.intersectionRatio > 0);
    });
    intersection?.observe(element);
    const fonts = document.fonts;
    void fonts?.ready.then(measure);
    fonts?.addEventListener("loadingdone", measure);
    return () => {
      live = false;
      resize.disconnect();
      intersection?.disconnect();
      fonts?.removeEventListener("loadingdone", measure);
    };
  }, [title]);

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    const coarse = window.matchMedia("(hover: none), (pointer: coarse)");
    const update = () => setStaticOnly(reduced.matches || coarse.matches);
    const visibility = () => setPageVisible(document.visibilityState !== "hidden");
    update();
    reduced.addEventListener("change", update);
    coarse.addEventListener("change", update);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      reduced.removeEventListener("change", update);
      coarse.removeEventListener("change", update);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, []);

  useEffect(() => {
    const trigger = viewport.current?.closest<HTMLElement>("a[href], button, [tabindex]");
    if (!trigger) return;
    const enter = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      setDismissed(false);
      setHovered(true);
    };
    const leave = () => setHovered(false);
    const focus = () => { setDismissed(false); setFocused(true); };
    const blur = (event: FocusEvent) => {
      if (event.relatedTarget instanceof Node && trigger.contains(event.relatedTarget)) return;
      setFocused(false);
      setFallbackOpen(false);
    };
    trigger.addEventListener("pointerenter", enter);
    trigger.addEventListener("pointerleave", leave);
    trigger.addEventListener("focusin", focus);
    trigger.addEventListener("focusout", blur);
    if (trigger.contains(document.activeElement)) focus();
    return () => {
      trigger.removeEventListener("pointerenter", enter);
      trigger.removeEventListener("pointerleave", leave);
      trigger.removeEventListener("focusin", focus);
      trigger.removeEventListener("focusout", blur);
    };
  }, [title]);

  useEffect(() => {
    if (!interacting) return;
    const escape = (event: KeyboardEvent) => {
      // A pointer inspection need not own keyboard focus. Dismiss it before
      // the outer drawer, while leaving a modal/menu's handled Escape alone.
      if (event.key !== "Escape" || event.defaultPrevented || !latest.current.revealing) return;
      event.preventDefault();
      setDismissed(true);
      setFallbackOpen(false);
      setPhase("idle");
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [interacting]);

  // Observe modal/hidden changes only while a row is being inspected, rather
  // than installing a document-wide mutation observer for every virtual row.
  useEffect(() => {
    if (!interacting) return;
    const check = () => {
      const element = viewport.current;
      let hidden = !element || Boolean(element.closest("[hidden], [inert], [aria-hidden=true]"));
      for (let ancestor: HTMLElement | null = element; ancestor && !hidden; ancestor = ancestor.parentElement) {
        const style = getComputedStyle(ancestor);
        hidden = style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse";
      }
      const modal = [...document.querySelectorAll<HTMLElement>('[aria-modal="true"], dialog[open]')]
        .some(dialog => !dialog.closest("[hidden]") && getComputedStyle(dialog).display !== "none");
      setBlocked(hidden || modal);
    };
    check();
    const observer = new MutationObserver(check);
    observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["hidden", "inert", "aria-hidden", "aria-modal", "open", "class", "style"] });
    return () => observer.disconnect();
  }, [interacting]);

  useEffect(() => { setPhase(animate ? "waiting" : "idle"); }, [animate, distance, title]);
  useEffect(() => {
    if (!available || !staticOnly) setFallbackOpen(false);
    else if (focused) setFallbackOpen(true);
  }, [available, staticOnly, focused]);

  return <TooltipProvider delayDuration={600}>
    <Tooltip open={staticOpen} onOpenChange={open => {
      if (!open || (available && staticOnly)) setFallbackOpen(open);
    }}>
      <TooltipTrigger asChild>
        <span ref={viewport} data-slot="overflow-reveal-title" data-overflow={distance > 1} data-reveal={staticOpen ? "static" : phase}
          className={cn("overflow-reveal-title", className)}
          style={{ "--title-reveal-distance": `${-distance}px`, "--title-reveal-travel": distance } as CSSProperties}>
          <span ref={text} data-slot="overflow-reveal-text" className="overflow-reveal-title-text"
            onAnimationStart={event => { if (event.target === text.current && event.animationName === "akb-title-reveal" && animate) setPhase("running"); }}
            onAnimationEnd={event => { if (event.target === text.current && event.animationName === "akb-title-reveal" && animate) setPhase("settled"); }}>{title}</span>
        </span>
      </TooltipTrigger>
      <TooltipPrimitive.Portal>
        <TooltipContent data-slot="overflow-reveal-tooltip" side={side} aria-hidden="true" className="max-w-[min(90vw,28rem)] break-words [overflow-wrap:anywhere]"
          onEscapeKeyDown={() => { setDismissed(true); setFallbackOpen(false); }}>{title}</TooltipContent>
      </TooltipPrimitive.Portal>
    </Tooltip>
  </TooltipProvider>;
}
