import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ResourceNavigationProvider, useResourceNavigation, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";

afterEach(cleanup);

function Guard({ guard }: { guard: ((href: string) => boolean) | null }) {
  useResourceNavigationGuard(guard);
  return null;
}

function Destination({ onAllowed }: { onAllowed: () => void }) {
  const { requestNavigation } = useResourceNavigation();
  return <button onClick={() => { if (requestNavigation("/vault/v/search")) onAllowed(); }}>Search</button>;
}

describe("explicit resource navigation guard", () => {
  it("allows ordinary navigation without a provider", async () => {
    const allowed = vi.fn();
    render(<Destination onAllowed={allowed} />);
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(allowed).toHaveBeenCalledOnce();
  });

  it("passes the destination to the active editor and clears its guard on unmount", async () => {
    const allowed = vi.fn();
    const guard = vi.fn(() => false);
    const { rerender } = render(<ResourceNavigationProvider><Guard guard={guard} /><Destination onAllowed={allowed} /></ResourceNavigationProvider>);
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(guard).toHaveBeenCalledWith("/vault/v/search");
    expect(allowed).not.toHaveBeenCalled();
    rerender(<ResourceNavigationProvider><Destination onAllowed={allowed} /></ResourceNavigationProvider>);
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(allowed).toHaveBeenCalledOnce();
  });

  it("uses the latest guard and resumes normal links when the editor becomes clean", async () => {
    const allowed = vi.fn();
    const first = vi.fn(() => false);
    const second = vi.fn(() => false);
    const contents = (guard: ((href: string) => boolean) | null) => <ResourceNavigationProvider><Guard guard={guard} /><Destination onAllowed={allowed} /></ResourceNavigationProvider>;
    const { rerender } = render(contents(first));
    rerender(contents(second));
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledOnce();
    rerender(contents(null));
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(allowed).toHaveBeenCalledOnce();
  });
});
