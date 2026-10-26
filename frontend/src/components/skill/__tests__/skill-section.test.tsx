import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SkillSection } from "../skill-section";
import { ResourceNavigationProvider, useResourceNavigation } from "@/contexts/resource-navigation-context";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { VAULT_SKILL_PATH } from "@/lib/skill";

const getSkillTemplate = vi.fn();
const getVaultSkillPreview = vi.fn();
const updateDocument = vi.fn();

vi.mock("@/lib/api", async original => ({
  ...await original<typeof import("@/lib/api")>(),
  getSkillTemplate: (...a: any[]) => getSkillTemplate(...a),
  getVaultSkillPreview: (...a: any[]) => getVaultSkillPreview(...a),
  updateDocument: (...a: any[]) => updateDocument(...a),
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>
  );
}

const DOC = {
  content: "# my-v Guide\n\nStored body",
  updated_at: "2026-05-18T10:00:00Z",
  current_commit: "abc123def456", // pragma: allowlist secret — synthetic Git commit
};

beforeEach(() => {
  vi.clearAllMocks();
  getVaultSkillPreview.mockResolvedValue("composed agent text");
});

async function openGuideOption(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("button", { name: "Guide options" }));
  await user.click(screen.getByRole("menuitem", { name }));
}

describe("SkillSection", () => {
  it.each(["success", "failure"])("accepts the same-account save %s across access proof revisions", async result => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const account = { user_id: "a", username: "a", email: "a@x.test", display_name: null, is_admin: false, auth_method: "local", key_class: null };
    const view = (checking = false, revision = 0) => <QueryClientProvider client={qc}><MemoryRouter><CurrentUserProvider user={account} checking={checking} revision={revision}><SkillSection vault="my-v" doc={DOC} canManage={!checking} /></CurrentUserProvider></MemoryRouter></QueryClientProvider>;
    let finish!: (saved: { current_commit: string }) => void;
    let fail!: (error: Error) => void;
    updateDocument.mockReturnValueOnce(new Promise((resolve, reject) => { finish = resolve; fail = reject; }));
    const { rerender } = render(view());
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: /save guide/i }));
    rerender(view(true));
    await act(async () => result === "success" ? finish({ current_commit: "saved" }) : fail(new Error("Guide save unavailable")));
    rerender(view(false, 1));
    if (result === "success") {
      expect(screen.queryByRole("textbox", { name: "Vault guide body" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Edit guide" })).toBeVisible();
      expect(qc.getQueryData(["document", "my-v", VAULT_SKILL_PATH])).toMatchObject({ current_commit: "saved", content: `${DOC.content} local draft` });
    } else {
      expect(await screen.findByText("Guide save unavailable")).toBeVisible();
      expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
      expect(screen.getByRole("button", { name: /save guide/i })).toBeEnabled();
    }
  });

  it("keeps dirty content and its revision pin across Agent view", async () => {
    const user = userEvent.setup();
    updateDocument.mockResolvedValue({ current_commit: "saved-revision" });
    const rendered = render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await openGuideOption(user, "Agent view");
    rendered.rerender(wrap(<SkillSection vault="my-v" doc={{ ...DOC, content: "New remote guide", current_commit: "new-remote-revision" }} canManage />));
    await openGuideOption(user, "Agent view");
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
    expect(screen.getByText("Guide changed elsewhere")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    expect(updateDocument).toHaveBeenCalledWith("my-v", "overview/vault-skill.md", {
      content: `${DOC.content} local draft`, expected_commit: DOC.current_commit,
    });
  });

  it("asks before Cancel discards a dirty draft and lets the user keep editing", async () => {
    const user = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    const dialog = screen.getByRole("dialog", { name: "Discard unsaved guide changes?" });
    await user.click(within(dialog).getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit guide" })).toHaveFocus());
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(DOC.content);
    expect(updateDocument).not.toHaveBeenCalled();
  });

  it("restores focus after closing an unchanged inline editor", async () => {
    const user = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Vault guide body")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit guide" })).toHaveFocus());
  });

  it("reports pending template restoration as a workspace write", async () => {
    const user = userEvent.setup();
    let finish!: (value: string) => void;
    getSkillTemplate.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    updateDocument.mockResolvedValueOnce({ current_commit: "reset-revision" });
    const stateChange = vi.fn();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage onDraftStateChange={stateChange} />));
    await openGuideOption(user, "Reset to template");
    await user.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(stateChange).toHaveBeenLastCalledWith({ dirty: false, saving: true }));
    expect(screen.getByRole("button", { name: "Guide options", hidden: true })).toBeDisabled();
    await act(async () => { finish("# {vault} guide"); });
    await waitFor(() => expect(stateChange).toHaveBeenLastCalledWith({ dirty: false, saving: false }));
    expect(screen.getByText("Guide saved")).toBeVisible();
  });

  it("guards participating workspace navigation even from the expanded preview", async () => {
    const user = userEvent.setup();
    function DestinationLink() {
      const { requestNavigation } = useResourceNavigation();
      return <Link to="/elsewhere" onClick={(event) => { if (!requestNavigation("/elsewhere")) event.preventDefault(); }}>Leave guide</Link>;
    }
    const queryClient = new QueryClient();
    render(<QueryClientProvider client={queryClient}><MemoryRouter><ResourceNavigationProvider>
      <DestinationLink />
      <Routes><Route path="/" element={<SkillSection vault="my-v" doc={DOC} canManage />} /><Route path="/elsewhere" element={<h1>Other workspace</h1>} /></Routes>
    </ResourceNavigationProvider></MemoryRouter></QueryClientProvider>);
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " draft");
    await openGuideOption(user, "Agent view");
    await user.click(screen.getByRole("link", { name: "Leave guide" }));
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.queryByRole("heading", { name: "Other workspace" })).toBeNull();
    await user.click(screen.getByRole("link", { name: "Leave guide" }));
    await user.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(await screen.findByRole("heading", { name: "Other workspace" })).toBeVisible();
  });

  it("keeps a saving buffer across preview switches and disables Cancel", async () => {
    let finishSave!: (saved: { current_commit: string }) => void;
    updateDocument.mockReturnValueOnce(new Promise((resolve) => { finishSave = resolve; }));
    const user = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " draft");
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await openGuideOption(user, "Agent view");
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} draft`);
    finishSave({ current_commit: "saved-revision" });
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Vault guide body" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Edit guide" })).toBeVisible();
  });

  it("shows a recoverable load error without calling the guide missing", async () => {
    const retry = vi.fn();
    const user = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" error="503 Service unavailable" onRetry={retry} canManage />));
    expect(screen.getByText("Couldn't load the vault guide")).toBeVisible();
    expect(screen.queryByText(/restored automatically/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledOnce();
  });
  it("renders the editor surface for a normal vault with a guide", async () => {
    // This case checks the ready guide, not lazy loading. Resolve the real
    // preview so cold module transforms do not consume findByText's budget.
    await import("../guide-preview");
    const u = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));

    // Anchor for the canonical guide-management section.
    expect(document.getElementById("skill")).toBeTruthy();
    const sectionHeader = screen.getByRole("heading", { name: "Vault guide" });
    expect(sectionHeader).toHaveClass("text-ui", "font-semibold");
    expect(sectionHeader.nextElementSibling).toHaveAttribute("data-slot", "settings-group");
    expect(sectionHeader.nextElementSibling).not.toHaveClass("border-border-strong");
    expect(sectionHeader).not.toHaveClass("bg-surface-2/55");
    expect(await screen.findByText(/Stored body/)).toBeTruthy();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();

    // History points at the canonical reader pinned to its commit.
    await u.click(screen.getByRole("button", { name: "Guide options" }));
    const history = screen.getByRole("menuitem", { name: /history/i });
    expect(history.getAttribute("href")).toBe(
      "/vault/my-v/doc/overview%2Fvault-skill.md?commit=abc123def456",
    );

    await u.keyboard("{Escape}");
    // Inline editing seeds from the canonical doc body.
    await u.click(screen.getByRole("button", { name: "Edit guide" }));
    expect(
      (screen.getByLabelText(/vault guide body/i) as HTMLTextAreaElement).value,
    ).toBe(DOC.content);
  });

  it("saves the edited body with an optimistic-concurrency pin", async () => {
    updateDocument.mockResolvedValue({ ok: true });
    const u = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));

    await u.click(screen.getByRole("button", { name: "Edit guide" }));
    await u.type(screen.getByLabelText(/vault guide body/i), " more");
    await u.click(screen.getByRole("button", { name: /save guide/i }));

    await waitFor(() =>
      expect(updateDocument).toHaveBeenCalledWith(
        "my-v",
        "overview/vault-skill.md",
        {
          content: `${DOC.content} more`,
          expected_commit: DOC.current_commit,
        },
      ),
    );
  });

  it("keeps the draft when a concurrent update rejects the commit pin", async () => {
    updateDocument.mockRejectedValue(
      new Error("current_commit moved: expected abc123, actual def456"),
    );
    const u = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));

    await u.click(screen.getByRole("button", { name: "Edit guide" }));
    const editor = screen.getByLabelText(
      /vault guide body/i,
    ) as HTMLTextAreaElement;
    await u.type(editor, " local draft");
    await u.click(screen.getByRole("button", { name: /save guide/i }));

    expect(await screen.findByText(/guide changed elsewhere/i)).toBeTruthy();
    expect(screen.getByText(/your draft is still here/i)).toBeTruthy();
    expect(screen.queryByText(/current_commit moved/i)).toBeNull();
    expect(screen.queryByText(/abc123|def456/i)).toBeNull();
    expect(editor.value).toBe(`${DOC.content} local draft`);
    expect(screen.getByRole("button", { name: "Save guide" })).toBeEnabled();
  });

  it("Reset opens the ConfirmDialog and writes the substituted template", async () => {
    getSkillTemplate.mockResolvedValue("# {vault} Vault Skill\n\nBody");
    updateDocument.mockResolvedValue({ ok: true });
    const u = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));

    await openGuideOption(u, "Reset to template");
    expect(await screen.findByText(/replace current content/i)).toBeTruthy();
    await u.click(screen.getByRole("button", { name: /^reset$/i }));

    await waitFor(() =>
      expect(updateDocument).toHaveBeenCalledWith(
        "my-v",
        "overview/vault-skill.md",
        {
          content: "# my-v Vault Skill\n\nBody",
          expected_commit: DOC.current_commit,
        },
      ),
    );
  });

  it("hides Edit and Reset for a reader but keeps preview and read options", async () => {
    const user = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage={false} />));

    expect(screen.queryByRole("button", { name: "Edit guide" })).toBeNull();
    expect(
      screen.queryByRole("menuitem", { name: /reset to template/i }),
    ).toBeNull();
    // Expanded preview, Agent view and History are reads — a reader keeps them.
    expect(await screen.findByText(/Stored body/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open guide" })).toHaveAttribute("href", "/vault/my-v/doc/overview%2Fvault-skill.md");
    await user.click(screen.getByRole("button", { name: "Guide options" }));
    expect(screen.getByRole("menuitem", { name: /agent view/i })).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: /history/i })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: /reset to template/i })).toBeNull();
  });

  it("renders the mirror note instead of the editor for an external-git vault", () => {
    render(
      wrap(<SkillSection vault="mirror-v" doc={null} isMirror canManage />),
    );
    expect(
      screen.getByText(/mirror uses instructions from its external git repository/i),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit guide" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /reset to template/i }),
    ).toBeNull();
  });

  it("renders the backfill note (and no create button) when the guide is missing", () => {
    render(wrap(<SkillSection vault="my-v" doc={null} canManage />));
    expect(
      screen.getByText(/restored automatically by the system backfill/i),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /create/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit guide" })).toBeNull();
  });
});
