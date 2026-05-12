import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { VaultRefreshProvider } from "@/contexts/vault-refresh-context";
import DocumentPage from "@/pages/document";

vi.mock("@/lib/api", () => ({
  getDocument: vi.fn(),
  getVaultInfo: vi.fn(),
  getRelations: vi.fn(),
  deleteDocument: vi.fn(),
  publishDoc: vi.fn(),
  unpublishDoc: vi.fn(),
}));

import {
  getDocument,
  getVaultInfo,
  getRelations,
} from "@/lib/api";

const getDocumentMock = getDocument as unknown as ReturnType<typeof vi.fn>;
const getVaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const getRelationsMock = getRelations as unknown as ReturnType<typeof vi.fn>;

const SAMPLE_CONTENT = "# BodyHeading\n\nworld";

function makeDoc(overrides: Record<string, unknown> = {}) {
  return {
    id: "0c37e906-6db0-48c2-ac5d-576d0797b3f7",
    path: "notes/hello.md",
    title: "DocTitle",
    content: SAMPLE_CONTENT,
    current_commit: "abcdef1234567",
    type: null,
    status: null,
    tags: [],
    is_public: false,
    public_slug: null,
    created_by: null,
    updated_at: null,
    ...overrides,
  };
}

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location-search">{loc.search}</div>;
}

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <VaultRefreshProvider refetchVaults={vi.fn()} refetchTree={vi.fn()}>
        <Routes>
          <Route path="/vault/:name/doc/:id" element={<DocumentPage />} />
        </Routes>
      </VaultRefreshProvider>
      <LocationProbe />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  getDocumentMock.mockReset();
  getVaultInfoMock.mockReset();
  getRelationsMock.mockReset();

  getDocumentMock.mockResolvedValue(makeDoc());
  getVaultInfoMock.mockResolvedValue({ role: "reader" });
  getRelationsMock.mockResolvedValue({ relations: [] });

  // /activity is fetched directly via fetch() — stub it to a no-op.
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ activity: [] }),
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DocumentPage view toggle", () => {
  it("renders Markdown by default", async () => {
    renderAt("/vault/v/doc/notes%2Fhello.md");
    // Wait for the markdown body heading to render through react-markdown.
    expect(
      await screen.findByRole("heading", { level: 1, name: "BodyHeading" }),
    ).toBeInTheDocument();
    // The raw <pre> should NOT be present.
    expect(screen.queryByTestId("doc-raw")).not.toBeInTheDocument();
  });

  it("?view=raw renders the raw markdown inside <pre>", async () => {
    renderAt("/vault/v/doc/notes%2Fhello.md?view=raw");
    const pre = await screen.findByTestId("doc-raw");
    expect(pre.tagName).toBe("PRE");
    expect(pre.textContent).toBe(SAMPLE_CONTENT);
    // The rendered-markdown body heading should NOT be present.
    expect(
      screen.queryByRole("heading", { level: 1, name: "BodyHeading" }),
    ).not.toBeInTheDocument();
    // No `.prose` container is rendered in raw mode.
    expect(document.querySelector(".prose")).toBeNull();
  });

  it("clicking the toggle button switches to raw view and updates the URL", async () => {
    const user = userEvent.setup();
    renderAt("/vault/v/doc/notes%2Fhello.md");

    // Wait for the page to settle in rendered mode.
    await screen.findByRole("heading", { level: 1, name: "BodyHeading" });

    const rawTab = screen.getByRole("tab", { name: "RAW" });
    const renderedTab = screen.getByRole("tab", { name: "RENDERED" });
    expect(rawTab).toHaveAttribute("aria-selected", "false");
    expect(renderedTab).toHaveAttribute("aria-selected", "true");
    await user.click(rawTab);

    // Pre appears now.
    const pre = await screen.findByTestId("doc-raw");
    expect(pre).toBeInTheDocument();

    // Selection flips on the segmented control.
    expect(screen.getByRole("tab", { name: "RAW" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "RENDERED" })).toHaveAttribute("aria-selected", "false");

    // The URL search now contains view=raw.
    expect(screen.getByTestId("location-search")).toHaveTextContent("view=raw");
  });

  it("Copy button writes raw content to the clipboard and flips to COPIED", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
      writable: true,
    });

    renderAt("/vault/v/doc/notes%2Fhello.md?view=raw");

    await screen.findByTestId("doc-raw");
    const copy = screen.getByRole("button", { name: /copy markdown/i });
    expect(copy).toHaveTextContent("COPY");

    // Direct .click() avoids userEvent's clipboard-aware setup, which
    // installs its own ClipboardStubImpl and shadows our writeText spy.
    copy.click();

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(SAMPLE_CONTENT);
    });

    // Button text flips to COPIED while the 1.5s feedback timer runs.
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /copy markdown/i }),
      ).toHaveTextContent("COPIED");
    });
  });
});
