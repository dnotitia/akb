import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { useGraphHistory } from "../use-graph-history";

const account = { user_id: "alice", username: "alice", email: "a@example.com", display_name: null, is_admin: false, auth_method: "local", key_class: null };
beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("graph history ownership", () => {
  it("does not adopt unowned legacy resource titles or saved views", () => {
    localStorage.setItem("akb-graph-recent:team", JSON.stringify([{ doc_id: "private", title: "Another account secret" }]));
    localStorage.setItem("akb-graph-saves:team", JSON.stringify([{ name: "Secret view", url: "?entry=private" }]));
    const wrapper = ({ children }: { children: ReactNode }) => <CurrentUserProvider user={account}>{children}</CurrentUserProvider>;
    const { result } = renderHook(() => useGraphHistory("team"), { wrapper });
    expect(result.current.recent).toEqual([]);
    expect(result.current.saved).toEqual([]);
  });

  it("switches accounts and logout without rendering the previous account's metadata", () => {
    let userId: string | null = "alice";
    const wrapper = ({ children }: { children: ReactNode }) => <CurrentUserProvider user={userId ? { ...account, user_id: userId } : null}>{children}</CurrentUserProvider>;
    const { result, rerender } = renderHook(() => useGraphHistory("team"), { wrapper });
    act(() => {
      result.current.pushRecent({ doc_id: "private", title: "Alice secret" });
      result.current.saveView("Alice view", "?entry=private");
    });
    userId = "bob";
    rerender();
    expect(result.current.recent).toEqual([]);
    expect(result.current.saved).toEqual([]);
    userId = "alice";
    rerender();
    expect(result.current.saved[0]?.name).toBe("Alice view");
    userId = null;
    rerender();
    expect(result.current.recent).toEqual([]);
    expect(result.current.saved).toEqual([]);
  });

  it("survives disabled storage and keeps current-account actions available in memory", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("disabled"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("disabled"); });
    const wrapper = ({ children }: { children: ReactNode }) => <CurrentUserProvider user={account}>{children}</CurrentUserProvider>;
    const { result } = renderHook(() => useGraphHistory("team"), { wrapper });
    act(() => result.current.saveView("My view", "?hops=1"));
    expect(result.current.saved).toEqual([{ name: "My view", url: "?hops=1" }]);
  });

  it("hides history during access verification and preserves preferences after same-account refresh", () => {
    let checking = false;
    let revision = 0;
    const wrapper = ({ children }: { children: ReactNode }) => <CurrentUserProvider user={account} checking={checking} revision={revision}>{children}</CurrentUserProvider>;
    const { result, rerender } = renderHook(() => useGraphHistory("team"), { wrapper });
    act(() => {
      result.current.pushRecent({ doc_id: "private", title: "Private title" });
      result.current.saveView("Private view", "?entry=private");
    });
    checking = true;
    rerender();
    expect(result.current.recent).toEqual([]);
    expect(result.current.saved).toEqual([]);
    checking = false;
    revision = 1;
    rerender();
    expect(result.current.recent).toEqual([{ doc_id: "private", title: "Private title" }]);
    expect(result.current.saved).toEqual([{ name: "Private view", url: "?entry=private" }]);
    expect(localStorage.getItem("akb-graph-saves:v2:alice:team")).toContain("Private view");
  });

  it("ignores malformed storage entries and unsafe saved destinations", () => {
    localStorage.setItem("akb-graph-recent:v2:alice:team", JSON.stringify([null, { title: "Broken" }, { doc_id: "ok", title: "Readable" }]));
    localStorage.setItem("akb-graph-saves:v2:alice:team", JSON.stringify([{ name: "Broken", url: "https://example.com" }, { name: "Safe", url: "?hops=1" }]));
    const wrapper = ({ children }: { children: ReactNode }) => <CurrentUserProvider user={account}>{children}</CurrentUserProvider>;
    const { result } = renderHook(() => useGraphHistory("team"), { wrapper });
    expect(result.current.recent).toEqual([{ doc_id: "ok", title: "Readable" }]);
    expect(result.current.saved).toEqual([{ name: "Safe", url: "?hops=1" }]);
  });
});
