import type { Meta, StoryObj } from "@storybook/react-vite";
import { Archive, Download, Plus, Trash2 } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Panel, PanelHeader } from "@/components/ui/panel";

const meta = {
  title: "UI/Buttons, badges, alerts",
  parameters: {
    layout: "padded",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const badgeVariants = [
  "default",
  "secondary",
  "outline",
  "destructive",
  "success",
  "warning",
  "success-solid",
  "warning-solid",
  "info-solid",
  "info",
  "spark",
  "info-outline",
  "owner",
  "admin",
  "writer",
  "reader",
  "active",
  "draft",
  "archived",
  "pending",
  "syncing",
  "error",
] as const;

export const ButtonStates: Story = {
  render: () => (
    <main className="mx-auto max-w-5xl space-y-5 p-6">
      <Panel>
        <PanelHeader label="Button variants" />
        <div className="flex flex-wrap items-center gap-3 p-5">
          <Button><Plus className="h-4 w-4" aria-hidden />Create vault</Button>
          <Button variant="accent"><Plus className="h-4 w-4" aria-hidden />New document</Button>
          <Button variant="outline"><Download className="h-4 w-4" aria-hidden />Export</Button>
          <Button variant="secondary"><Archive className="h-4 w-4" aria-hidden />Archive</Button>
          <Button variant="ghost">Ghost action</Button>
          <Button variant="destructive"><Trash2 className="h-4 w-4" aria-hidden />Delete</Button>
          <Button variant="link">Inline link</Button>
        </div>
      </Panel>
      <Panel>
        <PanelHeader label="Size, loading, disabled" />
        <div className="flex flex-wrap items-center gap-3 p-5">
          <Button size="sm">Small</Button>
          <Button size="md">Medium</Button>
          <Button size="lg">Large</Button>
          <Button size="icon" aria-label="Create"><Plus className="h-4 w-4" aria-hidden /></Button>
          <Button loading>Saving</Button>
          <Button disabled>Disabled</Button>
        </div>
      </Panel>
    </main>
  ),
};

export const BadgeMatrix: Story = {
  render: () => (
    <div className="mx-auto max-w-5xl p-6">
      <Panel>
        <PanelHeader label="Badge variants" count={badgeVariants.length} />
        <div className="flex flex-wrap gap-2 p-5">
          {badgeVariants.map((variant) => (
            <Badge key={variant} variant={variant}>{variant}</Badge>
          ))}
        </div>
      </Panel>
    </div>
  ),
};

export const AlertStates: Story = {
  render: () => (
    <div className="mx-auto grid max-w-4xl gap-3 p-6">
      <Alert variant="info" title="Indexing is catching up">
        Search may temporarily return sparse results while vectors backfill.
      </Alert>
      <Alert variant="success" title="Vault guide saved">
        Agents will now receive updated vault instructions.
      </Alert>
      <Alert variant="warning" title="Archive pending">
        Archived vaults stay readable but hide write actions.
      </Alert>
      <Alert variant="destructive" title="Delete failed">
        The backend rejected the request because the vault still has members.
      </Alert>
    </div>
  ),
};
