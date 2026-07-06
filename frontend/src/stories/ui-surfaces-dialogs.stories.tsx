import { useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { Download, ExternalLink, History, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CodeSnippet } from "@/components/ui/code-snippet";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { CopyButton } from "@/components/ui/copy-button";
import { PageHeader } from "@/components/ui/page-header";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { RelativeTime } from "@/components/ui/relative-time";
import { Skeleton } from "@/components/ui/skeleton";
import { StatTile } from "@/components/ui/stat-tile";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TooltipText } from "@/components/ui/tooltip-text";
import { VaultChip } from "@/components/ui/vault-chip";

const meta = {
  title: "UI/Surfaces and dialogs",
  parameters: {
    layout: "padded",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const now = Date.now();
const iso = (minutesAgo: number) => new Date(now - minutesAgo * 60_000).toISOString();

function ConfirmHarness({ reject = false, destructive = false }: { reject?: boolean; destructive?: boolean }) {
  const [open, setOpen] = useState(true);
  return (
    <>
      <Button onClick={() => setOpen(true)}>Open dialog</Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={destructive ? "Delete vault permanently?" : "Publish this document?"}
        description={
          destructive
            ? "This removes documents, tables, files, git history, and member grants."
            : "Public readers will be able to open the latest rendered version."
        }
        confirmLabel={destructive ? "Delete vault" : "Publish"}
        variant={destructive ? "destructive" : "default"}
        onConfirm={async () => {
          if (reject) throw new Error("The backend rejected this operation.");
        }}
      />
    </>
  );
}

export const PageAndPanelComposition: Story = {
  render: () => (
    <main className="mx-auto max-w-6xl p-6">
      <PageHeader
        eyebrow="Vault overview"
        title="akb-platform"
        subtitle="A compact route header with one marquee action and quiet supporting commands."
        actions={(
          <>
            <Button variant="outline"><History className="h-4 w-4" aria-hidden />Activity</Button>
            <Button variant="accent"><Plus className="h-4 w-4" aria-hidden />New document</Button>
          </>
        )}
      />
      <div className="grid gap-3 md:grid-cols-4">
        <StatTile label="Documents" value={214} kind="indexed" />
        <StatTile label="Tables" value={12} kind="structured" />
        <StatTile label="Files" value={38} kind="binary" />
        <StatTile label="Pending" value={0} kind="backfill" dimZero />
      </div>
      <Panel className="mt-6">
        <PanelHeader
          label="Recent resources"
          count={3}
          right={<Button size="sm" variant="outline"><ExternalLink className="h-4 w-4" aria-hidden />Open</Button>}
        />
        <ol className="divide-y divide-border">
          {[
            ["akb", "overview/vault-skill.md", iso(8)],
            ["research", "papers/rag-eval.md", iso(180)],
            ["ops", "runbooks/deploy.md", iso(3600)],
          ].map(([vault, path, timestamp]) => (
            <li key={path} className="flex items-center gap-3 px-4 py-3">
              <VaultChip name={vault} />
              <TooltipText className="min-w-0 flex-1 truncate text-sm font-medium" tip={path}>{path}</TooltipText>
              <RelativeTime iso={timestamp} className="shrink-0" />
              <CopyButton value={`akb://${vault}/${path}`} label="Copy URI" />
            </li>
          ))}
        </ol>
      </Panel>
    </main>
  ),
};

export const TabsAndLoading: Story = {
  render: () => (
    <main className="mx-auto max-w-4xl p-6">
      <Tabs defaultValue="rendered">
        <TabsList>
          <TabsTrigger value="rendered">Rendered</TabsTrigger>
          <TabsTrigger value="raw">Raw</TabsTrigger>
          <TabsTrigger value="agent">Agent</TabsTrigger>
        </TabsList>
        <TabsContent value="rendered">
          <Panel className="p-5">
            <h2 className="text-lg font-semibold text-foreground">Document preview</h2>
            <p className="mt-2 text-sm leading-relaxed text-foreground-muted">
              A rendered markdown panel with predictable spacing and focus behavior.
            </p>
          </Panel>
        </TabsContent>
        <TabsContent value="raw">
          <CodeSnippet filename="overview/vault-skill.md" code={"# Vault guide\n\nUse AKB tokens and quote akb:// URIs."} />
        </TabsContent>
        <TabsContent value="agent">
          <div className="space-y-2 rounded-[var(--radius-lg)] border border-border bg-surface p-5">
            <Skeleton className="h-4 w-2/3 rounded-[var(--radius-sm)]" />
            <Skeleton className="h-4 w-full rounded-[var(--radius-sm)]" />
            <Skeleton className="h-4 w-5/6 rounded-[var(--radius-sm)]" />
          </div>
        </TabsContent>
      </Tabs>
    </main>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(canvas.getByRole("tab", { name: "Raw" }));
    await expect(canvas.getByText((text) => text.includes("# Vault guide"))).toBeVisible();
  },
};

export const CopyAndCode: Story = {
  render: () => (
    <main className="mx-auto max-w-3xl space-y-4 p-6">
      <CodeSnippet
        filename="codex mcp add"
        code={"codex mcp add akb -- akb-mcp --url http://localhost:8000/mcp --token $AKB_TOKEN"}
      />
      <div className="inline-flex items-center gap-2 rounded-[var(--radius-md)] border border-border bg-surface px-3 py-2">
        <span className="font-mono text-xs text-foreground-muted">akb://akb/overview/vault-skill.md</span>
        <CopyButton value="akb://akb/overview/vault-skill.md" label="Copy AKB URI" />
      </div>
      <Button variant="outline"><Download className="h-4 w-4" aria-hidden />Download file</Button>
    </main>
  ),
};

export const ConfirmDefault: Story = {
  render: () => <ConfirmHarness />,
};

export const ConfirmDestructive: Story = {
  render: () => <ConfirmHarness destructive />,
};

export const ConfirmFailure: Story = {
  render: () => <ConfirmHarness reject />,
  play: async () => {
    await userEvent.click(await within(document.body).findByRole("button", { name: "Publish" }));
    await expect(await within(document.body).findByText("The backend rejected this operation.")).toBeVisible();
  },
};
