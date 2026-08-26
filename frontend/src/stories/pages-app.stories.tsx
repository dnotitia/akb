import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, within } from "storybook/test";
import { delay, http, HttpResponse } from "msw";
import {
  API,
  activePatTokens,
  adminUsers,
  currentUser,
  defaultAnyVaultInfoHandler,
  defaultVaultListHandler,
  localAuthConfig,
  nonAdminUser,
  recentChanges,
  vaultHealth,
} from "./page-story-fixtures";
import { AkbRouteTree } from "./page-route-shell";

const meta = {
  title: "Pages/App",
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const chromeWithTokens = [
  http.get("/health", () => HttpResponse.json(vaultHealth)),
  http.get(`${API}/auth/me`, () => HttpResponse.json(currentUser)),
  http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
  http.get(`${API}/auth/tokens`, () => HttpResponse.json({ tokens: activePatTokens })),
];

const chromeWithUser = (user: typeof currentUser) => [
  http.get("/health", () => HttpResponse.json(vaultHealth)),
  http.get(`${API}/auth/me`, () => HttpResponse.json(user)),
  http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
  http.get(`${API}/auth/tokens`, () => HttpResponse.json({ tokens: activePatTokens })),
];

const appHomeHandlers = [
  ...chromeWithTokens,
  defaultVaultListHandler,
  defaultAnyVaultInfoHandler,
  http.get(`${API}/recent`, () => HttpResponse.json(recentChanges)),
];

export const HomeWorkspace: Story = {
  name: "Home / workspace summary",
  parameters: {
    router: { initialEntries: ["/"] },
    msw: {
      handlers: appHomeHandlers,
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(
      await canvas.findByRole("heading", { name: "Find what the team already knows." }),
    ).toBeInTheDocument();
    await expect(await canvas.findByText("Recent activity")).toBeInTheDocument();
    await expect(await canvas.findByText("Your vaults")).toBeInTheDocument();
    await expect(await canvas.findByRole("link", { name: "View all vaults" })).toBeInTheDocument();
    await expect(canvas.queryByRole("button", { name: "Show more vaults" })).not.toBeInTheDocument();
    await expect(await canvas.findByRole("heading", { name: "Connect" })).toBeInTheDocument();
    await expect(await canvas.findByRole("button", { name: "Mint token" })).toBeInTheDocument();
    await expect(await canvas.findByRole("tab", { name: "Claude Code" })).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

const expandableRecent = Array.from({ length: 16 }, (_, index) => ({
  ...recentChanges.changes[index % recentChanges.changes.length],
  doc_id: `d-expand-${index + 1}`,
  title: `Expandable change ${index + 1}`,
  changed_at: new Date(Date.UTC(2026, 6, 6, 4, 12 - index)).toISOString(),
}));

export const HomeRecentExpandable: Story = {
  name: "Home / recent show more",
  parameters: {
    router: { initialEntries: ["/"] },
    msw: {
      handlers: [
        ...chromeWithTokens,
        defaultVaultListHandler,
        defaultAnyVaultInfoHandler,
        http.get(`${API}/recent`, ({ request }) => {
          const limit = Number(new URL(request.url).searchParams.get("limit") || "8");
          return HttpResponse.json({ changes: expandableRecent.slice(0, limit) });
        }),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Expandable change 8")).toBeInTheDocument();
    await expect(canvas.queryByText("Expandable change 9")).not.toBeInTheDocument();
    await expect(
      await canvas.findByRole("button", { name: "Show more recent activity" }),
    ).toBeInTheDocument();
  },
};

export const HomeEmptyWorkspace: Story = {
  name: "Home / empty workspace",
  parameters: {
    router: { initialEntries: ["/"] },
    msw: {
      handlers: [
        ...chromeWithTokens,
        http.get(`${API}/my/vaults`, () => HttpResponse.json({ vaults: [] })),
        http.get(`${API}/recent`, () => HttpResponse.json({ changes: [] })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Nothing touched yet")).toBeInTheDocument();
    await expect(await canvas.findByText("No vaults yet")).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

export const HomeRecentLoading: Story = {
  name: "Home / recent loading",
  parameters: {
    router: { initialEntries: ["/"] },
    msw: {
      handlers: [
        ...chromeWithTokens,
        defaultVaultListHandler,
        defaultAnyVaultInfoHandler,
        http.get(`${API}/recent`, async () => {
          await delay("infinite");
          return HttpResponse.json({ changes: [] });
        }),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(
      await canvas.findByRole("heading", { name: "Find what the team already knows." }),
    ).toBeInTheDocument();
    await expect(await canvas.findByText("Recent activity")).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  },
};

export const HomeRecentError: Story = {
  name: "Home / recent error",
  parameters: {
    router: { initialEntries: ["/"] },
    msw: {
      handlers: [
        ...chromeWithTokens,
        defaultVaultListHandler,
        defaultAnyVaultInfoHandler,
        http.get(`${API}/recent`, () =>
          HttpResponse.json({ detail: "Recent API unavailable" }, { status: 503 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Couldn't load recent activity")).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  },
};

export const SettingsProfile: Story = {
  name: "Settings / profile",
  parameters: {
    router: { initialEntries: ["/settings"] },
    msw: {
      handlers: [
        ...chromeWithTokens,
        defaultVaultListHandler,
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "Account settings" })).toBeInTheDocument();
    await expect(await canvas.findByText("Personal workspace")).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

export const SettingsTokensError: Story = {
  name: "Settings / tokens error",
  parameters: {
    router: { initialEntries: ["/settings?tab=tokens"] },
    msw: {
      handlers: [
        http.get("/health", () => HttpResponse.json(vaultHealth)),
        http.get(`${API}/auth/me`, () => HttpResponse.json(currentUser)),
        http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
        http.get(`${API}/auth/tokens`, () =>
          HttpResponse.json({ detail: "Token store unavailable" }, { status: 503 }),
        ),
        defaultVaultListHandler,
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Couldn't load tokens")).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

export const SettingsAdminRoster: Story = {
  name: "Settings / admin roster",
  parameters: {
    router: { initialEntries: ["/settings?tab=admin"] },
    msw: {
      handlers: [
        ...chromeWithTokens,
        defaultVaultListHandler,
        http.get(`${API}/admin/users`, () => HttpResponse.json({ users: adminUsers })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Users")).toBeInTheDocument();
    await expect(await canvas.findByText("writer")).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  },
};

export const SettingsAdminPermissionFallback: Story = {
  name: "Settings / non-admin admin tab fallback",
  parameters: {
    router: { initialEntries: ["/settings?tab=admin"] },
    msw: {
      handlers: [
        ...chromeWithUser(nonAdminUser),
        defaultVaultListHandler,
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Personal workspace")).toBeInTheDocument();
    await expect(canvas.queryByRole("tab", { name: "Admin" })).not.toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  },
};
