import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import {
  TABLE_COLUMN_MAX_WIDTH,
  TABLE_COLUMN_MIN_WIDTH,
  clampTableColumnWidth,
  tableColumnWidthsStorageKey,
  useTableColumnWidths,
} from "@/hooks/use-table-column-widths";

const KEY = "akb.tableColumnWidths.v1:ops/incidents";

function saved(key = KEY) {
  const raw = localStorage.getItem(key);
  return raw === null ? null : JSON.parse(raw);
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("useTableColumnWidths", () => {
  it("keys the widths by vault and table, encoding both", () => {
    expect(tableColumnWidthsStorageKey("ops", "incidents")).toBe(KEY);
    expect(tableColumnWidthsStorageKey("ops team", "a/b")).toBe(
      "akb.tableColumnWidths.v1:ops%20team/a%2Fb",
    );
  });

  it("loads valid saved widths and ignores invalid entries", () => {
    localStorage.setItem(
      KEY,
      // 1e400 parses as Infinity.
      '{"title":240,"notes":80,"summary":960,"tiny":79,"huge":961,"far":1e400,' +
        '"text":"200","none":null,"list":[200],"half":123.5}',
    );
    const { result } = renderHook(() => useTableColumnWidths("ops", "incidents"));
    expect(Object.fromEntries(result.current.widths)).toEqual({
      title: 240,
      notes: 80,
      summary: 960,
      half: 123.5,
    });
    // Loading never writes back.
    expect(localStorage.getItem(KEY)).toContain('"tiny":79');
  });

  it.each([
    ["invalid JSON", "{title: 240"],
    ["an array", "[240]"],
    ["a number", "240"],
    ["null", "null"],
    ["an empty string", ""],
  ])("starts with automatic widths when storage holds %s", (_, raw) => {
    localStorage.setItem(KEY, raw);
    const { result } = renderHook(() => useTableColumnWidths("ops", "incidents"));
    expect(result.current.widths.size).toBe(0);
  });

  it("clamps and rounds a width, and ignores a non-finite one", () => {
    expect(clampTableColumnWidth(12)).toBe(TABLE_COLUMN_MIN_WIDTH);
    expect(clampTableColumnWidth(5000)).toBe(TABLE_COLUMN_MAX_WIDTH);
    expect(clampTableColumnWidth(123.5)).toBe(124);

    const { result } = renderHook(() => useTableColumnWidths("ops", "incidents"));
    act(() => result.current.setWidth("title", 12));
    expect(result.current.widths.get("title")).toBe(80);
    act(() => result.current.setWidth("title", 5000));
    expect(result.current.widths.get("title")).toBe(960);
    act(() => result.current.setWidth("title", 200.4));
    expect(result.current.widths.get("title")).toBe(200);
    act(() => result.current.setWidth("title", Number.NaN));
    act(() => result.current.setWidth("title", Number.POSITIVE_INFINITY));
    expect(result.current.widths.get("title")).toBe(200);
    expect(saved()).toEqual({ title: 200 });
  });

  it("saves a set width, and removes it again on reset", () => {
    const { result } = renderHook(() => useTableColumnWidths("ops", "incidents"));
    act(() => result.current.setWidth("title", 240));
    act(() => result.current.setWidth("notes", 320));
    expect(saved()).toEqual({ title: 240, notes: 320 });

    act(() => result.current.resetWidth("title"));
    expect(result.current.widths.has("title")).toBe(false);
    expect(saved()).toEqual({ notes: 320 });

    // The last reset leaves nothing behind.
    act(() => result.current.resetWidth("notes"));
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("restores saved widths on the next mount", () => {
    const first = renderHook(() => useTableColumnWidths("ops", "incidents"));
    act(() => first.result.current.setWidth("title", 240));
    first.unmount();

    const { result } = renderHook(() => useTableColumnWidths("ops", "incidents"));
    expect(result.current.widths.get("title")).toBe(240);
  });

  it("treats columns named like object properties as ordinary columns", () => {
    const first = renderHook(() => useTableColumnWidths("ops", "incidents"));
    expect(first.result.current.widths.get("constructor")).toBeUndefined();
    act(() => first.result.current.setWidth("__proto__", 200));
    act(() => first.result.current.setWidth("constructor", 300));
    first.unmount();

    const { result } = renderHook(() => useTableColumnWidths("ops", "incidents"));
    expect(result.current.widths.get("__proto__")).toBe(200);
    expect(result.current.widths.get("constructor")).toBe(300);
    expect(result.current.widths.get("toString")).toBeUndefined();
  });

  it("keeps each table's widths to itself", () => {
    const incidents = renderHook(() => useTableColumnWidths("ops", "incidents"));
    const releases = renderHook(() => useTableColumnWidths("ops", "releases"));
    const otherVault = renderHook(() => useTableColumnWidths("eng", "incidents"));
    act(() => incidents.result.current.setWidth("title", 240));

    expect(releases.result.current.widths.size).toBe(0);
    expect(otherVault.result.current.widths.size).toBe(0);
    expect(saved("akb.tableColumnWidths.v1:ops/releases")).toBeNull();
    expect(saved("akb.tableColumnWidths.v1:eng/incidents")).toBeNull();

    act(() => releases.result.current.setWidth("title", 400));
    expect(saved()).toEqual({ title: 240 });
    expect(saved("akb.tableColumnWidths.v1:ops/releases")).toEqual({ title: 400 });
  });

  it("loads the other table's widths when the table changes", () => {
    localStorage.setItem("akb.tableColumnWidths.v1:ops/releases", '{"title":400}');
    const { result, rerender } = renderHook(
      ({ table }) => useTableColumnWidths("ops", table),
      { initialProps: { table: "incidents" } },
    );
    act(() => result.current.setWidth("title", 240));

    rerender({ table: "releases" });
    expect(result.current.widths.get("title")).toBe(400);
    rerender({ table: "incidents" });
    expect(result.current.widths.get("title")).toBe(240);
  });

  it("keeps widths in memory when storage throws", () => {
    const failure = () => {
      throw new Error("Storage disabled");
    };
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(failure);
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(failure);
    const removeItem = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(failure);

    const { result } = renderHook(() => useTableColumnWidths("ops", "incidents"));
    expect(getItem).toHaveBeenCalledWith(KEY);
    expect(result.current.widths.size).toBe(0);

    act(() => result.current.setWidth("title", 240));
    expect(setItem).toHaveBeenCalledWith(KEY, '{"title":240}');
    expect(result.current.widths.get("title")).toBe(240);

    act(() => result.current.resetWidth("title"));
    expect(removeItem).toHaveBeenCalledWith(KEY);
    expect(result.current.widths.has("title")).toBe(false);
  });

  it("keeps widths in memory only without a vault and table", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const { result } = renderHook(() => useTableColumnWidths(undefined, "incidents"));
    act(() => result.current.setWidth("title", 240));
    expect(result.current.widths.get("title")).toBe(240);
    expect(setItem).not.toHaveBeenCalled();
  });

  it("keeps its callbacks stable across renders", () => {
    const { result, rerender } = renderHook(() => useTableColumnWidths("ops", "incidents"));
    const { setWidth, resetWidth } = result.current;
    act(() => result.current.setWidth("title", 240));
    rerender();
    expect(result.current.setWidth).toBe(setWidth);
    expect(result.current.resetWidth).toBe(resetWidth);
  });
});
