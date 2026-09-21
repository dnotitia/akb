import { createRef, useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentPublicationControl } from "@/components/document-publication-control";
import { createPublication, getDocument, listPublications, type Publication } from "@/lib/api";

vi.mock("@/lib/api", () => ({ createPublication: vi.fn(), getDocument: vi.fn(), listPublications: vi.fn() }));

const publication: Publication = {
  slug: "new-public-link", resource_type: "document", resource_uri: "akb://demo/coll/docs/doc/guide.md",
  share_url: "http://localhost:3000/p/new-public-link", vault: "demo", title: "Guide", mode: "live",
  expires_at: null, max_views: null, view_count: 0, allow_embed: true, section_filter: null,
  password_protected: false, created_at: "2026-09-21T00:00:00Z", snapshot_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getDocument).mockResolvedValue({ uri: publication.resource_uri, title: "Guide", path: "docs/guide.md" } as Awaited<ReturnType<typeof getDocument>>);
  vi.mocked(listPublications).mockResolvedValue({ publications: [] });
  vi.mocked(createPublication).mockResolvedValue(publication);
});

function PublicationHarness(props: React.ComponentProps<typeof DocumentPublicationControl>) {
  const [slug, setSlug] = useState(props.publicSlug);
  return <DocumentPublicationControl {...props} publicSlug={slug} onPublished={(next, publication) => {
    setSlug(next);
    props.onPublished(next, publication);
  }} />;
}

function ArticleAction() {
  const [count, setCount] = useState(0);
  return <button type="button" onClick={() => setCount(value => value + 1)}>Article action {count}</button>;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderControl(props: Partial<React.ComponentProps<typeof DocumentPublicationControl>> = {}) {
  const onPublished = vi.fn();
  const onUnpublish = vi.fn().mockResolvedValue(undefined);
  const view = render(
    <MemoryRouter>
      <PublicationHarness vault="demo" docId="docs/guide.md" onPublished={onPublished} onUnpublish={onUnpublish} {...props} />
      <ArticleAction />
    </MemoryRouter>,
  );
  return { ...view, onPublished, onUnpublish };
}

describe("DocumentPublicationControl", () => {
  it("opens publication options from the visible toolbar action without mutating", async () => {
    const user = userEvent.setup();
    const triggerRef = createRef<HTMLButtonElement>();
    const { onPublished, onUnpublish } = renderControl({ triggerRef });
    const trigger = screen.getByRole("button", { name: "Publish" });

    expect(trigger).toHaveAttribute("data-reader-control");
    expect(triggerRef.current).toBe(trigger);
    await user.click(trigger);

    expect(onPublished).not.toHaveBeenCalled();
    expect(onUnpublish).not.toHaveBeenCalled();
    expect(createPublication).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Publish document" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Article action 0" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Article action 1" })).toHaveFocus();
  });

  it("keeps the reason for unavailable publishing keyboard accessible", async () => {
    const user = userEvent.setup();
    const { onPublished } = renderControl({ disabledReason: "Historical versions cannot be published." });
    const trigger = screen.getByRole("button", { name: "Publish" });

    await user.tab();
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-disabled", "true");
    expect(trigger).toHaveAccessibleDescription("Historical versions cannot be published.");
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Historical versions cannot be published.");
    await user.keyboard("{Enter} ");
    await user.click(trigger);
    expect(onPublished).not.toHaveBeenCalled();
    expect(createPublication).not.toHaveBeenCalled();
  });

  it("validates access limits and transitions to the ready link in the same popover", async () => {
    const user = userEvent.setup();
    const { onPublished } = renderControl();
    await user.click(screen.getByRole("button", { name: "Publish" }));
    const form = screen.getByRole("dialog", { name: "Publish document" });
    const submit = within(form).getByRole("button", { name: "Publish" });
    await user.type(within(form).getByLabelText("Max views"), "0");
    expect(submit).toBeDisabled();
    await user.clear(within(form).getByLabelText("Max views"));
    await user.type(within(form).getByLabelText("Max views"), "25");
    await user.click(within(form).getByRole("checkbox", { name: /Require password/ }));
    expect(submit).toBeDisabled();
    await user.type(within(form).getByLabelText("Publication password"), "test-passphrase");
    await user.click(within(form).getByRole("button", { name: "7 days" }));
    await user.click(submit);

    const ready = await screen.findByRole("dialog", { name: "Public link" });
    expect(createPublication).toHaveBeenCalledWith("demo", {
      resource_type: "document", uri: publication.resource_uri, title: "Guide",
      password: "test-passphrase", expires_in: "7d", max_views: 25, // pragma: allowlist secret — synthetic publication fixture
    });
    expect(onPublished).toHaveBeenCalledWith("new-public-link", publication);
    const url = within(ready).getByRole("textbox", { name: "Public URL" });
    expect(url).toHaveValue(`${window.location.origin}/p/new-public-link`);
    await waitFor(() => expect(url).toHaveFocus());
    expect(within(ready).getByRole("button", { name: "Copy link" })).toBeVisible();
  });

  it("keeps pending publication open and preserves options for retry on failure", async () => {
    const user = userEvent.setup();
    let reject!: (error: Error) => void;
    vi.mocked(createPublication).mockImplementationOnce(() => new Promise((_, fail) => { reject = fail; }));
    renderControl();
    const trigger = screen.getByRole("button", { name: "Publish" });
    await user.click(trigger);
    const panel = screen.getByRole("dialog", { name: "Publish document" });
    await user.type(within(panel).getByLabelText("Max views"), "12");
    await user.click(within(panel).getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(createPublication).toHaveBeenCalledOnce());
    expect(within(panel).getByRole("button", { name: "Publishing…" })).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "Close publish options" })).toBeDisabled();
    await user.keyboard("{Escape}");
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Article action 0" }));
    expect(panel).toBeVisible();
    await act(async () => reject(new Error("Publication service unavailable.")));
    expect(within(panel).getByRole("alert")).toHaveTextContent("Publication service unavailable.");
    expect(within(panel).getByLabelText("Max views")).toHaveValue(12);
    await user.click(within(panel).getByRole("button", { name: "Publish" }));
    expect(await screen.findByRole("dialog", { name: "Public link" })).toBeVisible();
    expect(createPublication).toHaveBeenCalledTimes(2);
  });

  it("reuses an existing exact-URI publication without creating another link", async () => {
    vi.mocked(listPublications).mockResolvedValue({ publications: [publication] });
    const user = userEvent.setup();
    renderControl();
    await user.click(screen.getByRole("button", { name: "Publish" }));
    const panel = screen.getByRole("dialog", { name: "Publish document" });
    await user.click(within(panel).getByRole("button", { name: "Publish" }));
    expect(await screen.findByRole("dialog", { name: "Public link" })).toBeVisible();
    expect(createPublication).not.toHaveBeenCalled();
  });

  it("discards unsent options when dismissed and does not reopen after permissions recover", async () => {
    const user = userEvent.setup();
    const props = { vault: "demo", docId: "docs/guide.md", onPublished: vi.fn(), onUnpublish: vi.fn() };
    const { rerender } = render(<MemoryRouter><DocumentPublicationControl {...props} /></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Publish" }));
    await user.type(screen.getByLabelText("Max views"), "15");
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Publish" }));
    expect(screen.getByLabelText("Max views")).toHaveValue(null);
    rerender(<MemoryRouter><DocumentPublicationControl {...props} disabledReason="Access removed." /></MemoryRouter>);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    rerender(<MemoryRouter><DocumentPublicationControl {...props} /></MemoryRouter>);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(createPublication).not.toHaveBeenCalled();
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

  it("keeps the article interactive and lets an outside action dismiss the public-link panel", async () => {
    const user = userEvent.setup();
    renderControl({ publicSlug: "guide-public" });
    const trigger = screen.getByRole("button", { name: "Public link" });
    await user.click(trigger);
    expect(screen.getByRole("dialog", { name: "Public link" })).not.toHaveAttribute("aria-modal", "true");
    await user.click(screen.getByRole("button", { name: "Article action 0" }));
    expect(screen.queryByRole("dialog", { name: "Public link" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Article action 1" })).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("toggles from its trigger and has an explicit close action that returns focus", async () => {
    const user = userEvent.setup();
    renderControl({ publicSlug: "guide-public" });
    const trigger = screen.getByRole("button", { name: "Public link" });
    await user.click(trigger);
    await user.click(trigger);
    expect(screen.queryByRole("dialog", { name: "Public link" })).not.toBeInTheDocument();
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Close public link" }));
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByRole("dialog", { name: "Public link" })).not.toBeInTheDocument();
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
    const onPublished = vi.fn();
    const { rerender } = renderControl({ publicSlug: "guide-public", onUnpublish, onPublished });
    await user.click(screen.getByRole("button", { name: "Public link" }));
    await user.click(screen.getByRole("button", { name: "Unpublish" }));
    rerender(
      <MemoryRouter>
        <PublicationHarness
          vault="demo"
          docId="docs/guide.md"
          publicSlug="guide-public"
          disabledReason="Your write access was removed."
          onPublished={onPublished}
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
