import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HomeConnectionInvitation } from "@/components/home-connection-invitation";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

vi.mock("@/components/connection-setup", () => ({
  ConnectionSetup: ({ onSecretCreated, onTokenCreated, mcpOauthEnabled }: { onSecretCreated: () => void; onTokenCreated?: () => void; mcpOauthEnabled: boolean }) => <>
    <p>{mcpOauthEnabled ? "OAuth is available" : "Personal token setup"}</p>
    <button onClick={() => { onSecretCreated(); onTokenCreated?.(); }}>Create test token</button>
    <input aria-label="Saved private configuration" defaultValue="private-example" />
  </>,
}));

let sequence = 0;
let userId = "";
let desktop = true;
const mediaListeners = new Set<() => void>();
const initialMedia = window.matchMedia;

beforeEach(() => {
  userId = `invitation-user-${++sequence}`;
  desktop = true;
  localStorage.clear();
  mediaListeners.clear();
  window.matchMedia = vi.fn(query => ({
    media: query, get matches() { return desktop; }, onchange: null,
    addEventListener: (_type: string, callback: EventListenerOrEventListenerObject) => mediaListeners.add(callback as () => void),
    removeEventListener: (_type: string, callback: EventListenerOrEventListenerObject) => mediaListeners.delete(callback as () => void),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: () => true,
  }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); window.matchMedia = initialMedia; });

function invitation(eligible = true, id = userId) {
  return <HomeConnectionInvitation userId={id} eligible={eligible} oauthEnabled />;
}
function region() { return screen.getByTestId("home-connection-invitation"); }

describe("Home connection invitation", () => {
  it("escapes animated route containers and stays visible when the page landmark receives focus", () => {
    render(<main tabIndex={-1} data-testid="route-content" style={{ transform: "translateY(0)" }}>{invitation(false)}</main>);
    const main = screen.getByTestId("route-content");
    expect(region().parentElement).toBe(document.body);
    expect(main).not.toContainElement(region());
    const bounds = { left: 0, top: 0, right: 1440, bottom: 1000, width: 1440, height: 1000, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
    vi.spyOn(region(), "getBoundingClientRect").mockReturnValue(bounds);
    vi.spyOn(main, "getBoundingClientRect").mockReturnValue(bounds);
    act(() => main.focus());
    expect(main).toHaveFocus();
    expect(region()).toBeVisible();
  });

  it("introduces the optional capability once, without opening setup or claiming connection health", async () => {
    const { unmount } = render(invitation());
    expect(screen.getByRole("heading", { name: "Use AKB in your AI tools" })).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByText(/not connected|finish setup|0 agents/i)).not.toBeInTheDocument();
    expect(localStorage.getItem(`akb.homeConnectionInvitationSeen:${userId}`)).toBe("1");
    unmount();
    render(invitation());
    expect(region()).toHaveAttribute("data-expanded", "false");
    expect(screen.getByRole("button", { name: "Connect an agent" })).toBeVisible();
  });

  it("minimizes to a labelled button and restores keyboard focus", async () => {
    const user = userEvent.setup();
    render(invitation());
    await user.click(screen.getByRole("button", { name: "Minimize connection guide" }));
    const launcher = screen.getByRole("button", { name: "Connect an agent" });
    await waitFor(() => expect(launcher).toHaveFocus());
    expect(launcher).toHaveAttribute("aria-haspopup", "dialog");
    expect(launcher).toHaveClass("h-11");
  });

  it("honors previous account dismissal and keeps other accounts independent", () => {
    localStorage.setItem(`akb.homeConnectionGuideDismissed:${userId}`, "1");
    const { rerender } = render(invitation());
    expect(region()).toHaveAttribute("data-expanded", "false");
    rerender(invitation(true, `${userId}-other`));
    expect(region()).toHaveAttribute("data-expanded", "true");
  });

  it("only expands after eligibility is verified and uses a neutral launcher otherwise", () => {
    const { rerender } = render(invitation(false));
    expect(region()).toHaveAttribute("data-expanded", "false");
    expect(screen.getByRole("button", { name: "Connect an agent" })).toBeVisible();
    rerender(invitation(true));
    expect(region()).toHaveAttribute("data-expanded", "true");
  });

  it("keeps mobile and short landscape compact, with safe-area positioning", () => {
    desktop = false;
    render(invitation());
    expect(region()).toHaveAttribute("data-expanded", "false");
    expect(region().className).toContain("env(safe-area-inset-bottom)");
    expect(region()).toHaveClass("transition-none");
    act(() => { desktop = true; mediaListeners.forEach(listener => listener()); });
    expect(region()).toHaveAttribute("data-expanded", "true");
    act(() => { desktop = false; mediaListeners.forEach(listener => listener()); });
    expect(region()).toHaveAttribute("data-expanded", "false");
  });

  it("survives blocked storage and does not re-expand after navigation in the same session", async () => {
    const user = userEvent.setup();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Blocked"); });
    const { unmount } = render(invitation());
    await user.click(screen.getByRole("button", { name: "Minimize connection guide" }));
    unmount();
    render(invitation());
    expect(region()).toHaveAttribute("data-expanded", "false");
  });

  it("responds to a dismissal from another tab", () => {
    render(invitation());
    fireEvent(window, new StorageEvent("storage", { key: `akb.homeConnectionInvitationSeen:${userId}`, newValue: "1" }));
    expect(region()).toHaveAttribute("data-expanded", "false");
  });

  it("hides even beneath a transparent modal and restores without stealing its focus", () => {
    const view = (open: boolean) => <>{invitation()}<Dialog open={open}><DialogContent overlayProps={{ className: "bg-transparent" }} aria-describedby={undefined}><DialogTitle>Notifications example</DialogTitle></DialogContent></Dialog></>;
    const { rerender } = render(view(false));
    expect(region()).toBeVisible();
    rerender(view(true));
    expect(region()).not.toBeVisible();
    expect(region()).toHaveAttribute("inert");
    rerender(view(false));
    expect(region()).toBeVisible();
  });

  it("opens existing setup directly, preserves a new secret until confirmed, then restores launcher focus", async () => {
    const user = userEvent.setup();
    const tokenCreated = vi.fn();
    render(<HomeConnectionInvitation userId={userId} eligible oauthEnabled onTokenCreated={tokenCreated} />);
    await user.click(screen.getByRole("button", { name: "Connect an agent" }));
    expect(screen.getByText("OAuth is available")).toBeVisible();
    expect(region()).not.toBeVisible();
    await user.click(screen.getByRole("button", { name: "Create test token" }));
    expect(tokenCreated).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("Saved private configuration")).toHaveValue("private-example");
    await user.click(screen.getByRole("button", { name: "Close" }));
    const confirmation = screen.getByRole("dialog", { name: "Have you saved your token?" });
    expect(confirmation).toBeVisible();
    expect(region()).not.toBeVisible();
    await user.click(within(confirmation).getByRole("button", { name: "Keep setup open" }));
    expect(screen.getByLabelText("Saved private configuration")).toHaveValue("private-example");
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: "I've saved it — close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(region()).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect an agent" })).toHaveFocus());
  });

  it("also launches setup from the compact entry and clears private setup on identity change", async () => {
    const user = userEvent.setup();
    const { rerender } = render(invitation(false));
    await user.click(screen.getByRole("button", { name: "Connect an agent" }));
    expect(screen.getByRole("dialog", { name: "Connect an agent" })).toBeVisible();
    rerender(invitation(true, `${userId}-new`));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(region()).toHaveAttribute("data-expanded", "true");
  });

  it("collapses on focused-content overlap and hides the compact control only while it still overlaps", async () => {
    render(<><button>Middle content</button><button>Bottom content</button><button>Clear content</button>{invitation()}</>);
    const rect = (left: number, top: number, width: number, height: number) => ({ left, top, right: left + width, bottom: top + height, width, height, x: left, y: top, toJSON: () => ({}) } as DOMRect);
    vi.spyOn(region(), "getBoundingClientRect").mockImplementation(() => region().dataset.expanded === "true" ? rect(1000, 500, 304, 180) : rect(1100, 620, 204, 44));
    const middle = screen.getByRole("button", { name: "Middle content" });
    const bottom = screen.getByRole("button", { name: "Bottom content" });
    const clear = screen.getByRole("button", { name: "Clear content" });
    vi.spyOn(middle, "getBoundingClientRect").mockReturnValue(rect(1040, 560, 160, 40));
    vi.spyOn(bottom, "getBoundingClientRect").mockReturnValue(rect(1140, 630, 160, 40));
    vi.spyOn(clear, "getBoundingClientRect").mockReturnValue(rect(200, 300, 160, 40));
    act(() => middle.focus());
    expect(region()).toHaveAttribute("data-expanded", "false");
    expect(region()).toBeVisible();
    expect(middle).toHaveFocus();
    act(() => bottom.focus());
    expect(region()).not.toBeVisible();
    expect(bottom).toHaveFocus();
    act(() => clear.focus());
    expect(region()).toBeVisible();
    expect(clear).toHaveFocus();
  });
});
