import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SearchVaultPicker } from "../search-vault-picker";

afterEach(cleanup);

const selectedVaults = Array.from({ length: 40 }, (_, index) => `team-${index + 1}-production-knowledge-base`);

function Picker({ available = selectedVaults, initialSelected = selectedVaults, variant }: {
  available?: string[] | null;
  initialSelected?: string[];
  variant?: "chips" | "summary";
}) {
  const [selected, setSelected] = useState(initialSelected);
  return <>
    <SearchVaultPicker variant={variant} selected={selected} vaults={available} error={available === null} onRetry={vi.fn()} onChange={setSelected} />
    <output aria-label="Selected vault count">{selected.length}</output>
  </>;
}

describe("compact Vault search scope", () => {
  it("keeps a bounded chip preview while all selected Vaults remain removable", async () => {
    const user = userEvent.setup();
    render(<Picker />);
    const scope = screen.getByRole("group", { name: "Vault search scope" });
    expect(within(scope).getAllByRole("button", { name: /^Remove .* from search scope$/ })).toHaveLength(2);
    expect(within(scope).getByText("+38")).toBeInTheDocument();
    await user.click(within(scope).getByRole("button", { name: /^Search scope:/ }));
    expect(screen.getAllByRole("menuitemcheckbox", { checked: true })).toHaveLength(40);
    await user.type(screen.getByRole("searchbox", { name: "Filter vaults" }), "team-40");
    await user.click(screen.getByRole("menuitemcheckbox", { name: selectedVaults[39] }));
    expect(screen.getByLabelText("Selected vault count")).toHaveTextContent("39");
    await user.keyboard("{Escape}");
    expect(within(scope).getByText("+37")).toBeInTheDocument();
  });

  it("keeps hidden selected scopes removable while the Vault directory is unavailable", async () => {
    const user = userEvent.setup();
    render(<Picker available={null} />);
    await user.click(screen.getByRole("button", { name: /^Search scope:/ }));
    expect(screen.getAllByRole("menuitemcheckbox", { checked: true })).toHaveLength(40);
    await user.type(screen.getByRole("searchbox", { name: "Filter vaults" }), "team-40");
    await user.click(screen.getByRole("menuitemcheckbox", { name: selectedVaults[39] }));
    expect(screen.getByLabelText("Selected vault count")).toHaveTextContent("39");
  });
});


describe("summary Vault search scope", () => {
  it.each([
    { selected: [], label: "All vaults" },
    { selected: ["alpha"], label: "alpha" },
    { selected: ["alpha", "beta"], label: "2 vaults" },
    { selected: selectedVaults, label: "40 vaults" },
  ])("shows $label as one scope trigger", ({ selected, label }) => {
    render(<Picker variant="summary" initialSelected={selected} />);
    const scope = screen.getByRole("group", { name: "Vault search scope" });
    expect(within(scope).getAllByRole("button")).toHaveLength(1);
    expect(within(scope).getByRole("button", { name: `Search scope: ${label}` })).toHaveTextContent(label);
  });

  it("selects and deselects Vaults through the summary checklist", async () => {
    const user = userEvent.setup();
    render(<Picker variant="summary" available={["alpha", "beta"]} initialSelected={[]} />);
    await user.click(screen.getByRole("button", { name: "Search scope: All vaults" }));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "alpha", checked: false }));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "beta", checked: false }));
    expect(screen.getByRole("menuitemcheckbox", { name: "alpha", checked: true })).toBeInTheDocument();
    expect(screen.getByRole("menuitemcheckbox", { name: "beta", checked: true })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    const trigger = screen.getByRole("button", { name: "Search scope: 2 vaults" });
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    await user.click(screen.getByRole("menuitemcheckbox", { name: "alpha", checked: true }));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "Search scope: beta" })).toHaveTextContent("beta");
    expect(screen.getByLabelText("Selected vault count")).toHaveTextContent("1");
  });

  it.each([{ available: null }, { available: ["alpha", "beta"] }])(
    "keeps stale selected Vaults removable when the directory is $available",
    async ({ available }) => {
      const user = userEvent.setup();
      render(<Picker variant="summary" available={available} initialSelected={["stale-vault", "alpha"]} />);
      await user.click(screen.getByRole("button", { name: "Search scope: 2 vaults" }));
      expect(screen.getByRole("menuitemcheckbox", { name: "alpha", checked: true })).toBeInTheDocument();
      await user.click(screen.getByRole("menuitemcheckbox", { name: "stale-vault", checked: true }));
      await user.keyboard("{Escape}");
      expect(screen.getByRole("button", { name: "Search scope: alpha" })).toBeInTheDocument();
      expect(screen.getByLabelText("Selected vault count")).toHaveTextContent("1");
    },
  );
});
