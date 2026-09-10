import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { WorkspacePersonalSections } from "@/components/workspace-personal-sections";
import { recordRecentDocumentView } from "@/lib/recent-document-views";
import { saveDocumentDraft } from "@/lib/document-draft";
import { toggleWorkspaceShortcut } from "@/lib/workspace-shortcuts";

beforeEach(() => localStorage.clear());
const vaults = [{ name: "team", role: "writer" }];
function mount() { return render(<MemoryRouter><WorkspacePersonalSections userId="me" vaults={vaults} /></MemoryRouter>); }

describe("Workspace personal sections", () => {
  it("shows account-local recent links only for accessible Vaults, collapsed by default", async () => {
    recordRecentDocumentView("me", { vault: "team", path: "notes/a.md", title: "Viewed document" });
    recordRecentDocumentView("me", { vault: "revoked", path: "secret.md", title: "Hidden document" });
    recordRecentDocumentView("other", { vault: "team", path: "other.md", title: "Other account" });
    mount();
    expect(screen.getByText("Recently viewed", { selector: "summary" }).closest("details")).not.toHaveAttribute("open");
    await userEvent.click(screen.getByText("Recently viewed", { selector: "summary" }));
    expect(screen.getByRole("link", { name: "Viewed document" })).toHaveAttribute("href", "/vault/team/doc/notes%2Fa.md");
    expect(screen.queryByText("Viewed in this browser")).not.toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Recently viewed" })).not.toHaveAttribute("aria-describedby");
    expect(screen.getByRole("link", { name: "Viewed document" })).toHaveClass("pl-6");
    expect(screen.queryByText("Hidden document")).not.toBeInTheDocument();
    expect(screen.queryByText("Other account")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove Viewed document from recently viewed" }));
    expect(screen.queryByRole("link", { name: "Viewed document" })).not.toBeInTheDocument();
  });

  it("provides document and Collection pins with different destinations", async () => {
    mount();
    act(() => {
      toggleWorkspaceShortcut("me", { kind: "document", vault: "team", path: "a.md", title: "Pinned document" });
      toggleWorkspaceShortcut("me", { kind: "collection", vault: "team", path: "guides/onboarding", title: "Onboarding" });
    });
    await userEvent.click(screen.getByText("Pinned", { selector: "summary" }));
    expect(screen.getByRole("link", { name: "Pinned document" })).toHaveAttribute("href", "/vault/team/doc/a.md");
    expect(screen.getByRole("link", { name: "Onboarding" })).toHaveAttribute("href", "/vault/team?collection=guides%2Fonboarding");
    await userEvent.click(screen.getByRole("button", { name: "Remove Pinned document from pinned" }));
    expect(screen.queryByRole("link", { name: "Pinned document" })).not.toBeInTheDocument();
  });

  it("links a recoverable new draft to the composer and updates live", async () => {
    mount();
    act(() => { saveDocumentDraft({ userId: "me", vault: "team", collection: "guides", title: "Unpublished work", body: "Draft body", type: "note", domain: "", summary: "", tags: [], assetIds: [] }); });
    await userEvent.click(screen.getByText("Drafts", { selector: "summary" }));
    expect(screen.getByRole("link", { name: "Unpublished work" })).toHaveAttribute("href", "/vault/team/doc/new");
    expect(screen.queryByText("Draft body")).not.toBeInTheDocument();
  });
});
