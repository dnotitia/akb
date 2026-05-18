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
