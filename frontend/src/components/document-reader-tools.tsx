import { useEffect, useMemo, useRef, useState, type RefObject, type ReactNode } from "react";
import { ArrowDown, ArrowUp, Check, ChevronDown, ChevronRight, Copy, Minus, Plus, RotateCcw, Search, SlidersHorizontal, X, ZoomIn, ZoomOut, Maximize } from "lucide-react";
import { findReadingTextMatches, type MarkdownViewerCodeBlock } from "@akb/markdown-editor/react";
import { useReaderTools } from "@/contexts/reader-tools-context";
import { DEFAULT_READER_PREFERENCES } from "@/lib/reader-preferences";
import { DocumentIconButton } from "@/components/document-reading-controls";
import { Popover, PopoverAnchor, PopoverTrigger, PopoverContent } from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

export function DocumentReaderToolsActions({ anchorRef, settingsInMenu = false }: { anchorRef?: RefObject<HTMLButtonElement | null>; settingsInMenu?: boolean }) {
  const tools = useReaderTools();
  const findTrigger = useRef<HTMLButtonElement>(null);
  const settingsContent = useRef<HTMLDivElement>(null);
  const settingsAnchor = useMemo(() => ({ current: { getBoundingClientRect: () => anchorRef?.current?.getBoundingClientRect() ?? new DOMRect() } }), [anchorRef]);
  const openFind = tools?.openFind;
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (!openFind || event.defaultPrevented || event.isComposing || event.altKey || !(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "f"
        || [...document.querySelectorAll('[role="dialog"][data-state="open"], [role="alertdialog"]')].some(dialog => !dialog.contains(anchorRef?.current ?? findTrigger.current))
        || event.target instanceof HTMLElement && event.target.isContentEditable) return;
      event.preventDefault(); openFind(findTrigger.current ?? anchorRef?.current ?? undefined);
    };
    window.addEventListener("keydown", keydown); return () => window.removeEventListener("keydown", keydown);
  }, [openFind, anchorRef]);
  if (!tools) return null;
  const { preferences: value, setPreferences } = tools;
  return <>
    <DocumentIconButton ref={findTrigger} label="Find in document" className="document-find-trigger" aria-expanded={tools.findOpen} onClick={event => tools.openFind(event.currentTarget)}><Search size={16} aria-hidden /></DocumentIconButton>
    <Popover open={tools.settingsOpen} onOpenChange={tools.setSettingsOpen}>
      {settingsInMenu ? <PopoverAnchor virtualRef={settingsAnchor} /> : <PopoverTrigger asChild><DocumentIconButton label="Reading settings"><SlidersHorizontal size={16} aria-hidden /></DocumentIconButton></PopoverTrigger>}
      <PopoverContent ref={settingsContent} align="end" className="reader-settings" aria-label="Reading settings"
        portalContainer={settingsInMenu ? anchorRef?.current?.closest<HTMLElement>('[role="dialog"]') ?? undefined : undefined}
        onCloseAutoFocus={event => {
          if (!settingsInMenu) return;
          event.preventDefault();
          const focused = document.activeElement;
          if (focused instanceof HTMLElement && focused !== document.body && focused.isConnected && !settingsContent.current?.contains(focused)) return;
          anchorRef?.current?.focus({ preventScroll: true });
        }}>
        <h2 className="text-sm font-semibold">Reading settings</h2>
        <div className="reader-setting-row"><span id="reader-font-label">Text size</span><div role="group" aria-labelledby="reader-font-label">
          <Button size="icon" variant="ghost" aria-label="Decrease text size" disabled={value.fontSize <= 14} onClick={() => setPreferences({ ...value, fontSize: [14, 15, 16, 18, 20, 22].filter(size => size < value.fontSize).at(-1)! })}><Minus size={15} aria-hidden /></Button>
          <output className="tabular-nums text-xs">{value.fontSize}px</output>
          <Button size="icon" variant="ghost" aria-label="Increase text size" disabled={value.fontSize >= 22} onClick={() => setPreferences({ ...value, fontSize: [14, 15, 16, 18, 20, 22].find(size => size > value.fontSize)! })}><Plus size={15} aria-hidden /></Button>
        </div></div>
        <fieldset><legend className="mb-2 text-xs text-foreground-muted">Reading width</legend><div className="reader-width-options">{(["compact", "comfortable", "wide"] as const).map(width => <label key={width} data-selected={value.width === width}>
          <input className="reader-width-input" type="radio" name="reader-width" checked={value.width === width} onChange={() => setPreferences({ ...value, width })} /><span>{width === "comfortable" ? "Standard" : width === "compact" ? "Compact" : "Wide"}</span>
        </label>)}</div></fieldset>
        <label className="reader-setting-check"><input type="checkbox" checked={value.tableFit} onChange={event => setPreferences({ ...value, tableFit: event.target.checked })} />Fit tables to reading width</label>
        <label className="reader-setting-check"><input type="checkbox" checked={value.foldCode} onChange={event => setPreferences({ ...value, foldCode: event.target.checked })} />Start with code collapsed</label>
        <Button size="sm" variant="ghost" onClick={() => setPreferences(DEFAULT_READER_PREFERENCES)}><RotateCcw size={13} aria-hidden />Reset reading settings</Button>
      </PopoverContent>
    </Popover>
  </>;
}

export function DocumentFindBar() {
  const tools = useReaderTools();
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { if (tools?.findOpen) { input.current?.focus(); input.current?.select(); } }, [tools?.findOpen, tools?.findRequest]);
  if (!tools?.findOpen) return null;
  const close = tools.closeFind;
  return <div className="reader-find-bar" role="search" aria-label="Find in this document" onKeyDown={event => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (event.key === "Enter") { event.preventDefault(); tools.move(event.shiftKey ? -1 : 1); }
  }}>
    <Search size={15} aria-hidden /><Input ref={input} type="search" className="focus-visible:ring-offset-0" aria-label="Find text in document" placeholder="Find in this document…" maxLength={256} value={tools.query} onChange={event => tools.setQuery(event.target.value)} />
    <output aria-live="polite">{tools.query ? `${tools.result.count ? tools.current + 1 : 0}/${tools.result.count}${tools.result.limited ? "+" : ""}` : ""}</output>
    <Button variant="ghost" size="icon" aria-label="Previous match" disabled={!tools.result.count} onClick={() => tools.move(-1)}><ArrowUp size={14} aria-hidden /></Button>
    <Button variant="ghost" size="icon" aria-label="Next match" disabled={!tools.result.count} onClick={() => tools.move(1)}><ArrowDown size={14} aria-hidden /></Button>
    <Button variant="ghost" size="icon" aria-label="Close document find" onClick={close}><X size={15} aria-hidden /></Button>
  </div>;
}

export function DocumentCodeToolbar({ block, onToggle, children }: { block: MarkdownViewerCodeBlock; onToggle: () => void; children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  return <div className="reader-code-toolbar" role="group" aria-label={`${block.language} code tools`}>
    <span className="reader-code-language">{block.language}</span><span className="reader-code-lines">{block.code.split("\n").length} lines</span>
    {children}
    <Button size="sm" variant="ghost" aria-label={`Copy code block ${block.index + 1}`} onClick={async () => {
      try { await navigator.clipboard.writeText(block.code); setCopied(true); setFailed(false); clearTimeout(timer.current); timer.current = setTimeout(() => setCopied(false), 1500); }
      catch { setFailed(true); }
    }}>{copied ? <Check size={13} aria-hidden /> : <Copy size={13} aria-hidden />}<span aria-live="polite">{failed ? "Copy failed" : copied ? "Copied" : "Copy"}</span></Button>
    <Button size="icon" variant="ghost" aria-label={`${block.collapsed ? "Expand" : "Collapse"} code block ${block.index + 1}`} aria-expanded={!block.collapsed} onClick={onToggle}>{block.collapsed ? <ChevronRight size={14} aria-hidden /> : <ChevronDown size={14} aria-hidden />}</Button>
  </div>;
}

export function RawDocumentText({ text, searchable }: { text: string; searchable: boolean }) {
  const tools = useReaderTools();
  const query = searchable && tools?.findOpen ? tools.query : "";
  const result = useMemo(() => findReadingTextMatches(text, query), [text, query]);
  const report = tools?.report;
  const current = tools?.current ?? 0;
  const active = useRef<HTMLElement>(null);
  useEffect(() => { if (searchable) report?.({ count: result.matches.length, limited: result.limited }); }, [report, searchable, result]);
  useEffect(() => { active.current?.scrollIntoView?.({ block: "center" }); }, [current, query]);
  let last = 0;
  return <>{result.matches.map((match, index) => {
    const before = text.slice(last, match.from); last = match.to;
    return <span key={match.from}>{before}<mark ref={index === current ? active : undefined} className="markdown-reading-match" data-reading-match={index === current ? "current" : "other"}>{text.slice(match.from, match.to)}</mark></span>;
  })}{text.slice(last)}</>;
}

export function DocumentImageOpenButton({ index, alt, rootRef, onOpen }: { index: number; alt: string; rootRef: RefObject<HTMLDivElement | null>; onOpen: (image: { src: string; alt: string }, opener: HTMLElement) => void }) {
  const [placement, setPlacement] = useState<{ left: number; top: number } | null>(null);
  const getImage = () => rootRef.current?.querySelectorAll<HTMLImageElement>("img[data-markdown-image], img[data-markdown-target]")[index];
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const measure = () => {
      const picture = root.querySelectorAll<HTMLImageElement>("img[data-markdown-image], img[data-markdown-target]")[index];
      const bounds = picture?.getBoundingClientRect();
      if (!picture?.complete || !picture.naturalWidth || picture.hidden || picture.closest("a") || !bounds?.width || !bounds.height) { setPlacement(previous => previous === null ? previous : null); return; }
      const origin = root.getBoundingClientRect();
      const scale = root.offsetWidth ? origin.width / root.offsetWidth : 1;
      const control = (window.matchMedia("(pointer: coarse), (max-width: 40rem)").matches ? 2.75 : 2) * (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16);
      const next = { left: Math.max((bounds.left - origin.left) / scale, (bounds.right - origin.left) / scale - control - 4), top: (bounds.top - origin.top) / scale + 4 };
      setPlacement(previous => previous?.left === next.left && previous.top === next.top ? previous : next);
    };
    measure(); root.addEventListener("load", measure, true); root.addEventListener("error", measure, true);
    const observer = new MutationObserver(measure); observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["src", "hidden", "style", "class"] });
    const resize = new ResizeObserver(measure); resize.observe(root); window.addEventListener("resize", measure);
    return () => { root.removeEventListener("load", measure, true); root.removeEventListener("error", measure, true); observer.disconnect(); resize.disconnect(); window.removeEventListener("resize", measure); };
  }, [index, rootRef]);
  if (!placement) return null;
  return <button type="button" className="reader-image-open" style={placement} data-reader-image-index={index} aria-label={`Enlarge image${alt ? `: ${alt}` : ""}`} onClick={event => {
    const picture = getImage(); if (picture?.complete && picture.naturalWidth && !picture.closest("a")) onOpen({ src: picture.currentSrc || picture.src, alt: picture.alt }, event.currentTarget);
  }}><ZoomIn size={16} aria-hidden /></button>;
}

export function ReaderImageZoom({ image, onClose, returnFocusRef }: { image: { src: string; alt: string } | null; onClose: () => void; returnFocusRef?: RefObject<HTMLElement | null> }) {
  const [scale, setScale] = useState(1);
  const [stage, setStage] = useState<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ natural: 0, fit: 0 });
  const originalScale = size.fit ? size.natural / size.fit : 1;
  const effectiveScale = scale || originalScale;
  const maxScale = Math.max(8, originalScale);
  const zoomBy = (factor: number) => setScale(value => Math.min(maxScale, Math.max(.25, (value || originalScale) * factor)));
  useEffect(() => { setScale(1); }, [image]);
  useEffect(() => {
    const element = stage;
    if (!element) return;
    const measure = () => {
      const picture = element.querySelector("img");
      if (picture?.naturalWidth) setSize({ natural: picture.naturalWidth, fit: picture.naturalWidth * Math.min(1, element.clientWidth / picture.naturalWidth, element.clientHeight / picture.naturalHeight) });
    };
    element.addEventListener("load", measure, true);
    const observer = new ResizeObserver(measure); observer.observe(element); measure();
    return () => { element.removeEventListener("load", measure, true); observer.disconnect(); };
  }, [image, stage]);
  return <Dialog open={Boolean(image)} onOpenChange={open => { if (!open) onClose(); }}><DialogContent className="reader-image-dialog" aria-describedby={undefined} onCloseAutoFocus={event => {
    if (returnFocusRef?.current?.isConnected) { event.preventDefault(); returnFocusRef.current.focus({ preventScroll: true }); }
  }}>
    <DialogTitle className="sr-only">{image?.alt || "Image preview"}</DialogTitle>
    <div className="reader-image-controls" role="group" aria-label="Image zoom controls">
      <Button size="icon" variant="ghost" aria-label="Zoom out" disabled={effectiveScale <= .25} onClick={() => zoomBy(1 / 1.25)}><ZoomOut size={17} aria-hidden /></Button>
      <output className="text-xs tabular-nums">{scale === 0 ? "1:1" : scale === 1 ? "Fit" : `${Math.round(scale * 100)}%`}</output>
      <Button size="icon" variant="ghost" aria-label="Zoom in" disabled={effectiveScale >= maxScale} onClick={() => zoomBy(1.25)}><ZoomIn size={17} aria-hidden /></Button>
      <Button size="sm" variant="ghost" onClick={() => setScale(1)}><Maximize size={14} aria-hidden />Fit</Button>
      <Button size="sm" variant="ghost" onClick={() => setScale(0)}>1:1</Button>
    </div>
    <div ref={setStage} className="reader-image-stage rail-scroll" tabIndex={0} role="region" aria-label="Image preview; scroll to pan" onKeyDown={event => {
      if (event.key === "+" || event.key === "=") { event.preventDefault(); zoomBy(1.25); }
      if (event.key === "-") { event.preventDefault(); zoomBy(1 / 1.25); }
      if (event.key === "0") { event.preventDefault(); setScale(1); }
    }}>
      {image && <img src={image.src} alt={image.alt} referrerPolicy="no-referrer" style={{ width: size.fit ? scale === 0 ? size.natural : size.fit * scale : "100%", maxWidth: "none" }} />}
    </div>
  </DialogContent></Dialog>;
}
