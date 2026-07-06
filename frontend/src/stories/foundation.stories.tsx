import type { Meta, StoryObj } from "@storybook/react-vite";
import { Badge } from "@/components/ui/badge";
import { Eyebrow } from "@/components/ui/eyebrow";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { StatTile } from "@/components/ui/stat-tile";

const meta = {
  title: "Foundation/Design tokens",
  parameters: {
    layout: "padded",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const surfaces = [
  ["Background", "bg-background", "text-foreground"],
  ["Surface", "bg-surface", "text-foreground"],
  ["Surface 2", "bg-surface-2", "text-foreground"],
  ["Selected", "bg-surface-selected", "text-surface-selected-foreground"],
] as const;

const semantic = [
  ["Success", "bg-success-soft text-success-soft-foreground border-success/30"],
  ["Warning", "bg-warning-soft text-warning-soft-foreground border-warning/30"],
  ["Info", "bg-info-soft text-info-soft-foreground border-info/30"],
  ["Destructive", "bg-destructive-soft text-destructive-soft-foreground border-destructive/30"],
] as const;

const categories = ["cat-1", "cat-2", "cat-3", "cat-4", "cat-5", "cat-6"] as const;

export const TokenSystem: Story = {
  name: "Token system",
  render: () => (
    <main className="mx-auto max-w-6xl space-y-8 p-6">
      <section>
        <Eyebrow tone="ink" className="mb-3 block">Surfaces</Eyebrow>
        <div className="grid gap-3 md:grid-cols-4">
          {surfaces.map(([label, bg, fg]) => (
            <div
              key={label}
              className={`${bg} ${fg} rounded-[var(--radius-lg)] border border-border p-4 shadow-sm`}
            >
              <div className="font-medium">{label}</div>
              <p className="mt-1 text-sm text-foreground-muted">Token-backed surface sample.</p>
            </div>
          ))}
        </div>
      </section>
      <section>
        <Eyebrow tone="ink" className="mb-3 block">Semantic states</Eyebrow>
        <div className="grid gap-3 md:grid-cols-4">
          {semantic.map(([label, cls]) => (
            <div key={label} className={`${cls} rounded-[var(--radius-md)] border px-4 py-3 text-sm`}>
              <div className="font-semibold">{label}</div>
              <div className="mt-1">Icon/text always accompanies color.</div>
            </div>
          ))}
        </div>
      </section>
      <Panel>
        <PanelHeader label="Categorical ramp" right={<Badge variant="outline">graph + vault identity</Badge>} />
        <div className="grid grid-cols-2 gap-3 p-5 md:grid-cols-6">
          {categories.map((cat) => (
            <div key={cat} className="space-y-2">
              <div
                className="h-14 rounded-[var(--radius-md)] border border-border"
                style={{ backgroundColor: `var(--color-${cat})` }}
              />
              <div className="coord">{cat}</div>
            </div>
          ))}
        </div>
      </Panel>
      <section className="grid gap-3 md:grid-cols-4">
        <StatTile label="Documents" value={128} kind="indexed" />
        <StatTile label="Tables" value={0} kind="empty state" dimZero />
        <StatTile label="Files" value="2.4k" kind="binary assets" />
        <StatTile label="Pending" value={7} kind="backfill" />
      </section>
    </main>
  ),
};
