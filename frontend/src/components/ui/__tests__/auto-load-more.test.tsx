import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AutoLoadMore } from "../auto-load-more";

let intersect: IntersectionObserverCallback;
let options: IntersectionObserverInit | undefined;
const disconnect = vi.fn();
beforeEach(() => {
  vi.stubGlobal("IntersectionObserver", class {
    constructor(callback: IntersectionObserverCallback, init: IntersectionObserverInit) { intersect = callback; options = init; }
    observe() {}
    disconnect = disconnect;
  });
});
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });
function approachEnd() { act(() => intersect([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver)); }

it("observes its scrolling panel, requests once per page, and continues after an empty page", () => {
  const load = vi.fn();
  const view = (page: number, loading = false) => <div style={{ overflowY: "auto" }} data-testid="scroller">
    <AutoLoadMore hasMore loading={loading} requestKey={page} onLoadMore={load} />
  </div>;
  const { rerender } = render(view(1));
  expect(options?.root).toBe(screen.getByTestId("scroller"));
  expect(options?.rootMargin).toBe("0px 0px 240px 0px");
  approachEnd(); approachEnd();
  expect(load).toHaveBeenCalledTimes(1);
  rerender(view(1, true)); approachEnd();
  expect(load).toHaveBeenCalledTimes(1);
  rerender(view(2)); approachEnd();
  expect(load).toHaveBeenCalledTimes(2);
});

it("stops automatic retries on failure and keeps a keyboard-accessible retry", () => {
  const load = vi.fn();
  const { rerender } = render(<AutoLoadMore hasMore requestKey="one" onLoadMore={load} />);
  approachEnd();
  rerender(<AutoLoadMore hasMore error requestKey="one" onLoadMore={load} />);
  approachEnd();
  expect(load).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "Retry: load more items" }));
  expect(load).toHaveBeenCalledTimes(2);
});

it("does not load hidden or paused workspaces and removes the boundary at completion", () => {
  const load = vi.fn();
  const { rerender } = render(<div hidden><AutoLoadMore hasMore requestKey="one" onLoadMore={load} /></div>);
  approachEnd(); expect(load).not.toHaveBeenCalled();
  rerender(<AutoLoadMore hasMore paused requestKey="two" onLoadMore={load} />);
  approachEnd(); expect(load).not.toHaveBeenCalled();
  rerender(<AutoLoadMore hasMore={false} requestKey="two" onLoadMore={load} />);
  expect(screen.queryByRole("button")).toBeNull();
});

it("resumes the same cached page after its request was cancelled", () => {
  const load = vi.fn();
  const view = (loading = false, paused = false, requestKey = "scope-a:1") =>
    <AutoLoadMore hasMore loading={loading} paused={paused} requestKey={requestKey} onLoadMore={load} />;
  const { rerender } = render(view());
  approachEnd();
  rerender(view(true));
  rerender(view(false, true, "scope-b:1"));
  rerender(view());
  approachEnd(); approachEnd();
  expect(load).toHaveBeenCalledTimes(2);
  rerender(view(true));
  rerender(view());
  approachEnd();
  expect(load).toHaveBeenCalledTimes(3);
});

it("pauses scroll demand without disabling the explicit action and resumes on expansion", () => {
  const load = vi.fn();
  const { rerender } = render(<AutoLoadMore hasMore requestKey="one" onLoadMore={load} />);
  approachEnd();
  rerender(<AutoLoadMore hasMore requestKey="one" autoPaused onLoadMore={load} />);
  approachEnd();
  expect(load).toHaveBeenCalledTimes(1);
  const action = screen.getByRole("button", { name: "Load more items" });
  expect(action).toBeEnabled();
  fireEvent.click(action);
  expect(load).toHaveBeenCalledTimes(2);
  rerender(<AutoLoadMore hasMore requestKey="one" onLoadMore={load} />);
  approachEnd();
  expect(load).toHaveBeenCalledTimes(3);
});
