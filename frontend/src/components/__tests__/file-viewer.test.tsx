import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { FilePreviewBody } from "@/components/file-viewer";

const YAML_SOURCE = "# Synthetic YAML\nservice:\n  name: 'fixture: unchanged'\n";

function textResponse(text: string): Response {
  return { text: async () => text } as Response;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
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
  it("continues to render JSON through JsonTree", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      json: async () => ({ enabled: true }),
    } as Response));
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
