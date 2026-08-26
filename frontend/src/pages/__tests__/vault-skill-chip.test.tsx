import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import VaultPage from "@/pages/vault";

vi.mock("@/lib/api", () => ({
  authenticatedFetch: vi.fn(),
  createVaultTable: vi.fn(),
  getDocument: vi.fn(),
  getRecent: vi.fn(),
  getSkillTemplate: vi.fn(),
  getVaultActivity: vi.fn(),
  getVaultInfo: vi.fn(),
  importKnowledgeBundle: vi.fn(),
  uploadVaultFile: vi.fn(),
}));

import {
  authenticatedFetch,
  createVaultTable,
  getDocument,
  getRecent,
  getSkillTemplate,
  getVaultActivity,
  getVaultInfo,
  importKnowledgeBundle,
  uploadVaultFile,
} from "@/lib/api";

const authenticatedFetchMock = authenticatedFetch as unknown as ReturnType<
  typeof vi.fn
>;
const getDocumentMock = getDocument as unknown as ReturnType<typeof vi.fn>;
const getRecentMock = getRecent as unknown as ReturnType<typeof vi.fn>;
const getSkillTemplateMock = getSkillTemplate as unknown as ReturnType<
  typeof vi.fn
>;
const getVaultActivityMock = getVaultActivity as unknown as ReturnType<
  typeof vi.fn
>;
const getVaultInfoMock = getVaultInfo as unknown as ReturnType<typeof vi.fn>;
const importKnowledgeBundleMock =
  importKnowledgeBundle as unknown as ReturnType<typeof vi.fn>;
const uploadVaultFileMock = uploadVaultFile as unknown as ReturnType<
  typeof vi.fn
>;
const createVaultTableMock = createVaultTable as unknown as ReturnType<
  typeof vi.fn
>;

// The endpoint serves the raw seed file (trailing newline intact); the stored
// body comes back frontmatter-parsed and whitespace-stripped. An untouched
// guide therefore differs from the template by exactly that newline.
const TEMPLATE = "# {vault} Guide\n\n(Describe what this vault is for.)\n";
const SEEDED_BODY = "# my-v Guide\n\n(Describe what this vault is for.)";

function renderVault() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/vault/my-v"]}>
        <Routes>
          <Route path="/vault/:name" element={<VaultPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const findChip = () => screen.findByRole("link", { name: "Open vault guide" });

beforeEach(() => {
  vi.clearAllMocks();
  authenticatedFetchMock.mockResolvedValue({ ok: false });
  getRecentMock.mockResolvedValue({ changes: [] });
  getVaultActivityMock.mockResolvedValue({ activity: [] });
  getSkillTemplateMock.mockResolvedValue(TEMPLATE);
  getDocumentMock.mockResolvedValue({ content: SEEDED_BODY });
  getVaultInfoMock.mockResolvedValue({
    name: "my-v",
    role: "owner",
    document_count: 4,
    table_count: 0,
    file_count: 0,
  });
  importKnowledgeBundleMock.mockResolvedValue({
    format: "okf",
    vault: "my-v",
    created: 2,
    skipped: 0,
    failed: 0,
    uris: [],
    skipped_paths: [],
    reserved: [],
    errors: [],
  });
  uploadVaultFileMock.mockResolvedValue({
    kind: "file",
    uri: "akb://my-v/file/4f9d5609-c6c7-4b62-a8be-79308c18cb8d",
    vault: "my-v",
    name: "runbook.pdf",
    mime_type: "application/pdf",
    size_bytes: 7,
  });
  createVaultTableMock.mockResolvedValue({
    kind: "table",
    uri: "akb://my-v/table/incidents",
    vault: "my-v",
    name: "incidents",
  });
});

afterEach(cleanup);

describe("vault page guide chip", () => {
  it("reads as 'template' when the body still matches the substituted seed", async () => {
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(link.textContent).toContain("template"));
    expect(link.getAttribute("href")).toBe("/vault/my-v/settings#skill");
    expect(getSkillTemplateMock).toHaveBeenCalled();
  });

  it("reads as 'customized' once the body diverges", async () => {
    getDocumentMock.mockResolvedValue({
      content: `${SEEDED_BODY}\n\nWe keep incident write-ups here.`,
    });
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(link.textContent).toContain("customized"));
    expect(link.getAttribute("href")).toBe("/vault/my-v/settings#skill");
  });

  it("keeps an untouched historical seed classified as template while retaining overview context", async () => {
    getDocumentMock.mockResolvedValue({
      content: "# my-v Guide\n\nAn older default placeholder.",
      created_at: "2026-01-02T03:04:05Z",
      updated_at: "2026-01-02T03:04:05Z",
    });
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(link.textContent).toContain("template"));
    expect(
      screen.getByRole("heading", { name: "Vault guide" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Add purpose, scope, and agent instructions/),
    ).toBeInTheDocument();
  });

  it("keeps recent activity focused and progressively discloses commit history", async () => {
    getRecentMock.mockResolvedValue({
      changes: [
        {
          doc_id: "d-runbook",
          vault: "my-v",
          path: "ops/runbook.md",
          title: "Runbook",
          type: "note",
          commit: "abcdef0123456789",
          changed_at: "2026-08-25T08:00:00Z",
        },
      ],
    });
    getVaultActivityMock.mockResolvedValue({
      activity: [
        {
          hash: "abcdef0123456789",
          author_name: "Vault Owner",
          subject: "Update incident runbook",
          date: "2026-08-25T08:00:00Z",
          files: [{ path: "ops/runbook.md", change: "modified" }],
        },
      ],
    });

    renderVault();

    expect(
      await screen.findByRole("region", { name: "Recent activity" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("region", { name: "Commit history" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Full activity" })).toBeNull();
    expect(screen.queryByText("Update incident runbook")).toBeNull();
    expect(
      screen.getByRole("link", { name: "Full commit log" }),
    ).toHaveAttribute("href", "/vault/my-v/activity");
    const disclosure = await screen.findByRole("button", {
      name: "Show commits",
    });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(disclosure);
    expect(
      screen.getByRole("button", { name: "Hide commits" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Update incident runbook")).toBeInTheDocument();
    expect(screen.getByText("Vault Owner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Vault context" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Tables" })).toBeNull();
    await waitFor(() =>
      expect(getVaultActivityMock).toHaveBeenCalledWith("my-v", { limit: 10 }),
    );
  });

  it("shows no state (never a wrong one) while the template is in flight", async () => {
    getSkillTemplateMock.mockReturnValue(new Promise(() => {}));
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(getSkillTemplateMock).toHaveBeenCalled());
    expect(link.textContent).not.toContain("customized");
    expect(link.textContent).not.toContain("template");
  });

  it("withholds the chip on a mirror vault, which carries no guide", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "mirror-v",
      role: "owner",
      is_external_git: true,
      document_count: 4,
      table_count: 0,
      file_count: 0,
    });
    getDocumentMock.mockRejectedValue(new Error("not found"));
    renderVault();
    // The stat tiles only render once /info resolved — i.e. once the chip
    // would have had everything it needs.
    await screen.findByText("Documents");
    expect(screen.queryByRole("link", { name: "Open vault guide" })).toBeNull();
    expect(
      screen.queryByRole("link", { name: "Set up vault guide" }),
    ).toBeNull();
    expect(getSkillTemplateMock).not.toHaveBeenCalled();
  });

  it("keeps the overview hierarchy for an empty vault and points the guide step at settings", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v",
      role: "owner",
      owner_display_name: "Vault Owner",
      member_count: 1,
      public_access: "none",
      created_at: "2026-01-02T03:04:05Z",
      description: "A quiet place for the team's first knowledge.",
      document_count: 1, // the seeded guide alone → still "empty"
      table_count: 0,
      file_count: 0,
    });
    renderVault();
    const step = await screen.findByRole("link", {
      name: /Describe this Vault/i,
    });
    expect(step.getAttribute("href")).toBe("/vault/my-v/settings#skill");
    const setup = screen.getByRole("region", { name: "Set up this Vault" });
    expect(setup).toContainElement(step);
    expect(setup).toContainElement(
      screen.getByRole("button", { name: /Import knowledge bundle/i }),
    );
    expect(setup).toContainElement(
      screen.getByRole("link", { name: /Connect an agent/i }),
    );
    expect(
      screen.getByRole("button", { name: /New document/ }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /Upload file/ }).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByRole("button", { name: /Create a table|New table/ })
        .length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("Optional next steps")).toBeInTheDocument();
    expect(screen.queryByText("Build the foundation")).toBeNull();
    expect(
      screen.getByRole("heading", { level: 1, name: "my-v" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Vault inventory" }),
    ).toBeInTheDocument();
    const workspace = screen.getByRole("region", {
      name: "my-v Vault overview",
    });
    expect(workspace).toContainElement(
      screen.getByRole("region", { name: "Vault inventory" }),
    );
    const overview = screen.getByRole("complementary", {
      name: "Vault overview details",
    });
    expect(overview).toHaveTextContent("Vault Owner");
    expect(overview).toHaveTextContent("Private");
    expect(overview).toHaveTextContent("Members");
    expect(overview).toHaveTextContent("1");
    expect(
      screen.queryByRole("heading", { name: "About this vault" }),
    ).toBeNull();
    expect(screen.queryByText("Edges")).toBeNull();
    expect(screen.queryByText(/Owned by/)).toBeNull();
    expect(
      screen.getByText("A quiet place for the team's first knowledge."),
    ).toBeInTheDocument();
  });

  it("imports an OKF zip from the empty-state action and reports the result", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v",
      role: "owner",
      document_count: 1,
      table_count: 0,
      file_count: 0,
    });
    renderVault();
    const input = await screen.findByLabelText("Choose knowledge bundle");
    const bundle = new File(["bundle"], "knowledge.okf.zip", {
      type: "application/zip",
    });
    fireEvent.change(input, { target: { files: [bundle] } });
    await waitFor(() =>
      expect(importKnowledgeBundleMock).toHaveBeenCalledWith("my-v", bundle),
    );
    expect(await screen.findByText("Knowledge imported")).toBeInTheDocument();
    expect(screen.getByText(/2 documents created/i)).toBeInTheDocument();
  });

  it("wires file upload and table creation to their real creation flows", async () => {
    renderVault();

    fireEvent.click(await screen.findByRole("button", { name: "Upload file" }));
    const upload = new File(["runbook"], "runbook.pdf", {
      type: "application/pdf",
    });
    fireEvent.change(screen.getByLabelText(/File \*/), {
      target: { files: [upload] },
    });
    fireEvent.click(screen.getByRole("button", { name: "Upload file" }));
    await waitFor(() =>
      expect(uploadVaultFileMock).toHaveBeenCalledWith(
        "my-v",
        upload,
        expect.objectContaining({
          collection: "",
          description: "",
          onStageChange: expect.any(Function),
        }),
      ),
    );

    renderVault();
    fireEvent.click(await screen.findByRole("button", { name: "New table" }));
    fireEvent.change(screen.getByLabelText(/Table name/), {
      target: { value: "incidents" },
    });
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "status" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create table" }));
    await waitFor(() =>
      expect(createVaultTableMock).toHaveBeenCalledWith("my-v", {
        name: "incidents",
        description: "",
        collection: "",
        columns: [
          {
            name: "status",
            type: "text",
            required: false,
            unique: false,
          },
        ],
      }),
    );
  });
});
