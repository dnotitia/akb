import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { AppPageLocation } from "../app-page-location";

describe("App page location", () => {
  it.each([
    ["/", "Home"], ["/search?q=test", "Search"],
    ["/settings?tab=tokens", "Agent connections"], ["/vault", "Vaults"],
    ["/settings", "Profile"], ["/settings?tab=preferences", "Appearance"],
    ["/settings?tab=notifications", "Watched documents"],
    ["/settings?tab=admin", "Profile"], ["/settings?tab=unknown", "Profile"],
    ["/vault/new", "New vault"], ["/notifications", "Notifications"],
    ["/unknown", "Page not found"],
  ])("identifies %s without an unnecessary hierarchy", (path, label) => {
    render(<MemoryRouter initialEntries={[path]}><AppPageLocation /></MemoryRouter>);
    const location = screen.getByRole("navigation", { name: "Current page" });
    expect(within(location).getByText(label)).toHaveAttribute("aria-current", "page");
    expect(location.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(within(location).queryByRole("link")).not.toBeInTheDocument();
  });

  it.each([
    ["", "Overview"], ["/members", "Members"], ["/settings", "Settings"],
    ["/publications", "Publish"], ["/graph", "Graph"], ["/search", "Search"],
    ["/activity", "Activity"], ["/doc/new", "New document"],
    ["/doc/d-12345678", "Document"], ["/table/data", "Table"], ["/file/uuid", "File"],
  ])("labels Vault section %s and links back without exposing IDs", (tail, label) => {
    render(<MemoryRouter initialEntries={[`/vault/%ED%8C%80%20Vault${tail}`]}><AppPageLocation /></MemoryRouter>);
    const location = screen.getByRole("navigation", { name: "Current page" });
    expect(within(location).getByRole("link", { name: "팀 Vault" })).toHaveAttribute("href", "/vault/%ED%8C%80%20Vault");
    expect(within(location).getByText(label)).toHaveAttribute("aria-current", "page");
    expect(location).not.toHaveTextContent("d-12345678");
  });
});
