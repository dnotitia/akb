import type { Meta, StoryObj } from "@storybook/react-vite";
import { CheckCircle2, CircleDashed, FlaskConical } from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { CodeSnippet } from "@/components/ui/code-snippet";

const meta = {
  title: "Foundation/Storybook instructions",
  parameters: {
    layout: "padded",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const coverage = [
  ["Foundation", "Design tokens, typography, surfaces, semantic colors, light/dark rendering"],
  ["UI primitives", "Controls, disabled/invalid/loading states, dialogs, tabs, empty/loading patterns"],
  ["Domain components", "Vaults, roles, guide status, markdown, outlines, file/table/document utilities"],
  ["Pages", "Authenticated, unauthenticated, loading, empty, error, permission, and success states"],
  ["Tests", "Storybook build plus Vitest browser render checks for every story"],
] as const;

export const GoalAndCoverage: Story = {
  name: "Goal and coverage rules",
  render: () => (
    <main className="mx-auto max-w-5xl space-y-6 p-6">
      <Panel>
        <PanelHeader
          label="Storybook goal"
          right={<Badge variant="info-outline">AKB FE</Badge>}
        />
        <div className="space-y-4 p-5">
          <Alert variant="info" title="Working agreement">
            Stories should document real AKB interface states, not decorative
            one-offs. Prefer token classes and existing primitives, mirror app
            providers in preview, and mock network/state at the Storybook layer.
          </Alert>
          <div className="grid gap-3 md:grid-cols-2">
            {coverage.map(([label, description]) => (
              <div
                key={label}
                className="rounded-[var(--radius-md)] border border-border bg-surface px-4 py-3"
              >
                <div className="mb-1 flex items-center gap-2 font-medium text-foreground">
                  <CheckCircle2 className="h-4 w-4 text-success" aria-hidden />
                  {label}
                </div>
                <p className="text-sm leading-relaxed text-foreground-muted">{description}</p>
              </div>
            ))}
          </div>
        </div>
      </Panel>
      <Panel>
        <PanelHeader label="Definition of done" count={4} />
        <div className="grid gap-3 p-5 md:grid-cols-2">
          {[
            "Every story renders in light and dark through the toolbar.",
            "Network-dependent stories use MSW, not a live backend.",
            "Interactive controls include at least one play-function smoke path where useful.",
            "pnpm run build-storybook and pnpm run test:storybook pass before shipping.",
          ].map((item) => (
            <div key={item} className="flex gap-2 text-sm text-foreground">
              <CircleDashed className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
              <span>{item}</span>
            </div>
          ))}
        </div>
      </Panel>
      <CodeSnippet
        filename="Storybook setup command surface"
        code={[
          "pnpm storybook",
          "pnpm build-storybook",
          "pnpm test:storybook",
        ].join("\n")}
      />
      <div className="flex items-center gap-2 text-xs text-foreground-muted">
        <FlaskConical className="h-4 w-4" aria-hidden />
        Scenarios expand from low-level primitives outward to full routed pages.
      </div>
    </main>
  ),
};
