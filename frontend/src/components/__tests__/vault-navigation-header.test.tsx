import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { VaultNavigationHeader } from "@/components/vault-navigation-header";
import { useVaults } from "@/hooks/use-vaults";
import { useVaultSwitcher } from "@/contexts/vault-switcher-context";

vi.mock("@/hooks/use-vaults", () => ({ useVaults: vi.fn() }));
vi.mock("@/contexts/vault-switcher-context", () => ({ useVaultSwitcher: vi.fn() }));

const refetch = vi.fn();
const openVaultSwitcher = vi.fn();

function Location() { return <output aria-label="Location">{useLocation().pathname}</output>; }
function mount({ current = "team", path = "/vault/team/doc/start.md", onNavigate = vi.fn(), onCreateVault = vi.fn(), onRefetchReady = vi.fn(), onToggle = vi.fn() } = {}) {
  const header = () => <MemoryRouter initialEntries={[path]}><VaultNavigationHeader current={current} onCreateVault={onCreateVault} onNavigate={onNavigate} onRefetchReady={onRefetchReady} navigationControl={{ open: true, onToggle }} /><Location /></MemoryRouter>;
  const view = render(header());
  return { ...view, onNavigate, onCreateVault, onRefetchReady, onToggle, rerenderHeader: () => view.rerender(header()) };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useVaults).mockReturnValue({ vaults: [], loading: false, error: false, refetch });
  vi.mocked(useVaultSwitcher).mockReturnValue({ openVaultSwitcher });
});

describe("Vault identity picker", () => {
  it("keeps sidebar collapse and the whole-name picker together without parent navigation", async () => {
    const user = userEvent.setup();
    const { container, onRefetchReady, onToggle } = mount();
    const trigger = screen.getByRole("button", { name: "Switch vault: team" });
    const close = screen.getByRole("button", { name: "Close vault navigation" });
    expect(trigger).toHaveTextContent("team");
    expect(screen.getAllByRole("button").map(button => button.getAttribute("aria-label"))).toEqual(["Switch vault: team", "Close vault navigation"]);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    const identity = container.querySelector('[data-slot="vault-identity-header"]');
    expect(identity).toContainElement(close);
    expect(identity).toContainElement(trigger);
    expect(onRefetchReady).toHaveBeenCalledExactlyOnceWith(refetch);
    await user.tab();
    expect(trigger).toHaveFocus();
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("team"));
    await user.tab();
    expect(close).toHaveFocus();
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("Close vault navigation"));
    await user.click(close);
    expect(onToggle).toHaveBeenCalledOnce();
    expect(openVaultSwitcher).not.toHaveBeenCalled();
    await user.tab({ shift: true });
    expect(trigger).toHaveFocus();
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("team"));
  });

  it("opens the anchored picker when the visible name is clicked", async () => {
    const user = userEvent.setup(); const { onNavigate, onCreateVault } = mount();
    const trigger = screen.getByRole("button", { name: "Switch vault: team" });
    expect(trigger).toHaveAttribute("aria-haspopup", "dialog");
    expect(trigger).toHaveAttribute("data-vault-switcher-trigger");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    await user.click(screen.getByText("team", { exact: true }));
    expect(openVaultSwitcher).toHaveBeenCalledExactlyOnceWith(trigger, { side: "bottom", align: "end", onNavigate });
    expect(onNavigate).not.toHaveBeenCalled();
    expect(onCreateVault).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Location")).toHaveTextContent("/vault/team/doc/start.md");
  });

  it.each(["{Enter}", " "])("opens the same picker with the native button key %s", async key => {
    const user = userEvent.setup(); const { onNavigate } = mount();
    const trigger = screen.getByRole("button", { name: "Switch vault: team" });
    trigger.focus();
    await user.keyboard(key);
    expect(openVaultSwitcher).toHaveBeenCalledExactlyOnceWith(trigger, { side: "bottom", align: "end", onNavigate });
  });

  it("marks only its own picker invocation as expanded", () => {
    const view = mount();
    const trigger = screen.getByRole("button", { name: "Switch vault: team" });
    vi.mocked(useVaultSwitcher).mockReturnValue({ openVaultSwitcher, isOpen: true, trigger });
    view.rerenderHeader();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-controls", "vault-switcher-panel");
    vi.mocked(useVaultSwitcher).mockReturnValue({ openVaultSwitcher, isOpen: true, trigger: document.createElement("button") });
    view.rerenderHeader();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).not.toHaveAttribute("aria-controls");
    vi.mocked(useVaultSwitcher).mockReturnValue({ openVaultSwitcher, isOpen: false, trigger });
    view.rerenderHeader();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps the name visible and collapse usable without the picker provider", async () => {
    const user = userEvent.setup(); vi.mocked(useVaultSwitcher).mockReturnValue(null);
    const { onToggle } = mount();
    const trigger = screen.getByRole("button", { name: "Switch vault: team" });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveTextContent("team");
    expect(openVaultSwitcher).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Close vault navigation" }));
    expect(onToggle).toHaveBeenCalledOnce();
  });

  it("accepts existing shells that omit creation and refresh callbacks", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><VaultNavigationHeader current="team" /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Switch vault: team" }));
    expect(openVaultSwitcher).toHaveBeenCalledWith(expect.any(HTMLElement), { side: "bottom", align: "end", onNavigate: undefined });
  });

  it.each([
    "Team operating knowledge and shared decisions",
    "긴이름을가진지식저장소long-unbroken-vault-name-without-spaces-0123456789",
  ])("preserves the full human name in the control and tooltip: %s", async current => {
    mount({ current });
    const trigger = screen.getByRole("button", { name: `Switch vault: ${current}` });
    expect(trigger).toHaveTextContent(current);
    trigger.focus();
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent(current));
  });

});
