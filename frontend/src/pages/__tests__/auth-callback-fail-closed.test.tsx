import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import AuthCallbackPage from "../auth-callback";


afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("AuthCallbackPage", () => {
  it("never exchanges or stores a browser credential", () => {
    const fetchMock = vi.fn();
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MemoryRouter initialEntries={["/auth/callback?code=legacy-code"]}>
        <AuthCallbackPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: /Legacy SSO callback retired/i })).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(storageWrite).not.toHaveBeenCalled();
  });
});
