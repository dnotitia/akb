import { act, cleanup, render, screen } from "@testing-library/react";
import { BrowserRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installDocumentHistoryGuard, useDocumentHistoryGuard } from "@/hooks/use-document-history-guard";

let uninstall: () => void;

function Probe({ enabled, requestExit }: {
  enabled: boolean;
  requestExit: (proceed: () => void) => void;
}) {
  useDocumentHistoryGuard(enabled, requestExit);
  return <output aria-label="Rendered route">{useLocation().pathname}</output>;
}

function renderGuard(enabled = true, requestExit = vi.fn<(proceed: () => void) => void>()) {
  const props = { enabled, requestExit };
  const view = render(<BrowserRouter><Probe {...props} /></BrowserRouter>);
  return {
    ...view,
    requestExit,
    setEnabled: (next: boolean) => view.rerender(
      <BrowserRouter><Probe {...props} enabled={next} /></BrowserRouter>,
    ),
  };
}

function pop(index: number) {
  act(() => {
    window.history.replaceState({ idx: index, key: `entry-${index}` }, "", `/entry-${index}`);
    window.dispatchEvent(new PopStateEvent("popstate", { state: window.history.state }));
  });
}

function flushCorrection() {
  act(() => vi.runOnlyPendingTimers());
}

beforeEach(() => {
  vi.useFakeTimers();
  window.history.replaceState({ idx: 4, key: "entry-4" }, "", "/entry-4");
  vi.spyOn(window.history, "go").mockImplementation(() => {});
  uninstall = installDocumentHistoryGuard();
});

afterEach(() => {
  cleanup();
  uninstall();
  vi.runOnlyPendingTimers();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("useDocumentHistoryGuard", () => {
  it("lets clean browser navigation reach BrowserRouter without a corrective traversal", () => {
    const { requestExit } = renderGuard(false);
    pop(3);
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-3");
    expect(window.history.go).not.toHaveBeenCalled();
    expect(requestExit).not.toHaveBeenCalled();
  });

  it("restores a blocked Back before requesting exit, then replays it only once", () => {
    const { requestExit } = renderGuard();
    pop(3);
    flushCorrection();
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-4");
    expect(window.history.go).toHaveBeenLastCalledWith(1);
    expect(requestExit).not.toHaveBeenCalled();

    pop(4);
    expect(requestExit).toHaveBeenCalledTimes(1);
    const proceed = requestExit.mock.calls[0][0];
    act(() => { proceed(); proceed(); });
    expect(window.history.go).toHaveBeenCalledTimes(2);
    expect(window.history.go).toHaveBeenLastCalledWith(-1);

    pop(3);
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-3");
    expect(requestExit).toHaveBeenCalledTimes(1);
  });

  it("keeps the current route after cancelling Forward and invalidates its old confirmation", () => {
    const { requestExit } = renderGuard();
    pop(5);
    flushCorrection();
    expect(window.history.go).toHaveBeenLastCalledWith(-1);
    pop(4);
    const cancelledProceed = requestExit.mock.calls[0][0];
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-4");

    pop(3);
    flushCorrection();
    pop(4);
    expect(requestExit).toHaveBeenCalledTimes(2);
    act(cancelledProceed);
    expect(window.history.go).toHaveBeenCalledTimes(2);
    act(requestExit.mock.calls[1][0]);
    expect(window.history.go).toHaveBeenLastCalledWith(-1);
    pop(3);
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-3");
  });

  it("coalesces rapid Back events and preserves the first requested destination", () => {
    const { requestExit } = renderGuard();
    pop(3);
    pop(2);
    flushCorrection();
    expect(window.history.go).toHaveBeenCalledTimes(1);
    expect(window.history.go).toHaveBeenLastCalledWith(2);
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-4");
    pop(4);
    expect(requestExit).toHaveBeenCalledTimes(1);
    act(requestExit.mock.calls[0][0]);
    expect(window.history.go).toHaveBeenLastCalledWith(-1);
    pop(3);
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-3");
  });

  it("does not mistake an intervening Back for the corrective POP", () => {
    const { requestExit } = renderGuard();
    pop(3);
    flushCorrection();
    pop(2);
    flushCorrection();
    expect(requestExit).not.toHaveBeenCalled();
    expect(window.history.go).toHaveBeenLastCalledWith(2);
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-4");
    pop(4);
    expect(requestExit).toHaveBeenCalledTimes(1);
  });

  it("finishes restoring without opening a stale prompt after the guard becomes clean", () => {
    const { requestExit, setEnabled } = renderGuard();
    pop(3);
    flushCorrection();
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-4");
    setEnabled(false);
    pop(4);
    expect(requestExit).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-4");
    pop(3);
    expect(screen.getByLabelText("Rendered route")).toHaveTextContent("/entry-3");
  });

  it("invalidates a confirmation callback when its owner unmounts", () => {
    const { requestExit, unmount } = renderGuard();
    pop(3);
    flushCorrection();
    pop(4);
    const proceed = requestExit.mock.calls[0][0];
    unmount();
    act(proceed);
    expect(window.history.go).toHaveBeenCalledTimes(1);
  });

  it("drains only the pending corrective event on unmount and releases subsequent navigation", () => {
    const bubbleListener = vi.fn();
    window.addEventListener("popstate", bubbleListener);
    const { requestExit, unmount } = renderGuard();
    pop(3);
    flushCorrection();
    unmount();
    pop(4);
    expect(bubbleListener).not.toHaveBeenCalled();
    expect(requestExit).not.toHaveBeenCalled();
    pop(3);
    expect(bubbleListener).toHaveBeenCalledTimes(1);
    window.removeEventListener("popstate", bubbleListener);
  });
});
