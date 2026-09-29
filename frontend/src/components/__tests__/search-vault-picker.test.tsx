import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SearchVaultPicker } from "../search-vault-picker";

afterEach(cleanup);

const selectedVaults = Array.from({ length: 40 }, (_, index) => `team-${index + 1}-production-knowledge-base`);

function Picker({ available = selectedVaults }: { available?: string[] | null }) {
  const [selected, setSelected] = useState(selectedVaults);
  return <>
    <SearchVaultPicker selected={selected} vaults={available} error={available === null} onRetry={vi.fn()} onChange={setSelected} />
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
