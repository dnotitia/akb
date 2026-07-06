import { useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { FileText, FolderOpen, Search } from "lucide-react";
import { DocStatusBadge, IndexingBadge, RoleBadge, VaultStateBadge } from "@/components/status-badge";
import { EmptyState } from "@/components/empty-state";
import { HistoryList, type HistoryEntry } from "@/components/history-list";
import { JsonTree } from "@/components/json-tree";
import { Logo } from "@/components/logo";
import { MarkdownEditorFallback } from "@/components/markdown-editor-fallback";
import { MarkdownRender } from "@/components/markdown-render";
import { PasswordGate } from "@/components/password-gate";
import { SummaryFold } from "@/components/summary-fold";
import { SkillBadge } from "@/components/ui/skill-badge";
import { Button } from "@/components/ui/button";
import { Panel, PanelHeader } from "@/components/ui/panel";

const meta = {
  title: "Components/Status and content",
  parameters: {
    layout: "padded",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const history: HistoryEntry[] = [
  {
    hash: "c0ffee1234567890",
    agent: "codex",
    subject: "Add Storybook scenarios",
    timestamp: new Date(Date.now() - 8 * 60_000).toISOString(),
  },
  {
    hash: "baddad9876543210",
    author: "jylkim",
    subject: "Tune vault settings copy",
    timestamp: new Date(Date.now() - 4 * 60 * 60_000).toISOString(),
  },
  {
    agent: "importer",
    subject: "Legacy entry without hash",
    timestamp: new Date(Date.now() - 2 * 24 * 60 * 60_000).toISOString(),
  },
];

const markdown = [
  "---",
  "title: Storybook markdown",
  "tags: [akb, design]",
  "---",
  "# Storybook markdown",
  "",
  "> [!NOTE]",
  "> AKB callouts use token-backed soft surfaces.",
  "",
  "A rendered document can include `inline code`, [safe links](https://example.com), tables, and math $E=mc^2$.",
  "",
  "| Type | Count |",
  "| --- | ---: |",
  "| Documents | 128 |",
  "| Tables | 12 |",
  "",
  "```ts",
  "const uri = 'akb://akb/overview/vault-skill.md';",
  "```",
].join("\n");

function PasswordHarness() {
  const [unlocked, setUnlocked] = useState(false);
  return unlocked ? (
    <EmptyState title="Publication unlocked" description="The protected content would render here." />
  ) : (
    <PasswordGate slug="private-note" onSuccess={() => setUnlocked(true)} />
  );
}

export const BadgeStates: Story = {
  render: () => (
    <main className="mx-auto grid max-w-5xl gap-5 p-6 md:grid-cols-2">
      <Panel>
        <PanelHeader label="Roles and document status" />
        <div className="flex flex-wrap gap-2 p-5">
          {["owner", "admin", "writer", "reader", "guest"].map((role) => (
            <RoleBadge key={role} role={role} />
          ))}
          {["draft", "active", "archived"].map((status) => (
            <DocStatusBadge key={status} status={status as "draft" | "active" | "archived"} />
          ))}
        </div>
      </Panel>
      <Panel>
        <PanelHeader label="Vault and indexing state" />
        <div className="space-y-3 p-5">
          <VaultStateBadge archived externalGit publicAccess="reader" />
          <VaultStateBadge publicAccess="writer" />
          <div className="flex flex-wrap gap-2">
            <IndexingBadge pending={null} />
            <IndexingBadge pending={12} />
            <IndexingBadge pending={0} abandoned={3} />
          </div>
        </div>
      </Panel>
      <Panel>
        <PanelHeader label="Skill guide state" />
        <div className="flex flex-wrap gap-2 p-5">
          <SkillBadge defined lineCount={42} />
          <SkillBadge defined />
          <SkillBadge defined={false} />
        </div>
      </Panel>
      <Panel>
        <PanelHeader label="Brand lockup" />
        <div className="flex flex-wrap items-center gap-5 p-5">
          <Logo size={28} />
          <Logo size={36} subtitle />
          <Logo size={44} wordmark={false} />
        </div>
      </Panel>
    </main>
  ),
};

export const EmptyAndLoadingStates: Story = {
  render: () => (
    <main className="mx-auto grid max-w-5xl gap-5 p-6 md:grid-cols-3">
      <EmptyState
        icon={<FolderOpen className="h-10 w-10" aria-hidden />}
        title="No vault selected"
        description="Choose a vault from the rail to inspect documents, tables, files, and graph relations."
        action={<Button variant="accent">Create vault</Button>}
      />
      <EmptyState
        icon={<Search className="h-10 w-10" aria-hidden />}
        title="No search results"
        description="Try a broader semantic query or switch to literal mode."
      />
      <div className="rounded-[var(--radius-lg)] border border-border bg-surface p-5">
        <MarkdownEditorFallback />
      </div>
    </main>
  ),
};

export const HistoryStates: Story = {
  render: () => (
    <main className="mx-auto max-w-3xl p-6">
      <Panel>
        <PanelHeader label="History" count={history.length} />
        <div className="p-4">
          <HistoryList
            entries={history}
            selectedHash="baddad9876543210"
            onSelect={() => undefined}
          />
        </div>
      </Panel>
    </main>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(canvas.getByRole("button", { name: /View document at commit c0ffee1/i }));
    await expect(canvas.getByText("codex")).toBeVisible();
  },
};

export const StructuredData: Story = {
  render: () => (
    <main className="mx-auto grid max-w-5xl gap-5 p-6 md:grid-cols-2">
      <Panel>
        <PanelHeader label="JSON metadata" />
        <div className="p-5">
          <JsonTree
            data={{
              id: "d-94d8657f",
              vault: "akb",
              indexed: true,
              score: 0.87,
              tags: ["storybook", "design", "coverage"],
              metadata: {
                role: "writer",
                path: "overview/vault-skill.md",
                links: [{ type: "source", uri: "akb://akb/doc/source" }],
              },
            }}
          />
        </div>
      </Panel>
      <Panel>
        <PanelHeader label="Summary fold" />
        <div className="p-5">
          <SummaryFold
            summary="This autogenerated summary is deliberately long enough to fold. It captures the document's high-level meaning for search relevance, but it should not overpower the actual content in the rendered read view, especially when metadata is dense."
          />
        </div>
      </Panel>
    </main>
  ),
};

export const MarkdownDocument: Story = {
  render: () => (
    <main className="mx-auto max-w-4xl p-6">
      <Panel className="p-6">
        <div className="mb-4 flex items-center gap-2 text-sm font-medium text-foreground">
          <FileText className="h-4 w-4 text-primary" aria-hidden />
          Rendered markdown
        </div>
        <MarkdownRender markdown={markdown} />
      </Panel>
    </main>
  ),
};

export const ProtectedPublicationGate: Story = {
  parameters: {
    authToken: false,
    layout: "fullscreen",
    msw: {
      handlers: [],
    },
  },
  render: () => <PasswordHarness />,
};
