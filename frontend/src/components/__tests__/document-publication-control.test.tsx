import { createRef } from "react";
import { MemoryRouter } from "react-router-dom";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DocumentPublicationControl } from "@/components/document-publication-control";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderControl(props: Partial<React.ComponentProps<typeof DocumentPublicationControl>> = {}) {
  const onPublish = vi.fn();
  const onUnpublish = vi.fn().mockResolvedValue(undefined);
  const view = render(
    <MemoryRouter>
      <DocumentPublicationControl vault="demo" onPublish={onPublish} onUnpublish={onUnpublish} {...props} />
    </MemoryRouter>,
  );
  return { ...view, onPublish, onUnpublish };
}

describe("DocumentPublicationControl", () => {
  it("opens publication options from the visible toolbar action without mutating", async () => {
    const user = userEvent.setup();
    const triggerRef = createRef<HTMLButtonElement>();
    const { onPublish, onUnpublish } = renderControl({ triggerRef });
    const trigger = screen.getByRole("button", { name: "Publish" });

    expect(trigger).toHaveAttribute("data-reader-control");
    expect(triggerRef.current).toBe(trigger);
    await user.click(trigger);

    expect(onPublish).toHaveBeenCalledOnce();
    expect(onUnpublish).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps the reason for unavailable publishing keyboard accessible", async () => {
    const user = userEvent.setup();
    const { onPublish } = renderControl({ disabledReason: "Historical versions cannot be published." });
    const trigger = screen.getByRole("button", { name: "Publish" });

    await user.tab();
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-disabled", "true");
    expect(trigger).toHaveAccessibleDescription("Historical versions cannot be published.");
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Historical versions cannot be published.");
    await user.keyboard("{Enter} ");
    await user.click(trigger);
    expect(onPublish).not.toHaveBeenCalled();
  });

  it("discloses the same-origin URL and management route, then restores trigger focus", async () => {
    const user = userEvent.setup();
    renderControl({ vault: "team notes", publicSlug: "guide-public" });
    const trigger = screen.getByRole("button", { name: "Public link" });
    await user.click(trigger);

    const panel = screen.getByRole("dialog", { name: "Public link" });
    const publicUrl = `${window.location.origin}/p/guide-public`;
    expect(within(panel).getByRole("textbox", { name: "Public URL" })).toHaveValue(publicUrl);
    expect(within(panel).getByRole("link", { name: "Open link" })).toHaveAttribute("href", publicUrl);
    expect(within(panel).getByRole("link", { name: "Open link" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(panel).getByRole("link", { name: "Manage links and access limits" }))
      .toHaveAttribute("href", "/vault/team%20notes/publications");
    expect(panel).toHaveAccessibleDescription(/Access depends on the link’s limits/);
    expect(trigger).toHaveAttribute("aria-expanded", "true");

    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("announces successful copying", async () => {
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    renderControl({ publicSlug: "guide-public" });
    await user.click(screen.getByRole("button", { name: "Public link" }));
    await user.click(screen.getByRole("button", { name: "Copy link" }));

    expect(copy).toHaveBeenCalledWith(`${window.location.origin}/p/guide-public`);
    expect(await screen.findByRole("status")).toHaveTextContent("Link copied.");
  });

  it.each(["unavailable", "rejected"])("offers selected manual-copy recovery when clipboard is %s", async (failure) => {
    const user = userEvent.setup();
    if (failure === "unavailable") vi.spyOn(navigator, "clipboard", "get").mockReturnValue(undefined as unknown as Clipboard);
    else vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("Clipboard permission denied"));
    renderControl({ publicSlug: "guide-public" });
    await user.click(screen.getByRole("button", { name: "Public link" }));
    await user.click(screen.getByRole("button", { name: "Copy link" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("copy it manually");
    expect(screen.queryByText("Link copied.")).not.toBeInTheDocument();
    const url = screen.getByRole("textbox", { name: "Public URL" }) as HTMLTextAreaElement;
    expect(url).toHaveFocus();
    expect(url.selectionStart).toBe(0);
    expect(url.selectionEnd).toBe(url.value.length);
  });

  it("confirms all document links, allows cancellation, and restores focus", async () => {
    const user = userEvent.setup();
    const { onUnpublish } = renderControl({ publicSlug: "guide-public" });
    await user.click(screen.getByRole("button", { name: "Public link" }));
    const unpublish = screen.getByRole("button", { name: "Unpublish" });
    await user.click(unpublish);

    const confirmation = screen.getByRole("dialog", { name: "Unpublish this document?" });
    expect(confirmation).toHaveAccessibleDescription("All public links to this document will stop working. The document will remain in this Vault.");
    expect(onUnpublish).not.toHaveBeenCalled();
    expect(within(confirmation).getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(within(confirmation).getByRole("button", { name: "Cancel" }));

    expect(onUnpublish).not.toHaveBeenCalled();
    await waitFor(() => expect(unpublish).toHaveFocus());
    expect(screen.getByRole("dialog", { name: "Public link" })).toBeInTheDocument();
  });

  it("retains an unpublish error for retry and closes both dialogs after success", async () => {
    const user = userEvent.setup();
    const onUnpublish = vi.fn().mockRejectedValueOnce(new Error("Network unavailable")).mockResolvedValueOnce(undefined);
    renderControl({ publicSlug: "guide-public", onUnpublish });
    const trigger = screen.getByRole("button", { name: "Public link" });
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Unpublish" }));
    const confirmation = screen.getByRole("dialog", { name: "Unpublish this document?" });
    await user.click(within(confirmation).getByRole("button", { name: "Unpublish" }));

    expect(await within(confirmation).findByRole("alert")).toHaveTextContent("Network unavailable");
    await user.click(within(confirmation).getByRole("button", { name: "Unpublish" }));

    expect(onUnpublish).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("keeps existing links inspectable while unavailable unpublishing is guarded", async () => {
    const user = userEvent.setup();
    const { onUnpublish } = renderControl({ publicSlug: "guide-public", disabledReason: "This Vault is read-only." });
    await user.click(screen.getByRole("button", { name: "Public link" }));
    const unpublish = screen.getByRole("button", { name: "Unpublish" });

    expect(screen.getByRole("textbox", { name: "Public URL" })).toBeInTheDocument();
    expect(unpublish).toHaveAttribute("aria-disabled", "true");
    expect(unpublish).toHaveAccessibleDescription("This Vault is read-only.");
    await user.click(unpublish);
    expect(screen.queryByRole("dialog", { name: "Unpublish this document?" })).not.toBeInTheDocument();
    expect(onUnpublish).not.toHaveBeenCalled();
  });

  it("blocks repeated confirmations while unpublishing is pending", async () => {
    const user = userEvent.setup();
    let resolve!: () => void;
    const onUnpublish = vi.fn(() => new Promise<void>((done) => { resolve = done; }));
    renderControl({ publicSlug: "guide-public", onUnpublish });
    await user.click(screen.getByRole("button", { name: "Public link" }));
    await user.click(screen.getByRole("button", { name: "Unpublish" }));
    const confirmation = screen.getByRole("dialog", { name: "Unpublish this document?" });
    await user.click(within(confirmation).getByRole("button", { name: "Unpublish" }));

    expect(within(confirmation).getByRole("button", { name: "Working…" })).toBeDisabled();
    expect(within(confirmation).getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(confirmation).toBeInTheDocument();
    expect(onUnpublish).toHaveBeenCalledOnce();
    await act(async () => resolve());
  });

  it("rechecks a changed permission before submitting an open confirmation", async () => {
    const user = userEvent.setup();
    const onUnpublish = vi.fn().mockResolvedValue(undefined);
    const onPublish = vi.fn();
    const { rerender } = renderControl({ publicSlug: "guide-public", onUnpublish, onPublish });
    await user.click(screen.getByRole("button", { name: "Public link" }));
    await user.click(screen.getByRole("button", { name: "Unpublish" }));
    rerender(
      <MemoryRouter>
        <DocumentPublicationControl
          vault="demo"
          publicSlug="guide-public"
          disabledReason="Your write access was removed."
          onPublish={onPublish}
          onUnpublish={onUnpublish}
        />
      </MemoryRouter>,
    );
    const confirmation = screen.getByRole("dialog", { name: "Unpublish this document?" });
    await user.click(within(confirmation).getByRole("button", { name: "Unpublish" }));

    expect(await within(confirmation).findByRole("alert")).toHaveTextContent("Your write access was removed.");
    expect(onUnpublish).not.toHaveBeenCalled();
  });
});
