import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SkillSection } from "../skill-section";
import { ResourceNavigationProvider, useResourceNavigation } from "@/contexts/resource-navigation-context";
import { CurrentUserProvider } from "@/contexts/current-user-context";
import { VAULT_SKILL_PATH } from "@/lib/skill";
import { ApiError } from "@/lib/api";

const getSkillTemplate = vi.fn();
const getVaultSkillPreview = vi.fn();
const updateDocument = vi.fn();
const getDocument = vi.fn();
const getDocumentHead = vi.fn();

vi.mock("@/lib/api", async original => ({
  ...await original<typeof import("@/lib/api")>(),
  getSkillTemplate: (...a: unknown[]) => getSkillTemplate(...a),
  getVaultSkillPreview: (...a: unknown[]) => getVaultSkillPreview(...a),
  updateDocument: (...a: unknown[]) => updateDocument(...a),
  getDocument: (...a: unknown[]) => getDocument(...a),
  getDocumentHead: (...a: unknown[]) => getDocumentHead(...a),
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

function headAt(commit: string) {
  return { kind: "document_head", uri: "akb://my-v/coll/overview/doc/vault-skill.md", vault: "my-v", path: VAULT_SKILL_PATH,
    current_commit: commit, updated_at: DOC.updated_at };
}

beforeEach(() => {
  vi.clearAllMocks();
  updateDocument.mockReset();
  getDocument.mockReset().mockResolvedValue({ content: "New remote guide", current_commit: "new-remote-revision" });
  getDocumentHead.mockReset().mockResolvedValue(headAt(DOC.current_commit));
  getVaultSkillPreview.mockResolvedValue("composed agent text");
});

async function openGuideOption(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole("button", { name: "Guide options" }));
  await user.click(screen.getByRole("menuitem", { name }));
}

describe("SkillSection", () => {
  it("preserves a reviewed guide and its revision through same-owner access verification", async () => {
    const user = userEvent.setup();
    getDocumentHead.mockResolvedValue(headAt("new-remote-revision"));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const account = { user_id: "a", username: "a", email: "a@x.test", display_name: null, is_admin: false, auth_method: "local", key_class: null };
    const view = (checking = false, revision = 0, canManage = !checking) => <QueryClientProvider client={client}><MemoryRouter><CurrentUserProvider user={account} checking={checking} revision={revision}><SkillSection vault="my-v" doc={DOC} canManage={canManage} /></CurrentUserProvider></MemoryRouter></QueryClientProvider>;
    const rendered = render(view());
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: "Compare changes" }));
    await user.type(await screen.findByRole("textbox", { name: "Combined document body" }), " manually composed guide");
    rendered.rerender(view(true));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    rendered.rerender(view(false, 1, false));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    rendered.rerender(view(false, 1));
    expect(await screen.findByRole("textbox", { name: "Combined document body" })).toHaveValue("New remote guide manually composed guide");
    expect(getDocument).toHaveBeenCalledOnce();
    updateDocument.mockResolvedValueOnce({ current_commit: "saved-revision" });
    await user.click(screen.getByRole("button", { name: "Apply combined changes" }));
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    expect(updateDocument).toHaveBeenCalledExactlyOnceWith("my-v", VAULT_SKILL_PATH, {
      content: "New remote guide manually composed guide", expected_commit: "new-remote-revision",
    });
  });

  it("keeps a write denial through reader-head success until a new owner proof", async () => {
    const user = userEvent.setup();
    const recheck = vi.fn();
    window.addEventListener("akb:revalidate-access", recheck);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const account = { user_id: "a", username: "a", email: "a@x.test", display_name: null, is_admin: false, auth_method: "local", key_class: null };
    const view = (checking = false, revision = 0, canManage = !checking) => <QueryClientProvider client={client}><MemoryRouter><CurrentUserProvider user={account} checking={checking} revision={revision}><SkillSection vault="my-v" doc={DOC} canManage={canManage} /></CurrentUserProvider></MemoryRouter></QueryClientProvider>;
    const rendered = render(view());
    try {
      await user.click(screen.getByRole("button", { name: "Edit guide" }));
      await user.type(screen.getByLabelText("Vault guide body"), " local draft");
      updateDocument.mockRejectedValueOnce(new ApiError("Owner permission required", 403, null));
      let finishHead!: (head: ReturnType<typeof headAt>) => void;
      getDocumentHead.mockReturnValueOnce(new Promise(resolve => { finishHead = resolve; }));
      await user.click(screen.getByRole("button", { name: "Save guide" }));
      await screen.findByText("Guide unavailable");
      await act(async () => { finishHead(headAt(DOC.current_commit)); });
      expect(screen.getByRole("button", { name: "Save guide" })).toBeDisabled();
      expect(recheck).toHaveBeenCalledOnce();
      expect(updateDocument).toHaveBeenCalledOnce();
      // A role prop update alone cannot stand in for a new access proof.
      rendered.rerender(view(false, 0, false));
      rendered.rerender(view(false, 0));
      expect(screen.getByRole("button", { name: "Save guide" })).toBeDisabled();
      rendered.rerender(view(true));
      rendered.rerender(view(false, 1, false));
      expect(screen.queryByRole("button", { name: "Save guide" })).not.toBeInTheDocument();
      rendered.rerender(view(false, 2));
      await waitFor(() => expect(screen.getByRole("button", { name: "Save guide" })).toBeEnabled());
      expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
      expect(updateDocument).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener("akb:revalidate-access", recheck);
    }
  });

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

  it("keeps dirty content and its revision pin across Preview and Agent view", async () => {
    const user = userEvent.setup();
    updateDocument.mockResolvedValue({ current_commit: "saved-revision" });
    const rendered = render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: "Preview guide" }));
    rendered.rerender(wrap(<SkillSection vault="my-v" doc={{ ...DOC, content: "New remote guide", current_commit: "new-remote-revision" }} canManage />));
    await openGuideOption(user, "Agent view");
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
    expect(screen.getByText("Guide changed elsewhere")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    expect(updateDocument).not.toHaveBeenCalled();
    const comparison = screen.getByRole("dialog", { name: "Review document changes" });
    expect(within(comparison).getByText(DOC.content, { selector: "pre", normalizer: text => text })).toBeVisible();
    expect(within(comparison).getByText(`${DOC.content} local draft`, { selector: "pre", normalizer: text => text })).toBeVisible();
    expect(await within(comparison).findByText("New remote guide", { selector: "pre" })).toBeVisible();
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
    await user.click(screen.getByRole("button", { name: "Preview guide" }));
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
    await user.click(screen.getByRole("button", { name: "Preview guide" }));
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
    const u = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));

    // Anchor for the #skill redirect target.
    expect(document.getElementById("skill")).toBeTruthy();
    const sectionHeader = screen.getByRole("heading", { name: "Vault guide" });
    expect(sectionHeader).toHaveClass("text-base", "font-semibold");
    expect(sectionHeader.nextElementSibling).toHaveAttribute("data-slot", "settings-group");
    expect(sectionHeader.nextElementSibling).toHaveClass("border-border-strong");
    expect(sectionHeader).not.toHaveClass("bg-surface-2/55");
    expect(screen.getByText(/Stored body/)).toBeTruthy();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();

    // History points at the canonical doc pinned to its commit — the one URL
    // the viewer does NOT bounce back to settings.
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

  it("observes only the open editor and requires body review before saving against the displayed latest commit", async () => {
    const user = userEvent.setup();
    updateDocument.mockResolvedValue({ current_commit: "saved-combination" });
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    expect(getDocumentHead).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Preview guide" }));
    expect(getDocumentHead).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    expect(getDocumentHead).toHaveBeenCalledWith("my-v", VAULT_SKILL_PATH, expect.any(AbortSignal));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    getDocumentHead.mockResolvedValue(headAt("new-remote-revision"));
    await user.click(screen.getByRole("button", { name: "Check for changes" }));
    expect(await screen.findByText("Guide changed elsewhere")).toBeVisible();
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
    expect(getDocument).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Save guide" }));
    const dialog = screen.getByRole("dialog", { name: "Review document changes" });
    expect(updateDocument).not.toHaveBeenCalled();
    expect(getDocument).toHaveBeenCalledWith("my-v", VAULT_SKILL_PATH, undefined,
      expect.objectContaining({ preserveSession: true, signal: expect.any(AbortSignal) }));
    expect(await within(dialog).findByText("New remote guide", { selector: "pre" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Apply combined changes" })).toBeDisabled();
    const result = within(dialog).getByRole("textbox", { name: "Combined document body" });
    expect(result).toHaveValue("New remote guide");
    await user.clear(result);
    await user.type(result, "New remote guide with my local draft");
    await user.click(within(dialog).getByRole("button", { name: "Apply combined changes" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Vault guide body")).toHaveValue("New remote guide with my local draft");
    expect(updateDocument).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    expect(updateDocument).toHaveBeenCalledExactlyOnceWith("my-v", VAULT_SKILL_PATH, {
      content: "New remote guide with my local draft", expected_commit: "new-remote-revision",
    });
  });

  it("preserves the draft when loading the comparison fails and supports an explicit retry", async () => {
    const user = userEvent.setup();
    getDocumentHead.mockResolvedValue(headAt("new-remote-revision"));
    getDocument.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    const dialog = screen.getByRole("dialog", { name: "Review document changes" });
    expect(await within(dialog).findByText(/latest guide could not be loaded/i)).toBeVisible();
    expect(within(dialog).getByText(`${DOC.content} local draft`, { selector: "pre", normalizer: text => text })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Apply combined changes" })).toBeDisabled();
    expect(updateDocument).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Try loading latest" }));
    expect(await screen.findByRole("textbox", { name: "Combined document body" })).toHaveValue("New remote guide");
    expect(getDocument).toHaveBeenCalledTimes(2);
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
    expect(updateDocument).not.toHaveBeenCalled();
  });

  it.each([401, 403, 404])("blocks saving after a head check returns %s and keeps the draft for recovery", async status => {
    const user = userEvent.setup();
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    getDocumentHead.mockRejectedValueOnce(new ApiError("Unavailable", status, null));
    await user.click(screen.getByRole("button", { name: "Check for changes" }));
    expect(await screen.findByText(/draft is preserved; saving is disabled/i)).toBeVisible();
    expect(screen.getByRole("button", { name: "Save guide" })).toBeDisabled();
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
    expect(updateDocument).not.toHaveBeenCalled();

    getDocumentHead.mockRejectedValueOnce(new ApiError("Unavailable", 503, null));
    await user.click(screen.getByRole("button", { name: "Check for changes" }));
    expect(screen.getByRole("button", { name: "Save guide" })).toBeDisabled();
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
    await user.click(screen.getByRole("button", { name: "Check for changes" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Save guide" })).toBeEnabled());
    expect(screen.queryByText("Guide unavailable")).not.toBeInTheDocument();
  });

  it.each([401, 403, 404])("preserves a combined guide through head status %s and rechecks access before applying", async status => {
    const user = userEvent.setup();
    getDocumentHead.mockResolvedValue(headAt("new-remote-revision"));
    updateDocument.mockResolvedValue({ current_commit: "saved-revision" });
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: "Compare changes" }));
    const dialog = screen.getByRole("dialog", { name: "Review document changes" });
    const body = await within(dialog).findByRole("textbox", { name: "Combined document body" });
    await user.type(body, " combined guide");
    getDocumentHead.mockRejectedValueOnce(new ApiError("Unavailable", status, null));
    await act(async () => { window.dispatchEvent(new Event("online")); });
    expect(body).toHaveValue("New remote guide combined guide");
    expect(within(dialog).getByRole("button", { name: "Apply combined changes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save guide", hidden: true })).toBeDisabled();
    getDocumentHead.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await user.click(within(dialog).getByRole("button", { name: "Check access" }));
    expect(within(dialog).getByRole("button", { name: "Apply combined changes" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Check access" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Apply combined changes" })).toBeEnabled());
    expect(body).toHaveValue("New remote guide combined guide");
    await user.click(within(dialog).getByRole("button", { name: "Apply combined changes" }));
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    expect(updateDocument).toHaveBeenCalledExactlyOnceWith("my-v", VAULT_SKILL_PATH, {
      content: "New remote guide combined guide", expected_commit: "new-remote-revision",
    });
  });

  it("allows a conditional save after a transient head failure", async () => {
    const user = userEvent.setup();
    updateDocument.mockResolvedValue({ current_commit: "saved-revision" });
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    getDocumentHead.mockRejectedValueOnce(new ApiError("Unavailable", 503, null));
    await user.click(screen.getByRole("button", { name: "Check for changes" }));
    expect(await screen.findByText("Could not check for changes")).toBeVisible();
    expect(screen.getByRole("button", { name: "Save guide" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    expect(updateDocument).toHaveBeenCalledExactlyOnceWith("my-v", VAULT_SKILL_PATH, {
      content: `${DOC.content} local draft`, expected_commit: DOC.current_commit,
    });
  });

  it("keeps the displayed server snapshot immutable while polling sees another revision", async () => {
    const user = userEvent.setup();
    getDocumentHead.mockResolvedValue(headAt("new-remote-revision"));
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: "Compare changes" }));
    const dialog = screen.getByRole("dialog", { name: "Review document changes" });
    const result = await within(dialog).findByRole("textbox", { name: "Combined document body" });
    await user.clear(result);
    await user.type(result, "Reviewed combined guide");
    getDocumentHead.mockResolvedValue(headAt("newer-racing-revision"));
    act(() => window.dispatchEvent(new Event("online")));
    expect(await within(dialog).findByText(/server changed again/i)).toBeVisible();
    expect(result).toHaveValue("Reviewed combined guide");
    expect(within(dialog).getByText("new-remote-revision")).toBeVisible();
    expect(within(dialog).queryByText("newer-racing-revision")).not.toBeInTheDocument();

    // The new base starts a fresh observation. A racing writer is still caught
    // by the conditional write against exactly the displayed revision.
    getDocumentHead.mockReturnValueOnce(new Promise(() => {}));
    await user.click(within(dialog).getByRole("button", { name: "Apply combined changes" }));
    updateDocument.mockRejectedValueOnce(new ApiError("Revision conflict", 409, { code: "conflict" }));
    getDocument.mockResolvedValueOnce({ content: "Another remote guide", current_commit: "newer-racing-revision" });
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    expect(updateDocument).toHaveBeenCalledExactlyOnceWith("my-v", VAULT_SKILL_PATH, {
      content: "Reviewed combined guide", expected_commit: "new-remote-revision",
    });
    const next = await screen.findByRole("dialog", { name: "Review document changes" });
    expect(await within(next).findByText("Another remote guide", { selector: "pre" })).toBeVisible();
    expect(within(next).getByText("Reviewed combined guide")).toBeVisible();
    expect(updateDocument).toHaveBeenCalledTimes(1);
  });

  it("discards a late comparison response after the user keeps editing", async () => {
    const user = userEvent.setup();
    let finish!: (value: { content: string; current_commit: string }) => void;
    getDocumentHead.mockResolvedValue(headAt("new-remote-revision"));
    getDocument.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    const signal = getDocument.mock.calls[0][3].signal as AbortSignal;
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(signal.aborted).toBe(true);
    await act(async () => finish({ content: "Ignored late body", current_commit: "ignored-revision" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
    expect(updateDocument).not.toHaveBeenCalled();
  });

  it("discards a comparison read across an access-proof revision without discarding the draft", async () => {
    const user = userEvent.setup();
    const account = { user_id: "a", username: "a", email: "a@x.test", display_name: null, is_admin: false, auth_method: "local", key_class: null };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = (checking = false, revision = 0) => <QueryClientProvider client={client}><MemoryRouter>
      <CurrentUserProvider user={account} checking={checking} revision={revision}>
        <SkillSection vault="my-v" doc={DOC} canManage={!checking} />
      </CurrentUserProvider>
    </MemoryRouter></QueryClientProvider>;
    let finish!: (value: { content: string; current_commit: string }) => void;
    getDocumentHead.mockResolvedValue(headAt("new-remote-revision"));
    getDocument.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const { rerender } = render(view());
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: "Compare changes" }));
    expect(screen.getByRole("dialog", { name: "Review document changes" })).toBeVisible();
    const signal = getDocument.mock.calls[0][3].signal as AbortSignal;

    rerender(view(true));
    expect(signal.aborted).toBe(true);
    await act(async () => finish({ content: "Ignored old proof body", current_commit: "old-proof-revision" }));
    rerender(view(false, 1));
    const comparison = screen.getByRole("dialog", { name: "Review document changes" });
    expect(within(comparison).queryByText("Ignored old proof body")).not.toBeInTheDocument();
    expect(within(comparison).getByRole("button", { name: "Apply combined changes" })).toBeDisabled();
    expect(within(comparison).getByRole("button", { name: "Try loading latest" })).toBeEnabled();
    await user.click(within(comparison).getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
    expect(updateDocument).not.toHaveBeenCalled();
  });

  it("blocks a terminal comparison failure until an explicit retry verifies the latest guide", async () => {
    const user = userEvent.setup();
    getDocumentHead.mockResolvedValue(headAt("new-remote-revision"));
    getDocument.mockRejectedValueOnce(new ApiError("Forbidden", 403, null));
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: "Compare changes" }));
    const dialog = screen.getByRole("dialog", { name: "Review document changes" });
    expect(await within(dialog).findByText(/latest guide could not be loaded/i)).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Try loading latest" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Check access" }));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Try loading latest" })).toBeEnabled());
    await user.click(within(dialog).getByRole("button", { name: "Try loading latest" }));
    const result = await screen.findByRole("textbox", { name: "Combined document body" });
    await user.clear(result);
    await user.type(result, "Reviewed after access recovered");
    await user.click(screen.getByRole("button", { name: "Apply combined changes" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Vault guide body")).toHaveValue("Reviewed after access recovered");
    expect(screen.getByRole("button", { name: "Save guide" })).toBeEnabled();
    expect(updateDocument).not.toHaveBeenCalled();
  });

  it("does not treat a title conflict as a revision conflict", async () => {
    const user = userEvent.setup();
    updateDocument.mockRejectedValueOnce(new ApiError("Duplicate title", 409, { code: "title_conflict" }));
    render(wrap(<SkillSection vault="my-v" doc={DOC} canManage />));
    await user.click(screen.getByRole("button", { name: "Edit guide" }));
    await user.type(screen.getByLabelText("Vault guide body"), " local draft");
    await user.click(screen.getByRole("button", { name: "Save guide" }));
    expect(await screen.findByText("Duplicate title")).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(getDocument).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Vault guide body")).toHaveValue(`${DOC.content} local draft`);
  });

  it("keeps the draft when a concurrent update rejects the commit pin", async () => {
    updateDocument.mockRejectedValue(
      new ApiError("current_commit moved: expected abc123, actual def456", 409, { code: "conflict" }),
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
    expect(await screen.findByRole("dialog", { name: "Review document changes" })).toBeVisible();
    expect(screen.queryByText(/current_commit moved/i)).toBeNull();
    expect(editor.value).toBe(`${DOC.content} local draft`);
    await u.click(screen.getByRole("button", { name: "Keep editing" }));
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
    expect(screen.getByText(/Stored body/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Preview guide" })).toBeTruthy();
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
