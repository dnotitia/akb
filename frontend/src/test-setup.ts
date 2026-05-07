import "@testing-library/jest-dom/vitest";

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
