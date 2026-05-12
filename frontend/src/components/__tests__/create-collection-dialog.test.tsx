import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CreateCollectionDialog } from "@/components/create-collection-dialog";

vi.mock("@/lib/api", () => ({
  createCollection: vi.fn(),
}));

import { createCollection } from "@/lib/api";
const createMock = createCollection as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  createMock.mockReset();
});

afterEach(() => cleanup());

describe("CreateCollectionDialog", () => {
  it("blocks submit when path contains '..' segment and does not call API", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onCreated = vi.fn();
    render(
      <CreateCollectionDialog
        vault="v"
        open
        onOpenChange={onOpenChange}
        onCreated={onCreated}
      />,
    );

    await user.type(screen.getByLabelText(/path/i), "../bad");
    await user.click(screen.getByRole("button", { name: /create/i }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(createMock).not.toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("blocks submit when path is only whitespace", async () => {
    const user = userEvent.setup();
    render(
      <CreateCollectionDialog
        vault="v"
        open
        onOpenChange={() => {}}
        onCreated={() => {}}
      />,
    );

    await user.type(screen.getByLabelText(/path/i), "   ");
    await user.click(screen.getByRole("button", { name: /create/i }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(createMock).not.toHaveBeenCalled();
  });

  it("trims trailing slash before calling createCollection", async () => {
    createMock.mockResolvedValue({
      ok: true,
      created: true,
      collection: { path: "specs", name: "specs", summary: null, doc_count: 0 },
    });
    const user = userEvent.setup();
    render(
      <CreateCollectionDialog
        vault="v"
        open
        onOpenChange={() => {}}
        onCreated={() => {}}
      />,
    );

    await user.type(screen.getByLabelText(/path/i), "specs/");
    await user.click(screen.getByRole("button", { name: /create/i }));

    await waitFor(() => expect(createMock).toHaveBeenCalled());
    expect(createMock).toHaveBeenCalledWith("v", "specs", undefined);
  });

  it("calls onCreated and closes when created=true", async () => {
    createMock.mockResolvedValue({
      ok: true,
      created: true,
      collection: { path: "new", name: "new", summary: null, doc_count: 0 },
    });
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onCreated = vi.fn();
    render(
      <CreateCollectionDialog
        vault="v"
        open
        onOpenChange={onOpenChange}
        onCreated={onCreated}
      />,
    );

    await user.type(screen.getByLabelText(/path/i), "new");
    await user.click(screen.getByRole("button", { name: /create/i }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("new"));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("shows 'already exists' and stays open when created=false", async () => {
    createMock.mockResolvedValue({
      ok: true,
      created: false,
      collection: { path: "x", name: "x", summary: null, doc_count: 0 },
    });
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onCreated = vi.fn();
    render(
      <CreateCollectionDialog
        vault="v"
        open
        onOpenChange={onOpenChange}
        onCreated={onCreated}
      />,
    );

    await user.type(screen.getByLabelText(/path/i), "x");
    await user.click(screen.getByRole("button", { name: /create/i }));

    expect(await screen.findByText(/already exists/i)).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("prefills the path input from initialPath with a trailing slash and submits the joined leaf", async () => {
    createMock.mockResolvedValue({
      ok: true,
      created: true,
      collection: { path: "parent/leaf", name: "leaf", summary: null, doc_count: 0 },
    });
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onCreated = vi.fn();
    render(
      <CreateCollectionDialog
        vault="v"
        open
        initialPath="parent"
        onOpenChange={onOpenChange}
        onCreated={onCreated}
      />,
    );

    const input = screen.getByLabelText(/path/i) as HTMLInputElement;
    // Prefilled with parent + trailing slash so the user can type the
    // leaf name directly.
    expect(input.value).toBe("parent/");

    // Append the leaf name. fireEvent.change is used instead of
    // userEvent.type because the input's controlled state means
    // userEvent.type would interact with the autoFocus selection
    // behavior in unpredictable ways across jsdom versions.
    fireEvent.change(input, { target: { value: "parent/leaf" } });
    await user.click(screen.getByRole("button", { name: /create/i }));

    await waitFor(() => expect(createMock).toHaveBeenCalled());
    expect(createMock).toHaveBeenCalledWith("v", "parent/leaf", undefined);
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("parent/leaf"));
  });

  it("shows inline error when the API rejects", async () => {
    createMock.mockRejectedValue(new Error("boom"));
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(
      <CreateCollectionDialog
        vault="v"
        open
        onOpenChange={onOpenChange}
        onCreated={() => {}}
      />,
    );

    await user.type(screen.getByLabelText(/path/i), "ok");
    await user.click(screen.getByRole("button", { name: /create/i }));

    expect(await screen.findByText(/boom/)).toBeInTheDocument();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
