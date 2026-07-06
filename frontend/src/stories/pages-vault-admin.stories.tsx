import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, within } from "storybook/test";
import { delay, http, HttpResponse } from "msw";
import {
  API,
  archivedVaultInfo,
  defaultVaultInfoHandler,
  storyPublications,
  vaultHealth,
  vaultInfo,
  vaultMembers,
  vaultShellHandlers,
  vaultSkillDoc,
} from "./page-story-fixtures";
import { AkbRouteTree } from "./page-route-shell";

const meta = {
  title: "Pages/Vault Admin",
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const ownerInfo = {
  ...vaultInfo,
  public_access: "none",
  is_archived: false,
};

const readerInfo = {
  ...vaultInfo,
  role: "reader",
  public_access: "reader",
  role_source: "public",
};

const adminShellHandlers = [
  ...vaultShellHandlers,
  defaultVaultInfoHandler,
  http.get("/health/vault/akb", () => HttpResponse.json(vaultHealth)),
];

async function expectVaultShell(canvasElement: HTMLElement) {
  const canvas = within(canvasElement);
  await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
  await expect(await canvas.findByRole("tree", { name: "akb explorer" })).toBeInTheDocument();
}

export const PublicationsList: Story = {
  name: "Publications / list",
  parameters: {
    router: { initialEntries: ["/vault/akb/publications"] },
    msw: {
      handlers: [
        ...adminShellHandlers,
        http.get(`${API}/publications/akb`, () =>
          HttpResponse.json({ publications: storyPublications }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "Published" })).toBeInTheDocument();
    await expect(await canvas.findByText("Storybook rollout")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const PublicationsEmpty: Story = {
  name: "Publications / empty",
  parameters: {
    router: { initialEntries: ["/vault/akb/publications"] },
    msw: {
      handlers: [
        ...adminShellHandlers,
        http.get(`${API}/publications/akb`, () => HttpResponse.json({ publications: [] })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("No publications yet")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const PublicationsLoading: Story = {
  name: "Publications / loading",
  parameters: {
    router: { initialEntries: ["/vault/akb/publications"] },
    msw: {
      handlers: [
        ...adminShellHandlers,
        http.get(`${API}/publications/akb`, async () => {
          await delay("infinite");
          return HttpResponse.json({ publications: [] });
        }),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Loading…")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const PublicationsError: Story = {
  name: "Publications / API error",
  parameters: {
    router: { initialEntries: ["/vault/akb/publications"] },
    msw: {
      handlers: [
        ...adminShellHandlers,
        http.get(`${API}/publications/akb`, () =>
          HttpResponse.json({ detail: "Publications unavailable" }, { status: 503 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Couldn't load publications")).toBeInTheDocument();
    await expect(await canvas.findByText("Publications unavailable")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const MembersOwner: Story = {
  name: "Members / owner management",
  parameters: {
    router: { initialEntries: ["/vault/akb/members"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        http.get(`${API}/vaults/akb/info`, () => HttpResponse.json(ownerInfo)),
        http.get(`${API}/vaults/akb/members`, () => HttpResponse.json({ members: vaultMembers })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "Members" })).toBeInTheDocument();
    await expect(await canvas.findByRole("button", { name: "Invite" })).toBeInTheDocument();
    await expect(await canvas.findByText("Story Writer")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const MembersReadOnly: Story = {
  name: "Members / reader permission",
  parameters: {
    router: { initialEntries: ["/vault/akb/members"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        http.get(`${API}/vaults/akb/info`, () => HttpResponse.json(readerInfo)),
        http.get(`${API}/vaults/akb/members`, () => HttpResponse.json({ members: vaultMembers })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/Roster is read-only/i)).toBeInTheDocument();
    await expect(canvas.queryByRole("button", { name: "Invite" })).not.toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const MembersEmptyEdge: Story = {
  name: "Members / empty edge",
  parameters: {
    router: { initialEntries: ["/vault/akb/members"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        http.get(`${API}/vaults/akb/info`, () => HttpResponse.json(ownerInfo)),
        http.get(`${API}/vaults/akb/members`, () => HttpResponse.json({ members: [] })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("No members on record")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const MembersError: Story = {
  name: "Members / API error",
  parameters: {
    router: { initialEntries: ["/vault/akb/members"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        http.get(`${API}/vaults/akb/info`, () => HttpResponse.json(ownerInfo)),
        http.get(`${API}/vaults/akb/members`, () =>
          HttpResponse.json({ detail: "Member service unavailable" }, { status: 503 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Failed to load members")).toBeInTheDocument();
    await expect(await canvas.findByText("Member service unavailable")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const VaultSettingsOwner: Story = {
  name: "Settings / owner controls",
  parameters: {
    router: { initialEntries: ["/vault/akb/settings"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        http.get(`${API}/vaults/akb/info`, () => HttpResponse.json(ownerInfo)),
        http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () => HttpResponse.json(vaultSkillDoc)),
        http.get("/health/vault/akb", () => HttpResponse.json(vaultHealth)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    await expect(await canvas.findByText("Danger zone")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const VaultSettingsReadOnly: Story = {
  name: "Settings / reader permission",
  parameters: {
    router: { initialEntries: ["/vault/akb/settings"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        http.get(`${API}/vaults/akb/info`, () => HttpResponse.json(readerInfo)),
        http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () => HttpResponse.json(vaultSkillDoc)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/Read-only view/i)).toBeInTheDocument();
    await expect(canvas.queryByText("Danger zone")).not.toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const VaultSettingsArchived: Story = {
  name: "Settings / archived edge",
  parameters: {
    router: { initialEntries: ["/vault/akb/settings"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        http.get(`${API}/vaults/akb/info`, () => HttpResponse.json({ ...archivedVaultInfo, role: "owner" })),
        http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () => HttpResponse.json(vaultSkillDoc)),
        http.get("/health/vault/akb", () => HttpResponse.json(vaultHealth)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Archived")).toBeInTheDocument();
    await expect(await canvas.findByRole("button", { name: "Unarchive" })).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const VaultSettingsLoadError: Story = {
  name: "Settings / load error",
  parameters: {
    router: { initialEntries: ["/vault/akb/settings"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        http.get(`${API}/vaults/akb/info`, () =>
          HttpResponse.json({ detail: "Vault info unavailable" }, { status: 503 }),
        ),
        http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () =>
          HttpResponse.json({ detail: "Not found" }, { status: 404 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Vault info unavailable")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const ActivityTimeline: Story = {
  name: "Activity / timeline",
  parameters: {
    router: { initialEntries: ["/vault/akb/activity"] },
    msw: {
      handlers: [
        ...adminShellHandlers,
        http.get(`${API}/activity/akb`, () =>
          HttpResponse.json({
            vault: "akb",
            total: 2,
            activity: [
              {
                hash: "9ad1204b67f2c",
                author_name: "JY Kim",
                subject: "Add Storybook route shell",
                timestamp: "2026-07-06T04:12:00.000Z",
                files: [{ path: "overview/vault-skill.md", change: "modified" }],
              },
              {
                hash: "7cc091a5d1e30",
                agent: "codex",
                subject: "Update release note",
                timestamp: "2026-07-05T22:48:00.000Z",
                files: [{ path: "notes/release.md", change: "added" }],
              },
            ],
          }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "Activity" })).toBeInTheDocument();
    await expect(await canvas.findByText("Add Storybook route shell")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const ActivityEmpty: Story = {
  name: "Activity / empty",
  parameters: {
    router: { initialEntries: ["/vault/akb/activity"] },
    msw: {
      handlers: [
        ...adminShellHandlers,
        http.get(`${API}/activity/akb`, () =>
          HttpResponse.json({ vault: "akb", total: 0, activity: [] }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("No activity yet")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const ActivityLoading: Story = {
  name: "Activity / loading",
  parameters: {
    router: { initialEntries: ["/vault/akb/activity"] },
    msw: {
      handlers: [
        ...adminShellHandlers,
        http.get(`${API}/activity/akb`, async () => {
          await delay("infinite");
          return HttpResponse.json({ vault: "akb", total: 0, activity: [] });
        }),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Loading…")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const ActivityError: Story = {
  name: "Activity / API error",
  parameters: {
    router: { initialEntries: ["/vault/akb/activity"] },
    msw: {
      handlers: [
        ...adminShellHandlers,
        http.get(`${API}/activity/akb`, () =>
          HttpResponse.json({ detail: "Activity service unavailable" }, { status: 503 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Failed to load")).toBeInTheDocument();
    await expect(await canvas.findByText("Activity service unavailable")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};
