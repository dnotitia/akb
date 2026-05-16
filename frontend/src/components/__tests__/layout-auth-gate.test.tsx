import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { Layout } from "../layout";
import * as api from "@/lib/api";

// Mock api so getToken() can be flipped between tests, and the health
// hook's network call never fires. UserMenu (rendered by Layout) calls
// getMe() in an effect; stub it to resolve null so the component doesn't
// throw inside React's commit phase.
vi.mock("@/lib/api", () => ({
  getToken: vi.fn(),
  setToken: vi.fn(),
  getMe: vi.fn().mockResolvedValue(null),
}));

vi.mock("@/hooks/use-health", () => ({
  useHealth: () => ({ data: undefined, isLoading: false, error: null }),
}));

vi.mock("@/hooks/use-measured-height", () => ({
  // Return the same shape `[ref, number]` the real hook gives.
  useMeasuredHeight: () => [vi.fn(), 0],
}));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<div data-testid="home" />} />
          <Route path="/search" element={<div data-testid="search-page" />} />
        </Route>
        <Route path="/auth" element={<div data-testid="auth-page" />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Layout — auth gate", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => cleanup());

  it("redirects to /auth when no token", () => {
    (api.getToken as any).mockReturnValue(null);
    renderAt("/");
    expect(screen.getByTestId("auth-page")).toBeTruthy();
    expect(screen.queryByTestId("home")).toBeNull();
  });

  it("renders the outlet when a token is present", () => {
    (api.getToken as any).mockReturnValue("fake-jwt");
    renderAt("/");
    expect(screen.getByTestId("home")).toBeTruthy();
    expect(screen.queryByTestId("auth-page")).toBeNull();
  });

  it("preserves hook order across a logged-in → logged-out re-render", () => {
    // Logged-in mount succeeds.
    (api.getToken as any).mockReturnValue("fake-jwt");
    const { rerender, unmount } = renderAt("/");
    expect(screen.getByTestId("home")).toBeTruthy();

    // Flip to logged-out and re-render. With the old code (hooks AFTER
    // the auth-gate early return) this would throw
    // "Rendered fewer hooks than expected" — the regression we just fixed.
    (api.getToken as any).mockReturnValue(null);
    rerender(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<div data-testid="home" />} />
          </Route>
          <Route path="/auth" element={<div data-testid="auth-page" />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("auth-page")).toBeTruthy();
    unmount();
  });
});
