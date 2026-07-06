import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { http, HttpResponse } from "msw";
import {
  API,
  appChromeHandlers,
  appLayoutHandlers,
  archivedVaultInfo,
  defaultVaultInfoHandler,
  emptyVaultInfo,
  recentChanges,
  templates,
  vaultHealth,
  vaultInfo,
  vaultShellHandlers,
  vaultSkillDoc,
} from "./page-story-fixtures";
import { AkbRouteTree } from "./page-route-shell";

const meta = {
  title: "Pages/Vaults",
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const emptyRecent = { changes: [] };

const vaultPageHandlers = [
  ...vaultShellHandlers,
  defaultVaultInfoHandler,
  http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () => HttpResponse.json(vaultSkillDoc)),
  http.get("/health/vault/akb", () => HttpResponse.json(vaultHealth)),
  http.get(`${API}/recent`, () => HttpResponse.json(recentChanges)),
  http.get(`${API}/activity/akb`, () => HttpResponse.json({ vault: "akb", total: 0, activity: [] })),
];

export const IndexWithExistingVaults: Story = {
  name: "Index / select an existing vault",
  parameters: {
    router: { initialEntries: ["/vault"] },
    msw: {
      handlers: [
        ...appChromeHandlers,
        http.get(`${API}/my/vaults`, () =>
          HttpResponse.json({ vaults: [{ id: "v-akb", name: "akb", role: "owner" }, { id: "v-research", name: "research", role: "writer" }] }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Select a vault")).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
  },
};

export const IndexEmpty: Story = {
  name: "Index / no vaults yet",
  parameters: {
    router: { initialEntries: ["/vault"] },
    msw: {
      handlers: [
        ...appChromeHandlers,
        http.get(`${API}/my/vaults`, () => HttpResponse.json({ vaults: [] })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect((await canvas.findAllByText("No vaults yet")).length).toBeGreaterThan(0);
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
  },
};

export const NewVaultWithTemplates: Story = {
  name: "Create / template picker",
  parameters: {
    router: { initialEntries: ["/vault/new"] },
    msw: {
      handlers: [
        ...appLayoutHandlers,
        http.get(`${API}/vaults/templates`, () => HttpResponse.json(templates)),
        http.post(`${API}/vaults`, () => HttpResponse.json({ ok: true, name: "storybook" })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
    await userEvent.click(await canvas.findByLabelText("Vault template"));
    await expect(await within(document.body).findByText("Engineering workspace")).toBeVisible();
    await userEvent.keyboard("{Escape}");
    await userEvent.type(canvas.getByLabelText(/Name/i), "storybook");
    await expect(canvas.getByRole("button", { name: /Create vault/i })).toBeEnabled();
  },
};

export const NewVaultNameConflict: Story = {
  name: "Create / name conflict",
  parameters: {
    router: { initialEntries: ["/vault/new"] },
    msw: {
      handlers: [
        ...appLayoutHandlers,
        http.get(`${API}/vaults/templates`, () => HttpResponse.json(templates)),
        http.post(`${API}/vaults`, () =>
          HttpResponse.json({ detail: "Vault already exists." }, { status: 409 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.type(await canvas.findByLabelText(/Name/i), "akb");
    await userEvent.click(canvas.getByRole("button", { name: /Create vault/i }));
    await expect(await canvas.findByText("Vault already exists.")).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

export const OverviewActive: Story = {
  name: "Overview / active vault",
  parameters: {
    router: { initialEntries: ["/vault/akb"] },
    msw: {
      handlers: [
        ...vaultPageHandlers,
        http.get(`${API}/vaults/akb/info`, () => HttpResponse.json(vaultInfo)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "akb" })).toBeInTheDocument();
    await expect(await canvas.findByText("Recent activity")).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
    await expect(await canvas.findByRole("tree", { name: "akb explorer" })).toBeInTheDocument();
  },
};

export const OverviewEmptyOnboarding: Story = {
  name: "Overview / empty onboarding",
  parameters: {
    router: { initialEntries: ["/vault/empty"] },
    msw: {
      handlers: [
        ...appChromeHandlers,
        http.get(`${API}/my/vaults`, () =>
          HttpResponse.json({ vaults: [{ id: "v-empty", name: "empty", role: "writer" }] }),
        ),
        http.get(`${API}/browse/empty`, () =>
          HttpResponse.json({ vault: "empty", path: "", items: [] }),
        ),
        http.get(`${API}/vaults/empty/info`, () => HttpResponse.json(emptyVaultInfo)),
        http.get(`${API}/documents/empty/overview%2Fvault-skill.md`, () =>
          HttpResponse.json({ ...vaultSkillDoc, title: "Empty vault guide" }),
        ),
        http.get("/health/vault/empty", () => HttpResponse.json(vaultHealth)),
        http.get(`${API}/recent`, () => HttpResponse.json(emptyRecent)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("This vault is just getting started")).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
    await expect(await canvas.findByRole("tree", { name: "empty explorer" })).toBeInTheDocument();
  },
};

export const OverviewArchivedReadOnly: Story = {
  name: "Overview / archived read-only",
  parameters: {
    router: { initialEntries: ["/vault/archive"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        http.get(`${API}/vaults/archive/info`, () => HttpResponse.json(archivedVaultInfo)),
        http.get(`${API}/documents/archive/overview%2Fvault-skill.md`, () => HttpResponse.json(vaultSkillDoc)),
        http.get("/health/vault/archive", () => HttpResponse.json(vaultHealth)),
        http.get(`${API}/recent`, () => HttpResponse.json(recentChanges)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/This vault is archived/i)).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
    await expect(await canvas.findByRole("tree", { name: "archive explorer" })).toBeInTheDocument();
  },
};

export const OverviewInfoError: Story = {
  name: "Overview / info API error",
  parameters: {
    router: { initialEntries: ["/vault/akb"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        http.get(`${API}/vaults/akb/info`, () =>
          HttpResponse.json({ detail: "Database unavailable" }, { status: 503 }),
        ),
        http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () =>
          HttpResponse.json({ detail: "Not found" }, { status: 404 }),
        ),
        http.get("/health/vault/akb", () => HttpResponse.json(vaultHealth)),
        http.get(`${API}/recent`, () => HttpResponse.json({ detail: "Recent failed" }, { status: 500 })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/Could not load this vault/i)).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
    await expect(await canvas.findByRole("tree", { name: "akb explorer" })).toBeInTheDocument();
  },
};
