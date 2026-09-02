import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getDocumentDiff: vi.fn() };
});

import DocumentDiffView from "@/components/document-diff-view";
import {
  DocumentRevisionApiError,
  getDocumentDiff,
  type DocumentDiff,
} from "@/lib/api";

const getDocumentDiffMock = vi.mocked(getDocumentDiff);

function result(overrides: Partial<DocumentDiff> = {}): DocumentDiff {
  return {
    kind: "document_diff",
    file: "notes/guide.md",
    commit: "abcdef123456", // pragma: allowlist secret — synthetic Git commit
    type: "modified",
    diff: [
      "--- a/notes/guide.md",
      "+++ b/notes/guide.md",
      "@@ -1,2 +1,2 @@",
      "-Old title",
      "+New title",
      " body",
      "@@ -10,1 +10,1 @@",
      "-Old ending",
      "+New ending",
    ].join("\n"),
    ...overrides,
  };
}

function renderDiff(props: Partial<React.ComponentProps<typeof DocumentDiffView>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const callbacks = {
    onBackToVersion: vi.fn(),
    onBackToLatest: vi.fn(),
    onOpenBase: vi.fn(),
  };
  return {
    callbacks,
    ...render(
      <QueryClientProvider client={client}>
        <DocumentDiffView
          vault="demo"
          docId="notes/guide.md"
          revision="abcdef123456"
          baseRevision={"123456abcdef" /* pragma: allowlist secret — synthetic Git commit */}
          targetEntry={{
            hash: "abcdef123456",
            message: "Update guide",
            author: "user-1",
            author_name: "Kim",
            date: "2026-09-02T00:00:00Z",
          }}
          {...callbacks}
          {...props}
        />
      </QueryClientProvider>,
    ),
  };
}

beforeEach(() => {
  getDocumentDiffMock.mockReset();
  getDocumentDiffMock.mockResolvedValue(result());
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DocumentDiffView", () => {
  it("renders a full-width unified change ledger with non-color labels", async () => {
    renderDiff();

    expect(await screen.findByRole("table", { name: /unified document changes/i })).toBeInTheDocument();
    expect(screen.getByRole("row", { name: "Removed line 1: Old title" })).toBeInTheDocument();
    expect(screen.getByRole("row", { name: "Added line 1: New title" })).toBeInTheDocument();
    expect(screen.getByLabelText("2 added lines")).toHaveTextContent("+2 added");
    expect(screen.getByLabelText("2 removed lines")).toHaveTextContent("−2 removed");
    expect(screen.getByText(/Update guide/)).toHaveTextContent("Update guide · Kim");
  });

  it("navigates hunks and announces the current change", async () => {
    const user = userEvent.setup();
    renderDiff();

    await screen.findByRole("table", { name: /unified document changes/i });
    const next = screen.getByRole("button", { name: /next change\. change 1 of 2/i });
    await user.click(next);

    expect(screen.getByText("Change 2 of 2", { selector: ".sr-only" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /next change\. change 2 of 2/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /previous change\. change 2 of 2/i })).toBeEnabled();
  });

  it("copies the original server patch", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
      writable: true,
    });
    const patch = result().diff;
    renderDiff();

    await screen.findByRole("table", { name: /unified document changes/i });
    const copy = await screen.findByRole("button", { name: "Copy patch" });
    copy.click();
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(patch));
    expect(copy).toHaveAccessibleName("Patch copied");
  });

  it("distinguishes an old-server 405 from retryable failures", async () => {
    getDocumentDiffMock.mockRejectedValue(
      new DocumentRevisionApiError("Method Not Allowed", 405),
    );
    const { callbacks } = renderDiff();

    expect(await screen.findByText("Changes are not available on this server")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Open version" }));
    expect(callbacks.onBackToVersion).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("shows a stable no-content state", async () => {
    getDocumentDiffMock.mockResolvedValue(result({ type: "unchanged", diff: "" }));
    const { callbacks } = renderDiff();

    expect(await screen.findByText("No content changes in this revision")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Open version" }));
    expect(callbacks.onBackToVersion).toHaveBeenCalledOnce();
  });
});
