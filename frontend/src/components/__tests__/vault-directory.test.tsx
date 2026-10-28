import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { VaultDirectory } from "@/components/vault-directory";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import type { ComponentProps } from "react";

const KEY = "akb-vault-favorites:v2:directory-focus-test";
const vaults = [
  { id: "v-alpha", name: "alpha", role: "owner" },
  { id: "v-beta", name: "beta", role: "reader" },
  { id: "v-gamma", name: "gamma", role: "writer" },
];

function CurrentLocation() {
  return <div aria-label="Current route">{useLocation().pathname}</div>;
}

function renderDirectory(favorites: string[], rows: ComponentProps<typeof VaultDirectory>["vaults"] = vaults) {
  localStorage.setItem(KEY, JSON.stringify(favorites));
  return render(<MemoryRouter><CurrentUserProvider user={{
    user_id: "directory-focus-test", username: "fixture", email: "fixture@example.test",
    display_name: null, is_admin: false, auth_method: "local", key_class: null,
  }}><VaultDirectory vaults={rows} /><CurrentLocation /></CurrentUserProvider></MemoryRouter>);
}

beforeEach(() => {
  localStorage.clear();
  window.dispatchEvent(new StorageEvent("storage", { key: null }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("VaultDirectory cards", () => {
  it("keeps a semantic list with one named native link and a separate favorite control per card", async () => {
    const user = userEvent.setup();
    renderDirectory([]);
    const list = screen.getByRole("list", { name: "Available vaults" });
    const cards = within(list).getAllByRole("listitem");
    expect(cards).toHaveLength(3);
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
    expect(within(list).queryByText("Owner")).not.toBeInTheDocument();
    for (const [index, card] of cards.entries()) {
      const link = within(card).getByRole("link", { name: vaults[index].name });
      expect(link).toHaveAttribute("href", `/vault/${vaults[index].name}`);
      expect(within(link).queryByRole("button")).not.toBeInTheDocument();
      expect(within(card).getAllByRole("button")).toHaveLength(1);
    }
    screen.getByRole("link", { name: "alpha" }).focus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Add alpha to favorites" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("link", { name: "beta" })).toHaveFocus();
  });

  it("keeps full names and read-only/archive information available alongside descriptions", () => {
    const name = "Shared research and architecture decisions across international product teams";
    renderDirectory([], [{ id: "shared", name, role: "reader", status: "archived", description: "Historical decisions and source materials." }]);
    const link = screen.getByRole("link", { name });
    expect(within(link).getByRole("heading", { level: 2, name })).toBeVisible();
    expect(within(link).getByText("Archived")).toBeVisible();
    expect(within(link).getByText("Read only")).toBeVisible();
    expect(link).toHaveAccessibleDescription("Historical decisions and source materials. Archived Read only");
    expect(within(link).queryByText("Open vault")).not.toBeInTheDocument();
  });

  it("opens the encoded Vault route through the card name", async () => {
    const user = userEvent.setup();
    const name = "team notes 한글";
    renderDirectory([], [{ id: "shared", name }]);
    const link = screen.getByRole("link", { name });
    expect(link).toHaveAttribute("href", `/vault/${encodeURIComponent(name)}`);
    await user.click(within(link).getByRole("heading", { name }));
    expect(screen.getByLabelText("Current route")).toHaveTextContent(`/vault/${encodeURIComponent(name)}`);
  });

  it.each([{ ctrlKey: true }, { metaKey: true }, { button: 1 }])("preserves native modified activation %j", modifier => {
    renderDirectory([]);
    let prevented: boolean | undefined;
    const observe = (event: MouseEvent) => {
      prevented = event.defaultPrevented;
      event.preventDefault(); // JSDOM cannot perform the browser's new-tab action.
    };
    document.addEventListener("click", observe, { once: true });
    fireEvent.click(screen.getByRole("link", { name: "alpha" }), modifier);
    expect(prevented).toBe(false);
    expect(screen.getByLabelText("Current route")).toHaveTextContent(/^\/$/);
  });

  it("retains description search, role filtering, and progressive display", async () => {
    const user = userEvent.setup();
    const rows = Array.from({ length: 31 }, (_, index) => ({ id: `v-${index}`, name: `vault-${String(index).padStart(2, "0")}`, role: index === 30 ? "owner" : "reader", description: index === 30 ? "Architecture archive" : "Team notes" }));
    renderDirectory([], rows);
    expect(within(screen.getByRole("list", { name: "Available vaults" })).getAllByRole("listitem")).toHaveLength(30);
    await user.click(screen.getByRole("button", { name: "Show more vaults" }));
    expect(within(screen.getByRole("list", { name: "Available vaults" })).getAllByRole("listitem")).toHaveLength(31);
    await user.click(screen.getByRole("button", { name: "List view" }));
    expect(within(screen.getByRole("list", { name: "Available vaults" })).getAllByRole("listitem")).toHaveLength(31);
    await user.type(screen.getByRole("textbox", { name: "Find a vault" }), "architecture");
    await user.click(screen.getByRole("button", { name: "Owned" }));
    expect(within(screen.getByRole("list", { name: "Available vaults" })).getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "vault-30" })).toBeVisible();
    expect(screen.getByText("1 vault matching your filters")).toBeVisible();
  });
});

describe("VaultDirectory view preference", () => {
  it("switches layout without losing search, filters, or favorites and remembers it on return", async () => {
    const user = userEvent.setup();
    const rendered = renderDirectory(["v-alpha"]);
    expect(screen.getByRole("button", { name: "Grid view" })).toHaveAttribute("aria-pressed", "true");
    await user.type(screen.getByRole("textbox", { name: "Find a vault" }), "alpha");
    await user.click(screen.getByRole("button", { name: "Favorites" }));
    await user.click(screen.getByRole("button", { name: "List view" }));
    expect(screen.getByRole("button", { name: "List view" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "List view" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("textbox", { name: "Find a vault" })).toHaveValue("alpha");
    expect(screen.getByRole("button", { name: "Favorites" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("list", { name: "Available vaults" })).toHaveAttribute("data-view", "list");
    expect(screen.getByRole("link", { name: "alpha" })).toHaveAttribute("href", "/vault/alpha");
    expect(screen.getByRole("button", { name: "Remove alpha from favorites" })).toHaveAttribute("aria-pressed", "true");
    rendered.unmount();
    renderDirectory(["v-alpha"]);
    expect(screen.getByRole("button", { name: "List view" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Grid view" }));
    expect(screen.getByRole("list", { name: "Available vaults" })).toHaveAttribute("data-view", "grid");
  });

  it("keeps both views usable when browser storage is blocked", async () => {
    const user = userEvent.setup();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    render(<MemoryRouter><VaultDirectory vaults={vaults} /></MemoryRouter>);
    expect(screen.getByRole("button", { name: "Grid view" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "List view" }));
    expect(screen.getByRole("list", { name: "Available vaults" })).toHaveAttribute("data-view", "list");
    expect(screen.getAllByRole("link")).toHaveLength(3);
  });
});

describe("VaultDirectory favorite removal focus", () => {
  it.each(["{Enter}", " "])("focuses the next favorite after keyboard removal with %s", async key => {
    const user = userEvent.setup();
    renderDirectory(["v-alpha", "v-gamma", "v-beta"]);
    await user.click(screen.getByRole("button", { name: "Favorites" }));
    screen.getByRole("button", { name: "Remove alpha from favorites" }).focus();

    await user.keyboard(key);

    expect(screen.queryByRole("link", { name: "alpha" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove gamma from favorites" })).toHaveFocus();
  });

  it("focuses the preceding favorite after removing the final row", async () => {
    const user = userEvent.setup();
    renderDirectory(["v-alpha", "v-beta"]);
    await user.click(screen.getByRole("button", { name: "Favorites" }));
    screen.getByRole("button", { name: "Remove beta from favorites" }).focus();

    await user.keyboard("{Enter}");

    expect(screen.getByRole("button", { name: "Remove alpha from favorites" })).toHaveFocus();
  });

  it("focuses the stable Favorites filter when the last matching favorite disappears", async () => {
    const user = userEvent.setup();
    renderDirectory(["v-alpha", "v-beta"]);
    await user.click(screen.getByRole("button", { name: "Favorites" }));
    await user.type(screen.getByRole("textbox", { name: "Find a vault" }), "alpha");
    screen.getByRole("button", { name: "Remove alpha from favorites" }).focus();

    await user.keyboard("{Enter}");

    expect(screen.getByRole("button", { name: "Favorites" })).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("No vaults match these filters");
  });

  it("focuses the newly revealed favorite at the progressive display boundary", async () => {
    const user = userEvent.setup();
    const rows = Array.from({ length: 31 }, (_, index) => ({ id: `v-${index}`, name: `vault-${index}` }));
    renderDirectory(rows.map(row => row.id), rows);
    await user.click(screen.getByRole("button", { name: "Favorites" }));
    expect(screen.queryByRole("button", { name: "Remove vault-30 from favorites" })).not.toBeInTheDocument();
    screen.getByRole("button", { name: "Remove vault-29 from favorites" }).focus();

    await user.keyboard("{Enter}");

    expect(screen.getByRole("button", { name: "Remove vault-30 from favorites" })).toHaveFocus();
  });

  it("does not move focus to another control after a mouse removal", async () => {
    const user = userEvent.setup();
    renderDirectory(["v-alpha", "v-beta"]);
    await user.click(screen.getByRole("button", { name: "Favorites" }));

    await user.click(screen.getByRole("button", { name: "Remove alpha from favorites" }));

    expect(screen.queryByRole("link", { name: "alpha" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove beta from favorites" })).not.toHaveFocus();
    expect(screen.getByRole("button", { name: "Favorites" })).not.toHaveFocus();
  });

  it("preserves input focus when favorites change in another tab", async () => {
    const user = userEvent.setup();
    renderDirectory(["v-alpha", "v-beta"]);
    await user.click(screen.getByRole("button", { name: "Favorites" }));
    const input = screen.getByRole("textbox", { name: "Find a vault" });
    await user.click(input);

    act(() => {
      localStorage.setItem(KEY, JSON.stringify(["v-beta"]));
      window.dispatchEvent(new StorageEvent("storage", { key: KEY }));
    });

    expect(screen.queryByRole("link", { name: "alpha" })).not.toBeInTheDocument();
    expect(input).toHaveFocus();
  });

  it("retains the same control when keyboard removal keeps its row in All", async () => {
    const user = userEvent.setup();
    renderDirectory(["v-alpha", "v-beta"]);
    screen.getByRole("button", { name: "Remove alpha from favorites" }).focus();

    await user.keyboard("{Enter}");

    expect(screen.getByRole("button", { name: "Add alpha to favorites" })).toHaveFocus();
  });
});
