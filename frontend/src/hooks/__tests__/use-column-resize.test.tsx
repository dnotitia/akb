import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useColumnResize } from "@/hooks/use-column-resize";

function ResizeColumn() {
  const column = useColumnResize({ storageKey: "test.column", min: 192, max: 320, default: 218 });
  return <div role="separator" tabIndex={0} {...column.handlers}
    aria-valuenow={column.width} data-settled-width={column.settledWidth} data-resizing={column.isResizing} />;
}

let captured: number | null;
beforeEach(() => {
  localStorage.clear();
  captured = null;
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", { configurable: true, value: vi.fn((id: number) => { captured = id; }) });
  vi.spyOn(HTMLElement.prototype, "hasPointerCapture").mockImplementation(id => captured === id);
  vi.spyOn(HTMLElement.prototype, "releasePointerCapture").mockImplementation(() => { captured = null; });
});
afterEach(() => {
  vi.restoreAllMocks();
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
});
function start(handle: HTMLElement) {
  fireEvent.pointerDown(handle, { button: 0, pointerId: 7, clientX: 200, isPrimary: true });
}

describe("useColumnResize", () => {
  it("keeps the layout sizing input stable until release, clamps and persists the resulting width", () => {
    render(<ResizeColumn />);
    const handle = screen.getByRole("separator");
    start(handle);
    expect(handle).toHaveFocus();
    fireEvent.pointerMove(handle, { pointerId: 7, clientX: 400 });
    expect(handle).toHaveAttribute("aria-valuenow", "320");
    expect(handle).toHaveAttribute("data-settled-width", "218");
    expect(handle).toHaveAttribute("data-resizing", "true");
    expect(document.body.style.cursor).toBe("col-resize");
    fireEvent.pointerUp(handle, { pointerId: 7 });
    expect(handle).toHaveAttribute("data-settled-width", "320");
    expect(handle).toHaveAttribute("data-resizing", "false");
    expect(captured).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(localStorage.getItem("test.column")).toBe("320");
  });
  it("ignores a secondary pointer and unrelated pointer moves/releases", () => {
    render(<ResizeColumn />);
    const handle = screen.getByRole("separator");
    fireEvent.pointerDown(handle, { button: 2, pointerId: 8, clientX: 200 });
    expect(handle).toHaveAttribute("data-resizing", "false");
    start(handle);
    fireEvent.pointerMove(handle, { pointerId: 8, clientX: 400 });
    fireEvent.pointerUp(handle, { pointerId: 8 });
    expect(handle).toHaveAttribute("aria-valuenow", "218");
    expect(handle).toHaveAttribute("data-resizing", "true");
    fireEvent.pointerCancel(handle, { pointerId: 7 });
    expect(handle).toHaveAttribute("data-resizing", "false");
  });
  it.each(["pointerCancel", "lostPointerCapture", "blur", "unmount"])("restores existing body styles after %s", termination => {
    document.body.style.cursor = "crosshair";
    document.body.style.userSelect = "text";
    const { unmount } = render(<ResizeColumn />);
    const handle = screen.getByRole("separator");
    start(handle);
    if (termination === "unmount") unmount();
    else if (termination === "blur") act(() => window.dispatchEvent(new Event("blur")));
    else fireEvent[termination === "pointerCancel" ? "pointerCancel" : "lostPointerCapture"](handle, { pointerId: 7 });
    expect(captured).toBeNull();
    expect(document.body.style.cursor).toBe("crosshair");
    expect(document.body.style.userSelect).toBe("text");
  });
  it("supports bounded keyboard resizing, larger Shift steps and double-click reset", () => {
    localStorage.setItem("test.column", "240");
    render(<ResizeColumn />);
    const handle = screen.getByRole("separator");
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(handle).toHaveAttribute("aria-valuenow", "248");
    fireEvent.keyDown(handle, { key: "ArrowLeft", shiftKey: true });
    expect(handle).toHaveAttribute("aria-valuenow", "216");
    fireEvent.keyDown(handle, { key: "Home" });
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(handle).toHaveAttribute("aria-valuenow", "192");
    fireEvent.keyDown(handle, { key: "End" });
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(handle).toHaveAttribute("aria-valuenow", "320");
    fireEvent.doubleClick(handle);
    expect(handle).toHaveAttribute("aria-valuenow", "218");
    expect(localStorage.getItem("test.column")).toBe("218");
  });
});
