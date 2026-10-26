import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate, type NavigateOptions } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { VaultParentBackLink } from "@/components/vault-navigation-controls";
import { VaultOverviewNavigationFocus } from "@/components/vault-overview-navigation";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";

vi.mock("@/contexts/resource-navigation-context", () => ({ useResourceNavigation: vi.fn() }));
const requestNavigation = vi.fn<(href: string, options?: NavigateOptions) => boolean>(() => true);
function Location() { return <output aria-label="Location">{useLocation().pathname}</output>; }
function mount(path = "/vault/team/doc/start.md") {
  return render(<MemoryRouter initialEntries={[path]}><VaultParentBackLink /><Location /></MemoryRouter>);
}
beforeEach(() => {
  vi.clearAllMocks();
  requestNavigation.mockReturnValue(true);
  vi.mocked(useResourceNavigation).mockReturnValue({ requestNavigation, registerGuard: vi.fn(), continueNavigation: vi.fn(), completeSessionNavigation: vi.fn() });
});

describe("Vault parent navigation", () => {
  it.each(["/vault/team", "/vault/team/", "/vault/team?collection=guides"])("returns from the Vault overview to all vaults at %s", async path => {
    mount(path);
    const back = screen.getByRole("link", { name: "Back to all vaults" });
    expect(back).toHaveAttribute("href", "/vault");
    await userEvent.click(back);
    expect(screen.getByLabelText("Location")).toHaveTextContent(/^\/vault$/);
  });

  it.each(["doc/start.md", "file/file-id", "table/services", "settings", "members", "publications", "graph", "activity", "search", "doc/new"])("returns directly from %s to its Vault overview without prior history", async suffix => {
    mount(`/vault/team/${suffix}`);
    const back = screen.getByRole("link", { name: "Back to vault team" });
    expect(back).toHaveAttribute("href", "/vault/team");
    await userEvent.click(back);
    expect(screen.getByLabelText("Location")).toHaveTextContent(/^\/vault\/team$/);
    expect(screen.getByRole("link", { name: "Back to all vaults" })).toHaveAttribute("href", "/vault");
  });

  it("encodes a Vault name once when returning from a nested resource", () => {
    const vault = "팀 / #? 100%";
    mount(`/vault/${encodeURIComponent(vault)}/doc/guides%2Fstart.md`);
    expect(screen.getByRole("link", { name: `Back to vault ${vault}` })).toHaveAttribute("href", `/vault/${encodeURIComponent(vault)}`);
  });

  it("has no parent action outside a named Vault", () => {
    mount("/vault");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("uses a native parent link that respects unsaved navigation guards", async () => {
    const user = userEvent.setup();
    mount();
    const back = screen.getByRole("link", { name: "Back to vault team" });
    expect(back).toHaveAttribute("href", "/vault/team");
    expect(back).toHaveTextContent("");
    requestNavigation.mockReturnValue(false);
    await user.click(back);
    expect(screen.getByLabelText("Location")).toHaveTextContent("/vault/team/doc/start.md");
    requestNavigation.mockReturnValue(true);
    await user.click(back);
    expect(requestNavigation).toHaveBeenLastCalledWith("/vault/team", { state: { vaultPageNavigation: "/vault/team" } });
    expect(screen.getByLabelText("Location")).toHaveTextContent(/^\/vault\/team$/);
  });

  it.each([{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }])("preserves native modified parent links: %j", modifiers => {
    mount();
    fireEvent.click(screen.getByRole("link", { name: "Back to vault team" }), modifiers);
    expect(requestNavigation).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Location")).toHaveTextContent("/vault/team/doc/start.md");
  });

  it("preserves destination focus when confirming guarded parent navigation", async () => {
    const user = userEvent.setup();
    let pending: { href: string; options?: NavigateOptions } | undefined;
    requestNavigation.mockImplementation((href, options) => { pending = { href, options }; return false; });
    function Confirmation() {
      const navigate = useNavigate();
      return <button onClick={() => { if (pending) navigate(pending.href, pending.options); }}>Confirm leaving editor</button>;
    }
    render(<MemoryRouter initialEntries={["/vault/team/doc/start.md"]}>
      <VaultParentBackLink /><VaultOverviewNavigationFocus />
      <main id="main" tabIndex={-1}><Location /><Confirmation /></main>
    </MemoryRouter>);
    await user.click(screen.getByRole("link", { name: "Back to vault team" }));
    expect(screen.getByLabelText("Location")).toHaveTextContent("/vault/team/doc/start.md");
    await user.click(screen.getByRole("button", { name: "Confirm leaving editor" }));
    expect(screen.getByLabelText("Location")).toHaveTextContent(/^\/vault\/team$/);
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });
});
