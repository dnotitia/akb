import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useFlashStatus } from "../use-flash-status";

describe("useFlashStatus", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("starts empty", () => {
    const { result } = renderHook(() => useFlashStatus());
    expect(result.current.message).toBe("");
  });

  it("setFlash shows message then auto-clears after duration", () => {
    const { result } = renderHook(() => useFlashStatus(3000));
    act(() => result.current.setFlash("Saved"));
    expect(result.current.message).toBe("Saved");
    act(() => vi.advanceTimersByTime(2999));
    expect(result.current.message).toBe("Saved");
    act(() => vi.advanceTimersByTime(2));
    expect(result.current.message).toBe("");
  });

  it("re-setting before timer resets the duration", () => {
    const { result } = renderHook(() => useFlashStatus(3000));
    act(() => result.current.setFlash("First"));
    act(() => vi.advanceTimersByTime(2000));
    act(() => result.current.setFlash("Second"));
    act(() => vi.advanceTimersByTime(2999));
    expect(result.current.message).toBe("Second");
    act(() => vi.advanceTimersByTime(2));
    expect(result.current.message).toBe("");
  });

  it("clear() resets immediately", () => {
    const { result } = renderHook(() => useFlashStatus(3000));
    act(() => result.current.setFlash("Saved"));
    act(() => result.current.clear());
    expect(result.current.message).toBe("");
  });
});
