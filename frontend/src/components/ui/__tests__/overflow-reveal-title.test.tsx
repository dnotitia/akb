import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OverflowRevealTitle } from "@/components/ui/overflow-reveal-title";

const longTitle = "A complete operating guide whose full document name should remain available";
let textWidth = 360;
let viewportWidth = 120;
let reduced = false;
let coarse = false;
let visibility: DocumentVisibilityState = "visible";
const mediaListeners = new Set<() => void>();
const resizes: Array<{ targets: Element[]; run: () => void; disconnect: () => void }> = [];
const intersections: Array<{ target?: Element; run: (visible: boolean) => void; disconnect: () => void }> = [];
const originalFonts = Object.getOwnPropertyDescriptor(document, "fonts");

function mount(title = longTitle) {
  const activated = vi.fn();
  const view = render(<><a href="/document/guide" onClick={event => { activated(event.defaultPrevented); event.preventDefault(); }}>
    <span className="sr-only">Document: </span><OverflowRevealTitle title={title} /><span>Updated today</span>
  </a><button type="button">Next control</button></>);
  const viewport = view.container.querySelector<HTMLElement>('[data-slot="overflow-reveal-title"]')!;
  const text = view.container.querySelector<HTMLElement>('[data-slot="overflow-reveal-text"]')!;
  return { ...view, viewport, text, link: screen.getByRole("link"), activated };
}
function animation(text: HTMLElement, event: "animationStart" | "animationEnd") {
  // React selects the WebKit event in JSDOM when AnimationEvent is absent.
  for (const type of event === "animationStart" ? ["animationstart", "webkitAnimationStart"] : ["animationend", "webkitAnimationEnd"]) {
    const transition = new Event(type, { bubbles: true });
    Object.defineProperty(transition, "animationName", { value: "akb-title-reveal" });
    fireEvent(text, transition);
  }
}
function remeasure() {
  act(() => resizes.filter(observer => observer.targets.some(target => target.getAttribute("data-slot") === "overflow-reveal-title")).forEach(observer => observer.run()));
}

beforeEach(() => {
  textWidth = 360; viewportWidth = 120; reduced = false; coarse = false; visibility = "visible";
  resizes.length = 0; intersections.length = 0; mediaListeners.clear();
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) { return this.getAttribute("data-slot") === "overflow-reveal-title" ? viewportWidth : 0; });
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) { return this.getAttribute("data-slot") === "overflow-reveal-text" ? textWidth : 0; });
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
  vi.spyOn(window, "matchMedia").mockImplementation(query => ({
    get matches() { return query.includes("prefers-reduced-motion") ? reduced : coarse; }, media: query,
    onchange: null, addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: () => false,
    addEventListener: (_type: string, listener: () => void) => mediaListeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => mediaListeners.delete(listener),
  }) as unknown as MediaQueryList);
  vi.stubGlobal("ResizeObserver", class {
    record: typeof resizes[number];
    constructor(callback: ResizeObserverCallback) { this.record = { targets: [], run: () => callback([], this as unknown as ResizeObserver), disconnect: vi.fn() }; resizes.push(this.record); }
    observe(target: Element) { this.record.targets.push(target); }
    unobserve() {}
    disconnect() { this.record.disconnect(); }
  });
  vi.stubGlobal("IntersectionObserver", class {
    record: typeof intersections[number];
    constructor(callback: IntersectionObserverCallback) {
      this.record = { disconnect: vi.fn(), run: visible => callback([{ target: this.record.target!, isIntersecting: visible, intersectionRatio: visible ? 1 : 0 } as IntersectionObserverEntry], this as unknown as IntersectionObserver) };
      intersections.push(this.record);
    }
    observe(target: Element) { this.record.target = target; this.record.run(true); }
    unobserve() {}
    disconnect() { this.record.disconnect(); }
  });
});
afterEach(() => {
  cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  if (originalFonts) Object.defineProperty(document, "fonts", originalFonts);
  else Reflect.deleteProperty(document, "fonts");
});

describe("OverflowRevealTitle", () => {
  it("leaves fitting names still on hover and focus", async () => {
    textWidth = 90;
    const { viewport, link } = mount("Short guide");
    fireEvent.pointerEnter(link, { pointerType: "mouse" });
    link.focus();
    await waitFor(() => expect(viewport).toHaveAttribute("data-overflow", "false"));
    expect(viewport).toHaveAttribute("data-reveal", "idle");
    expect(document.querySelector('[data-slot="overflow-reveal-tooltip"]')).not.toBeInTheDocument();
  });

  it("starts only on its existing link, makes one traversal, holds and resets on leave", () => {
    const { viewport, text, link } = mount();
    expect(viewport).toHaveAttribute("data-reveal", "idle");
    fireEvent.pointerEnter(link, { pointerType: "mouse" });
    expect(viewport).toHaveAttribute("data-reveal", "waiting");
    animation(text, "animationStart");
    expect(viewport).toHaveAttribute("data-reveal", "running");
    animation(text, "animationEnd");
    expect(viewport).toHaveAttribute("data-reveal", "settled");
    expect(link).toHaveAccessibleName(new RegExp(`^Document:\\s*${longTitle}\\s*Updated today$`));
    fireEvent.pointerLeave(link, { pointerType: "mouse" });
    expect(viewport).toHaveAttribute("data-reveal", "idle");
    expect(viewport.style.getPropertyValue("--title-reveal-distance")).toBe("-240px");
  });

  it("uses existing keyboard focus without adding a Tab stop, and Escape cancels reveal", async () => {
    const user = userEvent.setup(); const { viewport, text, link } = mount();
    await user.tab();
    expect(link).toHaveFocus();
    expect(viewport).toHaveAttribute("data-reveal", "waiting");
    animation(text, "animationStart");
    await user.keyboard("{Escape}");
    expect(viewport).toHaveAttribute("data-reveal", "idle");
    expect(link).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Next control" })).toHaveFocus();
    expect(viewport).toHaveAttribute("data-reveal", "idle");
  });

  it("dismisses hover reveal with focus elsewhere and respects an already handled Escape", () => {
    const { viewport, text, link } = mount();
    const next = screen.getByRole("button", { name: "Next control" });
    act(() => next.focus());
    fireEvent.pointerEnter(link, { pointerType: "mouse" }); animation(text, "animationStart");
    const handled = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    handled.preventDefault();
    fireEvent(next, handled);
    expect(viewport).toHaveAttribute("data-reveal", "running");
    expect(fireEvent.keyDown(next, { key: "Escape", cancelable: true })).toBe(false);
    expect(viewport).toHaveAttribute("data-reveal", "idle");
    expect(next).toHaveFocus();
  });

  it("provides a portalled static reduced-motion title without duplicating the link name", async () => {
    reduced = true;
    const user = userEvent.setup(); const { viewport, container, link } = mount();
    await user.tab();
    const tooltip = document.querySelector<HTMLElement>('[data-slot="overflow-reveal-tooltip"]')!;
    expect(tooltip).toBeVisible();
    expect(tooltip).toHaveTextContent(longTitle);
    expect(tooltip).toHaveAttribute("aria-hidden", "true");
    expect(container).not.toContainElement(tooltip);
    expect(link).toHaveAccessibleName(new RegExp(`^Document:\\s*${longTitle}\\s*Updated today$`));
    expect(viewport).toHaveAttribute("data-reveal", "static");
    await user.keyboard("{Escape}");
    expect(document.querySelector('[data-slot="overflow-reveal-tooltip"]')).not.toBeInTheDocument();
    expect(viewport).toHaveAttribute("data-reveal", "idle");
    expect(link).toHaveFocus();
  });

  it("does not intercept the first coarse/touch activation or require a reveal tap", () => {
    coarse = true;
    const { viewport, link, activated } = mount();
    fireEvent.pointerEnter(link, { pointerType: "touch" });
    fireEvent.pointerDown(link, { pointerType: "touch" });
    fireEvent.click(link);
    expect(activated).toHaveBeenCalledExactlyOnceWith(false);
    expect(viewport).toHaveAttribute("data-reveal", "idle");
  });

  it("switches an inspected title to static disclosure if the motion preference changes", async () => {
    const { viewport, text, link } = mount();
    act(() => link.focus());
    animation(text, "animationStart");
    expect(viewport).toHaveAttribute("data-reveal", "running");
    act(() => { reduced = true; mediaListeners.forEach(listener => listener()); });
    await waitFor(() => expect(viewport).toHaveAttribute("data-reveal", "static"));
    expect(document.querySelector('[data-slot="overflow-reveal-tooltip"]')).toHaveTextContent(longTitle);
  });

  it("resets when offscreen or the document becomes hidden", () => {
    const { viewport, text, link } = mount();
    fireEvent.pointerEnter(link, { pointerType: "mouse" });
    animation(text, "animationStart");
    act(() => intersections[0].run(false));
    expect(viewport).toHaveAttribute("data-reveal", "idle");
    act(() => intersections[0].run(true));
    expect(viewport).toHaveAttribute("data-reveal", "waiting");
    animation(text, "animationStart");
    act(() => { visibility = "hidden"; document.dispatchEvent(new Event("visibilitychange")); });
    expect(viewport).toHaveAttribute("data-reveal", "idle");
  });

  it("stops beneath a modal or hidden ancestor and rechecks availability", async () => {
    const { viewport, text, link, container } = mount();
    fireEvent.pointerEnter(link, { pointerType: "mouse" });
    animation(text, "animationStart");
    const modal = document.createElement("div"); modal.setAttribute("role", "dialog"); modal.setAttribute("aria-modal", "true");
    document.body.append(modal);
    await waitFor(() => expect(viewport).toHaveAttribute("data-reveal", "idle"));
    modal.remove();
    await waitFor(() => expect(viewport).toHaveAttribute("data-reveal", "waiting"));
    container.setAttribute("inert", "");
    await waitFor(() => expect(viewport).toHaveAttribute("data-reveal", "idle"));
    container.removeAttribute("inert");
    await waitFor(() => expect(viewport).toHaveAttribute("data-reveal", "waiting"));
  });

  it("remeasures resized rows and stops revealing once a title fits", () => {
    const { viewport, text, link } = mount();
    fireEvent.pointerEnter(link, { pointerType: "mouse" }); animation(text, "animationStart");
    viewportWidth = 400; remeasure();
    expect(viewport).toHaveAttribute("data-overflow", "false");
    expect(viewport).toHaveAttribute("data-reveal", "idle");
  });

  it("remeasures loaded fonts and cleans up observers, listeners and pending font work", async () => {
    let finishFonts!: () => void;
    const fonts = new EventTarget();
    Object.defineProperty(fonts, "ready", { value: new Promise<void>(resolve => { finishFonts = resolve; }) });
    const removeFontListener = vi.spyOn(fonts, "removeEventListener");
    Object.defineProperty(document, "fonts", { configurable: true, value: fonts });
    textWidth = 90;
    const { viewport, unmount } = mount();
    expect(viewport).toHaveAttribute("data-overflow", "false");
    textWidth = 300;
    await act(async () => { finishFonts(); await Promise.resolve(); });
    expect(viewport).toHaveAttribute("data-overflow", "true");
    unmount();
    expect(resizes[0].disconnect).toHaveBeenCalledOnce();
    expect(intersections[0].disconnect).toHaveBeenCalledOnce();
    expect(removeFontListener).toHaveBeenCalledWith("loadingdone", expect.any(Function));
    expect(mediaListeners.size).toBe(0);
  });
});
