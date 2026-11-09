import { useRef, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { DocumentRenameDialog } from "@/components/document-rename-dialog";

vi.mock("@/lib/api", async original => ({
  ...await original<typeof import("@/lib/api")>(),
  getDocument: vi.fn(),
  updateDocument: vi.fn(),
}));
import { ApiError, getDocument, updateDocument } from "@/lib/api";

const read = vi.mocked(getDocument);
const update = vi.mocked(updateDocument);
const path = "guides/reading.md";
const snapshot = { path, title: "Reading guide", content: "Keep the complete body.", current_commit: "opaque-revision-current" };

function Harness({ onRenamed, onOpenChange }: { onRenamed: () => void; onOpenChange: (open: boolean) => void }) {
  const [open, setOpen] = useState(true);
  const trigger = useRef<HTMLButtonElement>(null);
  return <>
    <button ref={trigger}>Document actions</button>
    {open && <DocumentRenameDialog vault="fixture" path={path} returnFocusRef={trigger}
      onRenamed={onRenamed} onOpenChange={next => { onOpenChange(next); setOpen(next); }} />}
  </>;
}

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onRenamed = vi.fn();
  const onOpenChange = vi.fn();
  const view = (userId = "alice", revision = 0) => <QueryClientProvider client={client}>
    <CurrentUserProvider user={{ user_id: userId, username: userId, display_name: userId, email: `${userId}@example.com`, is_admin: false, auth_method: "local", key_class: null }} revision={revision}>
      <Harness onRenamed={onRenamed} onOpenChange={onOpenChange} />
    </CurrentUserProvider>
  </QueryClientProvider>;
  return { client, onRenamed, onOpenChange, view, ...render(view()) };
}

beforeEach(() => {
  read.mockReset().mockResolvedValue(snapshot);
  update.mockReset().mockResolvedValue({ current_commit: "opaque-revision-saved" });
});
afterEach(cleanup);

describe("document title rename", () => {
  it("saves only the title with the read revision, invalidates alias readers and restores focus", async () => {
    const user = userEvent.setup();
    const { client, onRenamed } = renderDialog();
    const aliasKey = ["document", "fixture", "d-alias", undefined, "alice", 0];
    client.setQueryData(aliasKey, snapshot);
    const field = await screen.findByRole("textbox", { name: "Title" });
    await waitFor(() => expect(field).toHaveFocus());
    expect(field).toHaveValue("Reading guide");
    expect((field as HTMLInputElement).selectionStart).toBe(0);
    expect((field as HTMLInputElement).selectionEnd).toBe("Reading guide".length);
    await user.clear(field);
    await user.type(field, "  Updated guide  ");
    await user.click(screen.getByRole("button", { name: "Save title" }));
    expect(update).toHaveBeenCalledExactlyOnceWith("fixture", path, {
      title: "Updated guide", expected_commit: "opaque-revision-current", title_conflict_policy: "reject",
    });
    expect(client.getQueryState(aliasKey)?.isInvalidated).toBe(true);
    expect(client.getQueryData(aliasKey)).toEqual(snapshot);
    expect(onRenamed).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.getByRole("button", { name: "Document actions" })).toHaveFocus());
  });

  it("requires a title and asks before using a duplicate title", async () => {
    const user = userEvent.setup();
    renderDialog();
    const field = await screen.findByRole("textbox", { name: "Title" });
    await user.clear(field);
    expect(screen.getByRole("button", { name: "Save title" })).toBeDisabled();
    await user.type(field, "Existing guide");
    update.mockRejectedValueOnce(new ApiError("Duplicate title", 409, {
      code: "document_title_conflict",
      details: { title: "Existing guide", collection: "guides", existing_path: "guides/other.md", existing_title: "Existing guide" },
    }));
    await user.click(screen.getByRole("button", { name: "Save title" }));
    await screen.findByText("“Existing guide” already exists here");
    expect(field).toHaveValue("Existing guide");
    await user.click(screen.getByRole("button", { name: "Use this title" }));
    expect(update).toHaveBeenLastCalledWith("fixture", path, {
      title: "Existing guide", expected_commit: "opaque-revision-current", title_conflict_policy: "allow",
    });
  });

  it("retains the draft after a concurrent edit and reloads a fresh revision on request", async () => {
    const user = userEvent.setup();
    renderDialog();
    const field = await screen.findByRole("textbox", { name: "Title" });
    await user.clear(field);
    await user.type(field, "My title");
    update.mockRejectedValueOnce(new ApiError("Revision conflict", 409, { code: "conflict" }));
    await user.click(screen.getByRole("button", { name: "Save title" }));
    await screen.findByText(/This document changed while you were renaming it/);
    expect(field).toHaveValue("My title");
    read.mockResolvedValueOnce({ ...snapshot, title: "Another writer's title", current_commit: "opaque-revision-newer" });
    await user.click(screen.getByRole("button", { name: "Reload current title" }));
    const refreshed = await screen.findByRole("textbox", { name: "Title" });
    await waitFor(() => expect(refreshed).toHaveValue("Another writer's title"));
    await user.clear(refreshed);
    await user.type(refreshed, "Reviewed title");
    await user.click(screen.getByRole("button", { name: "Save title" }));
    expect(update).toHaveBeenLastCalledWith("fixture", path, {
      title: "Reviewed title", expected_commit: "opaque-revision-newer", title_conflict_policy: "reject",
    });
  });

  it("closes on an account switch and ignores a late mutation callback", async () => {
    const user = userEvent.setup();
    let finish!: () => void;
    update.mockImplementationOnce(() => new Promise(resolve => { finish = () => resolve({ current_commit: "opaque-revision-saved" }); }));
    const { rerender, view, onRenamed, onOpenChange } = renderDialog();
    const field = await screen.findByRole("textbox", { name: "Title" });
    await user.clear(field);
    await user.type(field, "Alice's title");
    await user.click(screen.getByRole("button", { name: "Save title" }));
    rerender(view("bob", 1));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    finish();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(onRenamed).not.toHaveBeenCalled();
  });
});
