import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SkillSettingsLink } from "../skill-settings-link";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("SkillSettingsLink", () => {
  it("defined: shows Configure button linking to skill page", () => {
    render(wrap(<SkillSettingsLink vault="my-v" defined updatedAt="2026-05-18T10:00:00Z" />));
    expect(screen.getByText(/defined/i)).toBeTruthy();
    expect(screen.getByRole("link", { name: /configure/i }).getAttribute("href")).toContain("/vault/my-v/skill");
  });

  it("undefined: shows Create from template button", () => {
    render(wrap(<SkillSettingsLink vault="my-v" defined={false} />));
    expect(screen.getByRole("button", { name: /create from template/i })).toBeTruthy();
  });
});
