import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
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
    await waitFor(() => expect(link).toHaveAccessibleDescription("Starter template"));
    expect(link.getAttribute("href")).toBe("/vault/my-v/settings#skill");
    expect(getSkillTemplateMock).toHaveBeenCalled();
    expect(screen.getByText("Starter template")).not.toHaveAttribute("aria-hidden", "true");
  });

  it("reads as 'customized' once the body diverges", async () => {
    getDocumentMock.mockResolvedValue({
      content: `${SEEDED_BODY}\n\nWe keep incident write-ups here.`,
    });
    renderVault();
    const link = await findChip();
    await waitFor(() => expect(link).toHaveAccessibleDescription("Guide customized"));
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
    await waitFor(() => expect(link).toHaveAccessibleDescription("Starter template"));
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
    expect(screen.getByRole("region", { name: "Contents" })).toBeInTheDocument();
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
    expect(link).not.toHaveAccessibleDescription(/customized|template/);
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
      screen.getByRole("region", { name: "Contents" }),
    ).toBeInTheDocument();
    const workspace = screen.getByRole("region", {
      name: "my-v Vault overview",
    });
    expect(workspace).toContainElement(
      screen.getByRole("region", { name: "Contents" }),
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

describe("vault overview content hierarchy", () => {
  it.each([
    ["none", "Private", "Only people with access can open this vault."],
    ["reader", "Public read", "Any signed-in person can read this vault."],
    ["writer", "Public write", "Signed-in users can read, and write when vault policies allow."],
    [undefined, "Not available", "Visibility information is unavailable."],
  ])("explains %s access separately from the member destination", async (publicAccess, label, explanation) => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v", role: "reader", document_count: 4,
      public_access: publicAccess, owner_display_name: "Vault Owner", member_count: 5,
    });
    renderVault();
    const access = await screen.findByRole("region", { name: "Access and ownership" });
    expect(access).toHaveTextContent(label!);
    expect(access).toHaveTextContent(explanation!);
    expect(within(access).getAllByRole("link")).toHaveLength(1);
    expect(within(access).getByRole("link", { name: /Members.*5/ })).toHaveAttribute("href", "/vault/my-v/members");
    expect(within(access).queryByRole("button")).toBeNull();
  });

  it.each(["is_archived", "is_external_git"])("does not promise public writes when %s makes content read-only", async (restriction) => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v", role: "owner", document_count: 4,
      public_access: "writer", [restriction]: true,
    });
    renderVault();
    const access = await screen.findByRole("region", { name: "Access and ownership" });
    expect(access).toHaveTextContent("Any signed-in person can read this vault. Content is read-only.");
    expect(access).not.toHaveTextContent("change content");
    expect(screen.queryByRole("button", { name: "New document" })).toBeNull();
  });

  it("keeps identity and passive totals together above the content while membership stays in Access", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v", role: "writer", document_count: 36, collection_count: 8,
      table_count: 2, file_count: 7, member_count: 5, public_access: "reader",
      owner_display_name: "Vault Owner", tables: [{ name: "service_catalog", row_count: 18 }],
    });
    renderVault();
    const contents = await screen.findByRole("region", { name: "Contents" });
    const summary = screen.getByRole("region", { name: "Vault summary" });
    const details = screen.getByRole("complementary", { name: "Vault overview details" });
    expect(summary).toContainElement(contents);
    expect(details).not.toContainElement(contents);
    expect(within(summary).getByText("writer", { exact: true })).toBeInTheDocument();
    expect(within(summary).getByText("public:reader")).toBeInTheDocument();
    expect(within(summary).getByRole("button", { name: "Copy akb://my-v" })).toBeEnabled();
    expect(within(details).queryByRole("button", { name: "Copy akb://my-v" })).toBeNull();
    expect(within(contents).queryByRole("button")).toBeNull();
    for (const [label, value] of [["Documents", "36"], ["Collections", "8"], ["Tables", "2"], ["Files", "7"]]) {
      expect(within(contents).getByText(label).closest("div")).toHaveTextContent(value);
    }
    expect(within(contents).queryByText("Members")).toBeNull();
    const access = screen.getByRole("region", { name: "Access and ownership" });
    expect(within(access).getByRole("link", { name: /Members.*5/ })).toHaveAttribute("href", "/vault/my-v/members");
    expect(access).not.toHaveTextContent("Your role");
    expect(within(details).queryByRole("region", { name: "Tables" })).toBeNull();
    expect(screen.queryByRole("link", { name: /service_catalog/ })).toBeNull();
    const actions = within(summary).getByRole("group", { name: "Create content" });
    expect(within(actions).getByRole("button", { name: "Upload file" })).toBeEnabled();
    expect(within(actions).getByRole("button", { name: "New table" })).toBeEnabled();
  });

  it("distinguishes missing counts from zero and never treats a legacy info response as empty", async () => {
    getVaultInfoMock.mockResolvedValue({ name: "my-v", role: "reader", file_count: 0 });
    renderVault();
    const contents = await screen.findByRole("region", { name: "Contents" });
    expect(within(contents).getByText("Documents").closest("div")).toHaveTextContent("—");
    expect(within(contents).getByText("Files").closest("div")).toHaveTextContent("0");
    expect(screen.getByRole("region", { name: "Access and ownership" })).not.toHaveTextContent("Private");
    expect(screen.queryByRole("region", { name: "Set up this Vault" })).toBeNull();
    expect(screen.getByRole("region", { name: "Recent activity" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New table" })).toBeNull();
  });

  it("keeps document destinations while showing collection context instead of a repeated filename and commit", async () => {
    getRecentMock.mockResolvedValue({ changes: [
      { doc_id: "d-runbook", title: "Runbook", path: "team/ops/runbook.md", commit: "abcdef012345", changed_at: "2026-09-21T00:00:00Z" },
      { doc_id: "d-root", title: "Welcome", path: "welcome.md" },
    ] });
    renderVault();
    const recent = await screen.findByRole("region", { name: "Recent activity" });
    const runbook = await within(recent).findByRole("link", { name: /Runbook/ });
    expect(runbook).toHaveAttribute("href", "/vault/my-v/doc/team%2Fops%2Frunbook.md");
    expect(runbook).toHaveTextContent("team / ops");
    expect(runbook).not.toHaveTextContent("runbook.md");
    expect(recent).not.toHaveTextContent("abcdef0");
    expect(within(recent).getByRole("link", { name: /Welcome/ })).toHaveTextContent("Vault root");
  });

  it("keeps the sidebar for vault context without treating table inventory as recent activity", async () => {
    getVaultInfoMock.mockResolvedValue({
      name: "my-v", role: "reader", table_count: 4, file_count: 2,
      tables: [{ name: "catalog" }, { name: "inventory", row_count: 100 }],
    });
    renderVault();
    const details = await screen.findByRole("complementary", { name: "Vault overview details" });
    expect(within(details).getAllByRole("region")).toHaveLength(2);
    expect(within(details).getByRole("region", { name: "Vault guide" })).toBeInTheDocument();
    expect(within(details).getByRole("region", { name: "Access and ownership" })).toBeInTheDocument();
    expect(within(details).queryByRole("region", { name: "Tables" })).toBeNull();
    expect(screen.queryByRole("link", { name: /catalog|inventory/ })).toBeNull();
    const contents = screen.getByRole("region", { name: "Contents" });
    expect(within(contents).getByText("Tables").closest("div")).toHaveTextContent("4");
    expect(within(contents).getByText("Files").closest("div")).toHaveTextContent("2");
    const recent = screen.getByRole("region", { name: "Recent activity" });
    expect(await within(recent).findByText("Nothing written yet")).toBeInTheDocument();
    expect(within(recent).queryByRole("link")).toBeNull();
  });

  it("shows an unavailable context rather than a permanent loading skeleton after info fails", async () => {
    getVaultInfoMock.mockRejectedValue(new Error("Unavailable"));
    renderVault();
    expect(await screen.findByText("Vault details are unavailable.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
    expect(screen.queryByRole("region", { name: "Contents" })).toBeNull();
  });
});
