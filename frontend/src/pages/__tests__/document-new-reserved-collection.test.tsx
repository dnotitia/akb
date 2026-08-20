import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import DocumentNewPage from "../document-new";

const putDocument = vi.fn();

vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {},
  putDocument: (...args: unknown[]) => putDocument(...args),
}));

vi.mock("@/hooks/use-vault-tree", () => ({
  useVaultTree: () => ({ tree: [] }),
}));

vi.mock("@/contexts/vault-refresh-context", () => ({
  useVaultRefresh: () => ({ refetchTree: vi.fn(), refetchVaults: vi.fn() }),
}));

vi.mock("@/components/markdown-editor", () => ({
  default: ({ onChange }: { onChange: (body: string, ids: string[]) => void }) => (
    <textarea
      aria-label="Document body"
      onChange={(event) => onChange(event.target.value, [])}
    />
  ),
}));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/vault/my-v/doc/new?collection=overview"]}>
      <Routes>
        <Route path="/vault/:name/doc/new" element={<DocumentNewPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DocumentNewPage reserved collection feedback", () => {
  it("rejects overview before submit and enables create after a valid replacement", async () => {
    const user = userEvent.setup();
    renderPage();

    const collection = screen.getByLabelText(/^collection/i);
    const create = screen.getByRole("button", { name: /create document/i });
    expect(await screen.findByText(/system collection reserved/i)).toBeInTheDocument();
    expect(collection).toHaveAttribute("aria-invalid", "true");
    expect(create).toBeDisabled();
    expect(putDocument).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText(/^title/i), "A note");
    await user.type(await screen.findByLabelText(/document body/i), "Body");
    await user.clear(collection);
    await user.type(collection, "notes");

    expect(screen.queryByText(/system collection reserved/i)).toBeNull();
    expect(screen.getByText(/new collection/i)).toBeInTheDocument();
    expect(collection).not.toHaveAttribute("aria-invalid");
    expect(create).toBeEnabled();
  });
});
