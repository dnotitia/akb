import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import {
  getPublication,
  publicationCapabilities,
  type PublicationResponse,
} from "@/lib/api";
import PublicationPage from "@/pages/public-publication";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  getPublication: vi.fn(),
  publicationCapabilities: vi.fn(),
}));

const getPublicFile = vi.mocked(getPublication);
const getCapabilities = vi.mocked(publicationCapabilities);
const raw = "# Public synthetic file\nservice:\n  note: 'read exactly'\n";
const publication = {
  resource_type: "file",
  title: "Public settings",
  name: "public-settings.yaml",
  mime_type: "application/yaml",
  size_bytes: raw.length,
} as PublicationResponse;

beforeEach(() => {
  vi.clearAllMocks();
  getPublicFile.mockResolvedValue(publication);
  getCapabilities.mockResolvedValue({ can_edit: false });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    text: async () => raw,
  } as Response));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("public file publication", () => {
  it("renders the already-public YAML source in the shared Text preview", async () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/p/yaml-public"]}>
        <Routes><Route path="/p/:slug" element={<PublicationPage />} /></Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Public")).toBeInTheDocument();
    expect(getPublicFile).toHaveBeenCalledWith("yaml-public", {});
    await waitFor(() => expect(container.querySelector("pre")?.textContent).toBe(raw));
    expect(screen.queryByText("Preview unavailable")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download original" })).toHaveAttribute(
      "href",
      expect.stringContaining("/api/v1/public/yaml-public/download"),
    );
  });
});
