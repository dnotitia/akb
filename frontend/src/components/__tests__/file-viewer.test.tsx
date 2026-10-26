import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FilePreviewBody } from "@/components/file-viewer";
import userEvent from "@testing-library/user-event";

const YAML_SOURCE = "# Synthetic YAML\nservice:\n  name: 'fixture: unchanged'\n";

function textResponse(text: string): Response {
  return new Response(text);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("FilePreviewBody text handling", () => {
  it.each(["application/yaml", "text/yaml"])("renders %s as raw text", async (mime) => {
    const raw = "# Keep this comment\nservice:\n  invalid: [unfinished\n";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(textResponse(raw)));
    const { container } = render(
      <FilePreviewBody mime={mime} directUrl="/yaml" rawUrl="/yaml" name="settings.yaml" />,
    );

    await waitFor(() => expect(container.querySelector("pre")?.textContent).toBe(raw));
    expect(screen.queryByText("Preview unavailable")).not.toBeInTheDocument();
  });

  it("keeps the loading state until the raw text response arrives", async () => {
    let resolveFetch!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { resolveFetch = resolve; });
    vi.stubGlobal("fetch", vi.fn(() => pending));
    const { container } = render(
      <FilePreviewBody mime="text/yaml" directUrl="/yaml" rawUrl="/yaml" name="settings.yaml" />,
    );

    expect(screen.getByRole("status", { name: "Loading text preview" })).toBeInTheDocument();
    await act(async () => resolveFetch(textResponse(YAML_SOURCE)));
    await waitFor(() => expect(container.querySelector("pre")?.textContent).toBe(YAML_SOURCE));
  });

  it("keeps the existing text cap and explains when a preview is truncated", async () => {
    const raw = "x".repeat(512 * 1024 + 20);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(textResponse(raw)));
    const { container } = render(
      <FilePreviewBody mime="text/plain" directUrl="/large" rawUrl="/large" name="large.txt" />,
    );

    await waitFor(() => expect(container.querySelector("pre")?.textContent).toHaveLength(512 * 1024));
    expect(screen.getByText(/Preview truncated/)).toBeInTheDocument();
  });

  it("keeps the recoverable text-preview fetch error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<FilePreviewBody mime="text/yaml" directUrl="/yaml" rawUrl="/yaml" name="settings.yaml" />);

    expect(await screen.findByText(/offline/)).toBeInTheDocument();
  });
});

describe("FilePreviewBody existing format branches", () => {
  it("distinguishes fit and actual image size within a keyboard-scrollable canvas", async () => {
    const user = userEvent.setup();
    render(<FilePreviewBody mime="image/png" directUrl="/diagram.png" rawUrl="/diagram.png" name="diagram.png" />);
    const image = screen.getByRole("img", { name: "diagram.png" });
    fireEvent.load(image);
    expect(screen.getByRole("button", { name: "Fit image" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Actual size" }));
    expect(image).toHaveClass("max-w-none");
    expect(screen.getByRole("button", { name: "Actual size" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("region", { name: "Image file content" })).toHaveAttribute("tabindex", "0");
    await user.click(screen.getByRole("button", { name: "Fit image" }));
    expect(image).toHaveClass("max-w-full");
  });

  it("makes image errors recoverable through a current-access link refresh", async () => {
    const user = userEvent.setup();
    const refresh = vi.fn();
    render(<FilePreviewBody mime="image/png" directUrl="/diagram.png" rawUrl="/diagram.png" name="diagram.png" onRefreshLink={refresh} />);
    fireEvent.error(screen.getByRole("img"));
    expect(screen.getByText("Preview unavailable")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh link" }));
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("shows known identity and size for a download-only format", () => {
    render(<FilePreviewBody mime="application/octet-stream" directUrl="/archive" rawUrl="/archive" name="archive.bin" sizeBytes={2048} />);
    expect(screen.getByText("archive.bin")).toBeInTheDocument();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getByText(/Download the original file/)).toBeInTheDocument();
  });
  it("continues to render JSON through JsonTree", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(textResponse(JSON.stringify({ enabled: true }))));
    render(
      <FilePreviewBody
        mime="application/json"
        directUrl="/settings.json"
        rawUrl="/settings.json"
        name="settings.json"
      />,
    );

    expect(await screen.findByRole("button", { name: /object/ })).toBeInTheDocument();
  });

  it("offers recovery when a PDF does not finish loading", () => {
    vi.useFakeTimers();
    const refresh = vi.fn();
    try {
      render(<FilePreviewBody mime="application/pdf" directUrl="/slow.pdf" rawUrl="/slow.pdf" name="Slow PDF" onRefreshLink={refresh} />);
      act(() => vi.advanceTimersByTime(10_000));
      expect(screen.getByText(/PDF is taking longer/)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Open PDF" })).toHaveAttribute("href", "/slow.pdf");
      fireEvent.click(screen.getByRole("button", { name: "Refresh link" }));
      expect(refresh).toHaveBeenCalledOnce();
      fireEvent.load(screen.getByTitle("Slow PDF"));
      expect(screen.queryByText(/PDF is taking longer/)).toBeNull();
    } finally { vi.useRealTimers(); }
  });

  it("continues to render images and PDFs inline", () => {
    const image = render(
      <FilePreviewBody mime="image/png" directUrl="/diagram.png" rawUrl="/diagram.png" name="diagram.png" />,
    );
    expect(screen.getByRole("img", { name: "diagram.png" })).toHaveAttribute("src", "/diagram.png");

    image.unmount();
    render(
      <FilePreviewBody mime="application/pdf" directUrl="/guide.pdf" rawUrl="/guide.pdf" name="guide.pdf" />,
    );
    expect(screen.getByTitle("guide.pdf")).toHaveAttribute("src", "/guide.pdf");
  });
});

describe("bounded and recoverable File previews", () => {
  it.each([403, 404, 503])("does not render HTTP %s error bytes as file content", async (status) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("SECRET ERROR BODY", { status })));
    const { container } = render(<FilePreviewBody mime="text/plain" directUrl="/text" rawUrl="/text" name="error.txt" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(String(status));
    expect(container.querySelector("pre")).toBeNull();
    expect(screen.queryByText("SECRET ERROR BODY")).toBeNull();
  });

  it("does not parse oversized JSON and gives a download-only explanation", async () => {
    const raw = JSON.stringify({ value: "x".repeat(512 * 1024) });
    const parse = vi.spyOn(JSON, "parse");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(raw)));
    render(<FilePreviewBody mime="application/json" directUrl="/json" rawUrl="/json" name="large.json" />);
    expect(await screen.findByText(/too large for an inline preview/)).toBeInTheDocument();
    expect(parse).not.toHaveBeenCalledWith(raw);
    expect(screen.queryByRole("button", { name: /object/ })).toBeNull();
    parse.mockRestore();
  });

  it("reports invalid JSON separately from loading and supports Retry preview", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response("{invalid"))
      .mockResolvedValueOnce(new Response('{"valid":true}')));
    render(<FilePreviewBody mime="application/json" directUrl="/json" rawUrl="/json" name="data.json" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("isn't valid JSON");
    await user.click(screen.getByRole("button", { name: "Retry preview" }));
    expect(await screen.findByRole("button", { name: /object/ })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("aborts the old read and ignores its late bytes when the URL changes", async () => {
    let resolveOld!: (response: Response) => void;
    const fetcher = vi.fn().mockReturnValueOnce(new Promise<Response>((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce(new Response("New file content"));
    vi.stubGlobal("fetch", fetcher);
    const page = render(<FilePreviewBody mime="text/plain" directUrl="/old" rawUrl="/old" name="old.txt" />);
    const oldSignal = fetcher.mock.calls[0][1].signal as AbortSignal;
    page.rerender(<FilePreviewBody mime="text/plain" directUrl="/new" rawUrl="/new" name="new.txt" />);
    expect(oldSignal.aborted).toBe(true);
    await screen.findByText("New file content");
    await act(async () => { resolveOld(new Response("Old private bytes")); });
    expect(screen.queryByText("Old private bytes")).toBeNull();
    expect(screen.getByText("New file content")).toBeInTheDocument();
  });
});
