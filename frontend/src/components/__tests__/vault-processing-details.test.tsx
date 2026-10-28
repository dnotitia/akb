import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { VaultProcessingDetails } from "../vault-processing-details";
import { normalizeIndexingResponse, summarizeIndexing } from "@/lib/indexing-status";
import type { useSearchStatus } from "@/hooks/use-search-status";

let context: ReturnType<typeof useSearchStatus>;
vi.mock("@/hooks/use-search-status", () => ({ useOptionalSearchStatus: () => context }));

function row(name: string, pending = 0) {
  return normalizeIndexingResponse({ search_update_status: { version: 1, stages: {
    search_index: { mode: "enabled", unit: "chunks", scope: "stored_chunks", pending, retrying: 0, abandoned: 0 },
    file_projection: { mode: "enabled", unit: "file_updates", scope: "latest_file_intents", pending: 0, retrying: 0, exhausted: 0, abandoned: 0 },
    content_preparation: { mode: "enabled", unit: "revision_updates", scope: "current_heads", pending: 0, retrying: 0, exhausted: 0, abandoned: 0 },
    metadata: { mode: "enabled", unit: "documents", scope: "external_git_documents", pending: 0, retrying: 0, abandoned: 0 },
  } } }, { id: name, name }, Date.now());
}

beforeEach(() => {
  context = { summary: summarizeIndexing([], { verified: true, loading: false }), observations: [], verified: true, loading: false, refresh: vi.fn() };
});

describe("VaultProcessingDetails", () => {
  it("hides verified idle work and opens only the current Vault's queued details", async () => {
    const user = userEvent.setup();
    context.observations = [row("alpha"), row("beta", 17)];
    const view = render(<VaultProcessingDetails vaultName="alpha" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    context.observations = [row("alpha", 3), row("beta", 17)];
    view.rerender(<VaultProcessingDetails vaultName="alpha" />);
    await user.click(screen.getByRole("button", { name: "3 indexing in alpha" }));
    expect(screen.getByRole("dialog", { name: "Search updates · alpha" })).toHaveTextContent("3 chunks pending");
    expect(screen.getByRole("dialog")).not.toHaveTextContent("17");
    expect(screen.queryByText("Indexing details")).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "3 indexing in alpha" })).toHaveFocus();
  });

  it("does not expose cached private observations while verifying access", async () => {
    const user = userEvent.setup();
    context.observations = [row("alpha", 99)];
    context.verified = false;
    render(<VaultProcessingDetails vaultName="alpha" />);
    await user.click(screen.getByRole("button", { name: "Status unavailable in alpha" }));
    expect(screen.getByRole("dialog")).not.toHaveTextContent("99");
    expect(screen.getByRole("dialog")).toHaveTextContent("has not been verified");
  });

  it("keeps missing, stale and unverified observations distinct from idle", () => {
    const view = render(<VaultProcessingDetails vaultName="alpha" />);
    expect(screen.getByRole("button", { name: "Status unavailable in alpha" })).toBeInTheDocument();
    context.observations = [{ ...row("alpha", 99), observation: "stale" }];
    view.rerender(<VaultProcessingDetails vaultName="alpha" />);
    expect(screen.queryByText(/99/)).not.toBeInTheDocument();
    context.observations = [row("alpha")];
    context.verified = false;
    view.rerender(<VaultProcessingDetails vaultName="alpha" />);
    expect(screen.getByRole("button", { name: "Status unavailable in alpha" })).toBeInTheDocument();
  });

  it("keeps failures and paused work visible even without a pending count", () => {
    const failed = row("alpha");
    failed.stages[0].state = "attention";
    failed.stages[0].abandoned = 1;
    context.observations = [failed];
    const view = render(<VaultProcessingDetails vaultName="alpha" />);
    expect(screen.getByRole("button", { name: "Updates need attention in alpha" })).toBeInTheDocument();
    const paused = row("alpha", 5);
    paused.stages[0].mode = "disabled";
    paused.stages[0].state = "paused";
    context.observations = [paused];
    view.rerender(<VaultProcessingDetails vaultName="alpha" />);
    expect(screen.getByRole("button", { name: "Updates paused in alpha" })).toBeInTheDocument();
    expect(screen.queryByText(/5 indexing/)).not.toBeInTheDocument();
  });
});
