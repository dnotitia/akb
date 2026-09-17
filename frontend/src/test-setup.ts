import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Auto-cleanup after each test. @testing-library/react only registers this
// automatically when `afterEach` is a global (i.e. globals:true in vitest
// config). Since this project uses globals:false, we wire it up here.
afterEach(() => {
  cleanup();
});

// jsdom doesn't implement ResizeObserver — our useMeasuredHeight relies on it,
// so provide a no-op so components don't throw during mount.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (typeof globalThis.ResizeObserver === "undefined") {
  (globalThis as any).ResizeObserver = ResizeObserverStub;
}

// jsdom lacks the pointer-capture + scrollIntoView APIs that Radix popovers
// (DropdownMenu / Select) call when opening — stub them so menu-based controls
// can open under test instead of throwing on mount/interaction.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
}
if (!Element.prototype.releasePointerCapture) {
  Element.prototype.releasePointerCapture = () => {};
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// ProseMirror asks the browser for layout information while a contenteditable
// selection is being moved. jsdom intentionally has no layout engine, so keep
// the shared Tiptap surface deterministic in unit tests.
const editorRect = {
  bottom: 0,
  height: 0,
  left: 0,
  right: 0,
  top: 0,
  width: 0,
  x: 0,
  y: 0,
  toJSON: () => ({}),
} as DOMRect;
Object.defineProperty(document, "elementFromPoint", {
  configurable: true,
  value: () => document.body,
});
HTMLElement.prototype.getBoundingClientRect = () => editorRect;
HTMLElement.prototype.getClientRects = () =>
  [editorRect] as unknown as DOMRectList;
Range.prototype.getBoundingClientRect = () => editorRect;
Range.prototype.getClientRects = () => [editorRect] as unknown as DOMRectList;

// jsdom doesn't implement matchMedia — stub it so theme hooks don't throw.
if (typeof window.matchMedia === "undefined") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (_query: string) => ({
      matches: false,
      media: _query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}
