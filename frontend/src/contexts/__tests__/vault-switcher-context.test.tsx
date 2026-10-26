import { useRef, useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate, type NavigateOptions } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { VaultSwitcherProvider, useVaultSwitcher } from "@/contexts/vault-switcher-context";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { ResourceNavigationProvider, useResourceNavigationGuard } from "@/contexts/resource-navigation-context";
import { useVaults } from "@/hooks/use-vaults";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import type { CurrentUser } from "@/lib/api";

vi.mock("@/hooks/use-vaults", () => ({ useVaults: vi.fn() }));
vi.mock("@/hooks/use-vault-favorites", () => ({ useVaultFavorites: () => ({ isFavorite: (id: string) => id === "research-id", favOrder: () => 0 }) }));
vi.mock("@/components/vault-create-form", () => ({
  VaultCreateForm: ({ onCreated, onOpenExisting, onCancel }: { onCreated: (name: string) => void; onOpenExisting: (name: string) => void; onCancel: () => void }) => <div>
    <input aria-label="New vault name" autoFocus />
    <button onClick={() => onCreated("new / vault")}>Finish creating vault</button>
    <button onClick={() => onOpenExisting("research")}>Open existing vault</button>
    <button onClick={onCancel}>Cancel creation</button>
  </div>,
}));

const user: CurrentUser = { user_id: "switcher-user", username: "reader", email: "reader@example.invalid", display_name: "Reader", is_admin: false, auth_method: "local", key_class: null };
const vaults = [{ id: "team-id", name: "team", role: "owner" }, { id: "research-id", name: "research", role: "reader" }, { id: "name-id", name: "팀 / #? A full human name", role: "writer" }];
const refetch = vi.fn();

function Workspace({ guarded = false, onNavigate = vi.fn(), calls = vi.fn() }: { guarded?: boolean; onNavigate?: () => void; calls?: (href: string) => void }) {
  const switcher = useVaultSwitcher();
  const navigate = useNavigate();
  const location = useLocation();
  const trigger = useRef<HTMLButtonElement>(null);
  const headerTrigger = useRef<HTMLButtonElement>(null);
  const editor = useRef<HTMLInputElement>(null);
  const [pending, setPending] = useState<{ href: string; options?: NavigateOptions } | null>(null);
  const continueNavigation = useResourceNavigationGuard(guarded ? (href, options) => { calls(href); setPending({ href, options }); return false; } : null);
  return <>
    <div data-testid="vault-directory-origin"><button ref={trigger} id="workspace-vaults-link" data-vault-switcher-trigger aria-expanded={Boolean(switcher?.isOpen && switcher.trigger === trigger.current)} onClick={event => switcher?.openVaultSwitcher(event.currentTarget, { onNavigate })}>Open Vault directory</button></div>
    <button ref={headerTrigger} id="header-vaults-trigger" data-vault-switcher-trigger aria-expanded={Boolean(switcher?.isOpen && switcher.trigger === headerTrigger.current)} onClick={event => switcher?.openVaultSwitcher(event.currentTarget, { onNavigate, side: "bottom", align: "end" })}>Open Vault directory from header</button>
    <button>Outside action</button>
    <main id="main" tabIndex={-1}><input ref={editor} aria-label="Editor title" /><output aria-label="Location">{location.pathname}</output><output aria-label="Route state">{JSON.stringify(location.state)}</output></main>
    <ConfirmDialog open={pending !== null} onOpenChange={open => { if (!open) setPending(null); }} title="Leave editor?" confirmLabel="Leave editor" cancelLabel="Keep editing" returnFocusRef={editor} onConfirm={() => {
      if (!pending) return;
      const destination = pending;
      setPending(null);
      continueNavigation(() => navigate(destination.href, destination.options));
    }} />
  </>;
}
function mount({ path = "/vault/team/doc/start.md", guarded = false, onNavigate = vi.fn(), calls = vi.fn() } = {}) {
  const content = () => <CurrentUserProvider user={user}><ResourceNavigationProvider><VaultSwitcherProvider><Workspace guarded={guarded} onNavigate={onNavigate} calls={calls} /></VaultSwitcherProvider></ResourceNavigationProvider></CurrentUserProvider>;
  const view = render(<MemoryRouter initialEntries={[path]}>{content()}</MemoryRouter>);
  return { ...view, onNavigate, calls, content };
}
async function openDirectory() {
  await userEvent.click(screen.getByRole("button", { name: "Open Vault directory" }));
  const dialog = screen.getByRole("dialog", { name: "Vaults" });
  await waitFor(() => expect(within(dialog).getByRole("searchbox", { name: "Find a vault" })).toHaveFocus());
  return dialog;
}
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.mocked(useVaults).mockReturnValue({ vaults, loading: false, error: false, refetch });
});

describe("Shared Vault switcher", () => {
  it("has a nullable standalone hook", () => {
    function Probe() { return <output>{useVaultSwitcher() === null ? "No provider" : "Available"}</output>; }
    render(<Probe />);
    expect(screen.getByText("No provider")).toBeVisible();
  });

  it("opens outside a Vault with full names, favorites first and an always-focused filter", async () => {
    mount({ path: "/search" });
    const dialog = await openDirectory();
    const directory = within(dialog).getByRole("navigation", { name: "Accessible vaults" });
    expect(within(directory).getAllByRole("link").map(link => link.textContent)).toEqual(["research", "team", "팀 / #? A full human name"]);
    expect(within(directory).getByRole("link", { name: "팀 / #? A full human name" })).toHaveAttribute("href", `/vault/${encodeURIComponent("팀 / #? A full human name")}`);
    expect(directory.querySelector("[aria-current]")).toBeNull();
    const filter = within(dialog).getByRole("searchbox", { name: "Find a vault" });
    await userEvent.type(filter, "full human");
    expect(within(directory).getAllByRole("link")).toHaveLength(1);
    await userEvent.clear(filter); await userEvent.type(filter, "absent");
    expect(within(dialog).getByText("No vaults match this name.")).toBeVisible();
  });

  it("separates current-Vault recent collections from Vault switching and keeps native deep links", async () => {
    localStorage.setItem(`akb.recentCollections.v1:${user.user_id}`, JSON.stringify([
      { vault: "research", path: "private", viewedAt: new Date(Date.now() - 1_000).toISOString() },
      ...["guides/team", "decisions", "onboarding", "older"].map((path, index) => ({ vault: "team", path, viewedAt: new Date(Date.now() - (index + 2) * 60_000).toISOString() })),
    ]));
    mount();
    const dialog = await openDirectory();
    const toggle = within(dialog).getByRole("button", { name: "Recent collections in team" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    const directory = within(dialog).getByRole("navigation", { name: "Accessible vaults" });
    expect(directory.compareDocumentPosition(toggle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(dialog).queryByRole("navigation", { name: "Recent collections in team" })).toBeNull();
    toggle.focus();
    await userEvent.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const recent = within(dialog).getByRole("navigation", { name: "Recent collections in team" });
    expect(within(recent).getAllByRole("link")).toHaveLength(3);
    expect(within(recent).queryByText("private")).toBeNull();
    expect(within(recent).queryByText("older")).toBeNull();
    const link = within(recent).getByRole("link", { name: "guides / team" });
    const href = "/vault/team?collection=guides%2Fteam";
    expect(link).toHaveAttribute("href", href);
    const search = within(dialog).getByRole("searchbox");
    await userEvent.type(search, "team");
    expect(within(dialog).queryByRole("navigation", { name: "Recent collections in team" })).toBeNull();
    await userEvent.clear(search);
    await userEvent.click(within(dialog).getByRole("button", { name: "Filter by role" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Owned" }));
    expect(within(dialog).queryByRole("navigation", { name: "Recent collections in team" })).toBeNull();
    await userEvent.click(within(dialog).getByRole("button", { name: "Reset filters" }));
    const restoredToggle = within(dialog).getByRole("button", { name: "Recent collections in team" });
    expect(restoredToggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(restoredToggle);
    await userEvent.click(within(dialog).getByRole("link", { name: "guides / team" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Route state")).toHaveTextContent(JSON.stringify({ vaultPageNavigation: href }));
  });

  it("starts each fresh opening with recent collections collapsed", async () => {
    localStorage.setItem(`akb.recentCollections.v1:${user.user_id}`, JSON.stringify([
      { vault: "team", path: "guides", viewedAt: new Date(Date.now() - 1_000).toISOString() },
    ]));
    mount();
    let dialog = await openDirectory();
    const recent = within(dialog).getByRole("button", { name: "Recent collections in team" });
    recent.focus();
    await userEvent.keyboard(" ");
    expect(recent).toHaveAttribute("aria-expanded", "true");
    await userEvent.keyboard("{Escape}");
    dialog = await openDirectory();
    expect(within(dialog).getByRole("button", { name: "Recent collections in team" })).toHaveAttribute("aria-expanded", "false");
    expect(within(dialog).queryByRole("link", { name: "guides" })).toBeNull();
  });

  it("keeps the directory nonmodal, toggles the same external trigger and reanchors without discarding a query", async () => {
    mount();
    const dialog = await openDirectory();
    expect(document.querySelector('[data-slot="dialog-overlay"]')).toBeNull();
    expect(screen.getByRole("main")).toBeVisible();
    const primary = screen.getByRole("button", { name: "Open Vault directory" });
    expect(primary).toHaveAttribute("aria-expanded", "true");
    await userEvent.type(within(dialog).getByRole("searchbox"), "team");
    const header = screen.getByRole("button", { name: "Open Vault directory from header" });
    await userEvent.click(header);
    expect(screen.getByRole("dialog", { name: "Vaults" })).toBe(dialog);
    expect(primary).toHaveAttribute("aria-expanded", "false");
    expect(header).toHaveAttribute("aria-expanded", "true");
    expect(within(dialog).getByRole("searchbox")).toHaveValue("team");
    await waitFor(() => expect(within(dialog).getByRole("searchbox")).toHaveFocus());
    await userEvent.click(header);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(header).toHaveFocus());
  });

  it("lets an outside action retain focus when dismissing the nonmodal picker", async () => {
    mount();
    await openDirectory();
    const outside = screen.getByRole("button", { name: "Outside action" });
    await userEvent.click(outside);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(outside).toHaveFocus());
  });

  it("honors persisted role scope, keeps its selected label visible when closed and resets scope plus name", async () => {
    localStorage.setItem("akb-vault-role-scope", "owned");
    mount();
    let dialog = await openDirectory();
    const directory = within(dialog).getByRole("navigation", { name: "Accessible vaults" });
    expect(within(directory).getAllByRole("link")).toHaveLength(1);
    expect(within(directory).getByRole("link", { name: "team, current vault" })).toBeVisible();
    expect(within(dialog).getByText("Owned", { selector: "span" })).toBeVisible();
    await userEvent.click(within(dialog).getByRole("button", { name: "Filter by role, 1 active" }));
    const roles = within(dialog).getByRole("group", { name: "Filter vaults by your role" });
    expect(within(roles).getByRole("button", { name: "Owned" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(within(roles).getByRole("button", { name: "Can edit" }));
    expect(within(directory).getAllByRole("link")).toHaveLength(2);
    await userEvent.click(within(dialog).getByRole("button", { name: "Filter by role, 1 active" }));
    expect(within(dialog).getByText("Can edit", { selector: "span" })).toBeVisible();
    expect(localStorage.getItem("akb-vault-role-scope")).toBe("editable");
    await userEvent.keyboard("{Escape}");
    dialog = await openDirectory();
    expect(within(dialog).getByText("Can edit", { selector: "span" })).toBeVisible();
    await userEvent.type(within(dialog).getByRole("searchbox"), "missing");
    expect(within(dialog).getByText("No vaults match these filters.")).toBeVisible();
    await userEvent.click(within(dialog).getByRole("button", { name: "Reset filters" }));
    expect(within(dialog).getByRole("searchbox")).toHaveValue("");
    expect(within(dialog).getByRole("button", { name: "Filter by role" })).toHaveAttribute("aria-expanded", "false");
    expect(within(dialog).getByRole("navigation", { name: "Accessible vaults" }).querySelectorAll("a")).toHaveLength(3);
    expect(localStorage.getItem("akb-vault-role-scope")).toBe("all");
  });

  it("marks the accessible current Vault with a check and restores the invoking control on Escape", async () => {
    mount();
    const trigger = screen.getByRole("button", { name: "Open Vault directory" });
    const dialog = await openDirectory();
    const current = within(dialog).getByRole("link", { name: "team, current vault" });
    expect(current).toHaveAttribute("aria-current", "location");
    expect(current.querySelector(".lucide-check")).not.toBeNull();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it.each([{ display: "none" }, { visibility: "hidden" }])("restores a visible main landmark when responsive CSS hides the invoking control's ancestor: %j", async style => {
    mount();
    await openDirectory();
    const origin = screen.getByTestId("vault-directory-origin");
    Object.assign(origin.style, style);
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(origin).not.toBeVisible();
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("accepts a native Vault link, closes once and moves focus to the stable main landmark", async () => {
    const { onNavigate } = mount();
    const dialog = await openDirectory();
    await userEvent.click(within(dialog).getByRole("link", { name: "research" }));
    expect(screen.getByLabelText("Location")).toHaveTextContent("/vault/research");
    expect(screen.getByLabelText("Route state")).toHaveTextContent('{"vaultPageNavigation":"/vault/research"}');
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(onNavigate).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it.each([{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }])("preserves modified clicks without closing or consulting the editor: %j", async modifiers => {
    const { calls, onNavigate } = mount({ guarded: true });
    const dialog = await openDirectory();
    fireEvent.click(within(dialog).getByRole("link", { name: "research" }), modifiers);
    expect(dialog).toBeVisible();
    expect(calls).not.toHaveBeenCalled();
    expect(onNavigate).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Location")).toHaveTextContent("/vault/team/doc/start.md");
  });

  it("keeps a rejected switch and its filter until confirmation, then preserves route state and destination focus", async () => {
    const { calls, onNavigate } = mount({ guarded: true });
    let dialog = await openDirectory();
    await userEvent.type(within(dialog).getByRole("searchbox"), "research");
    await userEvent.click(within(dialog).getByRole("link", { name: "research" }));
    const confirmation = screen.getByRole("dialog", { name: "Leave editor?" });
    expect(confirmation).toBeVisible();
    expect(dialog).toHaveAttribute("inert");
    expect(dialog).toHaveAttribute("aria-hidden", "true");
    expect(calls).toHaveBeenCalledExactlyOnceWith("/vault/research");
    expect(onNavigate).not.toHaveBeenCalled();
    await userEvent.click(within(confirmation).getByRole("button", { name: "Keep editing" }));
    dialog = screen.getByRole("dialog", { name: "Vaults" });
    expect(within(dialog).getByRole("searchbox")).toHaveValue("research");
    await waitFor(() => expect(within(dialog).getByRole("searchbox")).toHaveFocus());
    // Document effects and the closing dialog can restore the editor after
    // the picker's own first restoration frame has already completed.
    await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    act(() => screen.getByRole("textbox", { name: "Editor title" }).focus());
    await waitFor(() => expect(within(dialog).getByRole("searchbox")).toHaveFocus());
    act(() => screen.getByRole("textbox", { name: "Editor title" }).focus());
    await waitFor(() => expect(within(dialog).getByRole("searchbox")).toHaveFocus());
    expect(dialog).toBeVisible();
    expect(within(dialog).getByRole("searchbox")).toHaveValue("research");
    await userEvent.click(within(dialog).getByRole("link", { name: "research" }));
    await userEvent.click(screen.getByRole("button", { name: "Leave editor" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Location")).toHaveTextContent("/vault/research");
    expect(screen.getByLabelText("Route state")).toHaveTextContent('{"vaultPageNavigation":"/vault/research"}');
    expect(onNavigate).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it.each(["pointer", "Tab"])("allows explicit %s departure after a rejected navigation returns to the picker", async departure => {
    mount({ guarded: true });
    let dialog = await openDirectory();
    await userEvent.click(within(dialog).getByRole("link", { name: "research" }));
    await userEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    dialog = screen.getByRole("dialog", { name: "Vaults" });
    await waitFor(() => expect(within(dialog).getByRole("searchbox")).toHaveFocus());
    const outside = screen.getByRole("button", { name: "Outside action" });
    if (departure === "pointer") await userEvent.click(outside);
    else {
      // The footer's last native action is followed by the surrounding page.
      within(dialog).getByRole("button", { name: "Create vault" }).focus();
      await userEvent.tab();
      // jsdom may send Tab to body; a browser focuses its next page control.
      if (!outside.matches(":focus")) outside.focus();
    }
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(outside).toHaveFocus());
  });

  it("provides a guarded directory footer and safe creation-dialog handoff", async () => {
    mount({ path: "/" });
    let dialog = await openDirectory();
    expect(within(dialog).getByRole("link", { name: "View all vaults" })).toHaveAttribute("href", "/vault");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create vault" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "New vault name" })).toHaveFocus());
    expect(screen.getByRole("dialog", { name: "Create a vault" })).toBeVisible();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: "Open Vault directory" })).toHaveFocus());
    dialog = await openDirectory();
    await userEvent.click(within(dialog).getByRole("link", { name: "View all vaults" }));
    expect(screen.getByLabelText("Location")).toHaveTextContent("/vault");
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("refetches after creation and opens the encoded Vault with destination focus", async () => {
    const { onNavigate } = mount({ path: "/" });
    const dialog = await openDirectory();
    await userEvent.click(within(dialog).getByRole("button", { name: "Create vault" }));
    await userEvent.click(screen.getByRole("button", { name: "Finish creating vault" }));
    expect(refetch).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("Location")).toHaveTextContent("/vault/new%20%2F%20vault");
    expect(onNavigate).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("retains editor guards after creation and closes the creation dialog only after confirmed navigation", async () => {
    const { onNavigate } = mount({ guarded: true });
    const dialog = await openDirectory();
    await userEvent.click(within(dialog).getByRole("button", { name: "Create vault" }));
    await userEvent.click(screen.getByRole("button", { name: "Finish creating vault" }));
    expect(screen.getByRole("dialog", { name: "Leave editor?" })).toBeVisible();
    expect(onNavigate).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByRole("dialog", { name: "Create a vault" })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Open existing vault" }));
    await userEvent.click(screen.getByRole("button", { name: "Leave editor" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Route state")).toHaveTextContent('{"vaultPageNavigation":"/vault/research"}');
    expect(onNavigate).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("closes an open directory or creation dialog when its account changes", async () => {
    const content = (identity: string) => <MemoryRouter><CurrentUserProvider user={{ ...user, user_id: identity }}><ResourceNavigationProvider><VaultSwitcherProvider><Workspace /></VaultSwitcherProvider></ResourceNavigationProvider></CurrentUserProvider></MemoryRouter>;
    const view = render(content(user.user_id));
    await openDirectory();
    view.rerender(content("replacement-user"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const dialog = await openDirectory();
    await userEvent.click(within(dialog).getByRole("button", { name: "Create vault" }));
    expect(screen.getByRole("dialog", { name: "Create a vault" })).toBeVisible();
    view.rerender(content("third-user"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("distinguishes loading, update errors, retry and empty accessible directories", async () => {
    vi.mocked(useVaults).mockReturnValue({ vaults: [], loading: true, error: false, refetch });
    const view = mount();
    const dialog = await openDirectory();
    expect(within(dialog).getByRole("status")).toHaveTextContent("Loading vaults");
    expect(screen.queryByText("No vaults yet.")).not.toBeInTheDocument();
    vi.mocked(useVaults).mockReturnValue({ vaults, loading: false, error: true, refetch });
    view.rerender(<MemoryRouter>{view.content()}</MemoryRouter>);
    expect(screen.getByText("Couldn't update vaults. Previous list shown.")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalledOnce();
    vi.mocked(useVaults).mockReturnValue({ vaults: [], loading: false, error: true, refetch });
    view.rerender(<MemoryRouter>{view.content()}</MemoryRouter>);
    expect(screen.getByText("Vaults unavailable.")).toBeVisible();
    expect(screen.queryByRole("link", { name: /current vault/ })).not.toBeInTheDocument();
    vi.mocked(useVaults).mockReturnValue({ vaults: [], loading: false, error: false, refetch });
    view.rerender(<MemoryRouter>{view.content()}</MemoryRouter>);
    expect(screen.getByText("No vaults yet.")).toBeVisible();
  });
});
