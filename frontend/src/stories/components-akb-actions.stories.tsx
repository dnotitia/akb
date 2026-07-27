import { useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { http, HttpResponse } from "msw";
import { Link } from "react-router-dom";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { RoleSelect, type MemberLike } from "@/components/role-select";
import { SkillCreateButton } from "@/components/skill/skill-create-button";
import { SkillSettingsLink } from "@/components/skill/skill-settings-link";
import { SkillStatusChip } from "@/components/skill/skill-status-chip";
import { VaultList, type VaultRow } from "@/components/vault-list";

const meta = {
  title: "Components/AKB actions",
  parameters: {
    layout: "padded",
    router: {
      initialEntries: ["/vault/akb/settings"],
    },
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const API = "/api/v1";

function RoleSelectHarness({ fail = false }: { fail?: boolean }) {
  const [member, setMember] = useState<MemberLike>({
    username: fail ? "readonly-user" : "writer-user",
    role: "reader",
  });
  return (
    <div className="flex items-center justify-between gap-4 rounded-[var(--radius-md)] border border-border bg-surface px-4 py-3">
      <div>
        <div className="text-sm font-medium text-foreground">{member.username}</div>
        <div className="coord">member role can be changed inline</div>
      </div>
      <RoleSelect
        vault={fail ? "locked" : "akb"}
        member={member}
        onChanged={(_prev, next) => setMember((m) => ({ ...m, role: next as MemberLike["role"] }))}
      />
      {fail && <span className="sr-only">failure story</span>}
    </div>
  );
}

const vaults: VaultRow[] = [
  {
    id: "v-akb",
    name: "akb",
    description: "Production agent knowledge base with design docs and MCP guides.",
    role: "owner",
  },
  {
    id: "v-research",
    name: "research",
    description: "Papers, eval notes, and experiment snapshots.",
    role: "writer",
  },
  {
    id: "v-archive",
    name: "archive",
    description: "Read-only historical imports.",
    role: "reader",
    status: "archived",
  },
];

const infoByVault: Record<string, unknown> = {
  akb: {
    document_count: 214,
    table_count: 12,
    file_count: 38,
    last_activity: new Date(Date.now() - 12 * 60_000).toISOString(),
  },
  research: {
    document_count: 83,
    table_count: 0,
    file_count: 9,
    last_activity: new Date(Date.now() - 8 * 60 * 60_000).toISOString(),
  },
  archive: {
    document_count: 12,
    table_count: 3,
    file_count: 0,
    last_activity: new Date(Date.now() - 20 * 24 * 60 * 60_000).toISOString(),
  },
};

export const RoleChangeSuccess: Story = {
  parameters: {
    msw: {
      handlers: [
        http.post(`${API}/vaults/akb/grant`, async () =>
          HttpResponse.json({ ok: true }),
        ),
      ],
    },
  },
  render: () => (
    <main className="mx-auto max-w-xl p-6">
      <Panel>
        <PanelHeader label="Role select success" />
        <div className="p-5">
          <RoleSelectHarness />
        </div>
      </Panel>
    </main>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(canvas.getByRole("button", { name: /Change role for writer-user/i }));
    await userEvent.click(await within(document.body).findByRole("menuitemradio", { name: "writer" }));
    await expect(canvas.getByRole("button", { name: /Change role for writer-user/i })).toHaveTextContent("writer");
  },
};

export const RoleChangeFailure: Story = {
  parameters: {
    msw: {
      handlers: [
        http.post(`${API}/vaults/locked/grant`, async () =>
          HttpResponse.json({ detail: "Not allowed" }, { status: 403 }),
        ),
      ],
    },
  },
  render: () => (
    <main className="mx-auto max-w-xl p-6">
      <Panel>
        <PanelHeader label="Role select failure" />
        <div className="p-5">
          <RoleSelectHarness fail />
        </div>
      </Panel>
    </main>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(canvas.getByRole("button", { name: /Change role for readonly-user/i }));
    await userEvent.click(await within(document.body).findByRole("menuitemradio", { name: "writer" }));
    await expect(await canvas.findByRole("alert")).toHaveTextContent("Not allowed");
  },
};

export const SkillEntryPoints: Story = {
  parameters: {
    msw: {
      handlers: [
        http.get(`${API}/skill/template`, async () =>
          HttpResponse.text("# {vault} guide\n\nUse durable AKB instructions."),
        ),
        http.post(`${API}/documents`, async () =>
          HttpResponse.json({ ok: true, id: "d-skill" }),
        ),
      ],
    },
  },
  render: () => (
    <main className="mx-auto max-w-3xl space-y-4 p-6">
      <Panel>
        <PanelHeader label="Skill status chips" />
        <div className="flex flex-wrap items-center gap-3 p-5">
          <SkillStatusChip vault="akb" defined lineCount={42} />
          <SkillStatusChip vault="akb" defined={false} />
        </div>
      </Panel>
      <Panel>
        <PanelHeader label="Settings row" />
        <div className="px-5">
          <SkillSettingsLink
            vault="akb"
            defined
            updatedAt={new Date(Date.now() - 33 * 60_000).toISOString()}
          />
          <SkillSettingsLink vault="research" defined={false} />
        </div>
      </Panel>
      <div className="flex items-center gap-3">
        <SkillCreateButton vault="research" variant="outline" />
        <Button asChild variant="link">
          <Link to="/vault/akb/doc/overview%2Fvault-skill.md">Open guide</Link>
        </Button>
      </div>
    </main>
  ),
};

export const VaultDirectory: Story = {
  parameters: {
    router: {
      initialEntries: ["/"],
    },
    msw: {
      handlers: [
        http.get(`${API}/vaults/:vault/info`, ({ params }) =>
          HttpResponse.json(infoByVault[String(params.vault)] || {}),
        ),
      ],
    },
  },
  render: () => (
    <main className="mx-auto max-w-5xl p-6">
      <Alert variant="info" title="MSW-backed live metrics">
        The directory rows fetch per-vault metrics through mocked /vaults/:name/info endpoints.
      </Alert>
      <VaultList vaults={vaults} />
    </main>
  ),
};

/** VaultList with the optional favorite control — the star is a sibling of the
 *  row link, so clicking it toggles the pin instead of navigating. */
function VaultListFavDemo() {
  const [favs, setFavs] = useState<Set<string>>(new Set());
  return (
    <main className="mx-auto max-w-5xl p-6">
      <VaultList
        vaults={vaults}
        favoriteControl={{
          isFavorite: (id) => favs.has(id),
          onToggle: (v) =>
            setFavs((prev) => {
              const next = new Set(prev);
              if (next.has(v.id)) next.delete(v.id);
              else next.add(v.id);
              return next;
            }),
        }}
      />
    </main>
  );
}

export const VaultDirectoryFavorites: Story = {
  parameters: {
    router: { initialEntries: ["/"] },
    msw: {
      handlers: [
        http.get(`${API}/vaults/:vault/info`, ({ params }) =>
          HttpResponse.json(infoByVault[String(params.vault)] || {}),
        ),
      ],
    },
  },
  render: () => <VaultListFavDemo />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const name = vaults[0].name;
    // Starts unpinned: the accessible name says "Add …", aria-pressed=false.
    const star = canvas.getByRole("button", { name: new RegExp(`Add ${name} to favorites`, "i") });
    await expect(star).toHaveAttribute("aria-pressed", "false");
    // Clicking the star toggles the pin in place (does not navigate — it is a
    // sibling of the row link, not nested inside it).
    await userEvent.click(star);
    const unstar = await canvas.findByRole("button", {
      name: new RegExp(`Remove ${name} from favorites`, "i"),
    });
    await expect(unstar).toHaveAttribute("aria-pressed", "true");
    // Toggling back restores the original state.
    await userEvent.click(unstar);
    await expect(
      canvas.getByRole("button", { name: new RegExp(`Add ${name} to favorites`, "i") }),
    ).toHaveAttribute("aria-pressed", "false");
  },
};
