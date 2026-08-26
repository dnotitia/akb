import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, within } from "storybook/test";
import { http, HttpResponse } from "msw";
import {
  API,
  appLayoutHandlers,
  defaultVaultInfoHandler,
  degradedSearchResults,
  denseSearchResults,
  emptySearchResults,
  literalSearchResults,
  vaultShellHandlers,
} from "./page-story-fixtures";
import { AkbRouteTree } from "./page-route-shell";

const meta = {
  title: "Pages/Search",
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const ReadyNoQuery: Story = {
  name: "Ready / no query",
  parameters: {
    router: { initialEntries: ["/search"] },
    msw: {
      handlers: appLayoutHandlers,
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "Suggested searches" })).toBeInTheDocument();
    await expect(canvas.getByRole("search", { name: "Search all vaults" })).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

export const SemanticResults: Story = {
  name: "Semantic / mixed results",
  parameters: {
    router: { initialEntries: ["/search?q=guide"] },
    msw: {
      handlers: [
        ...appLayoutHandlers,
        http.get(`${API}/search`, () => HttpResponse.json(denseSearchResults)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("AKB Guide")).toBeInTheDocument();
    await expect(canvas.getByText("storybook.png")).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  },
};

export const SemanticNoResults: Story = {
  name: "Semantic / no results",
  parameters: {
    router: { initialEntries: ["/search?q=missing"] },
    msw: {
      handlers: [
        ...appLayoutHandlers,
        http.get(`${API}/search`, () => HttpResponse.json(emptySearchResults)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: /No results/i })).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  },
};

export const SemanticDegraded: Story = {
  name: "Semantic / degraded index",
  parameters: {
    router: { initialEntries: ["/search?q=guide"] },
    msw: {
      handlers: [
        ...appLayoutHandlers,
        http.get(`${API}/search`, () => HttpResponse.json(degradedSearchResults)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/Search is degraded/i)).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  },
};

export const SemanticError: Story = {
  name: "Semantic / API error",
  parameters: {
    router: { initialEntries: ["/search?q=broken"] },
    msw: {
      handlers: [
        ...appLayoutHandlers,
        http.get(`${API}/search`, () =>
          HttpResponse.json({ detail: "Vector store unavailable" }, { status: 503 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/Vector store unavailable/i)).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  },
};

export const LiteralResults: Story = {
  name: "Literal / line matches",
  parameters: {
    router: { initialEntries: ["/search?q=token&mode=literal"] },
    msw: {
      handlers: [
        ...appLayoutHandlers,
        http.get(`${API}/grep`, () => HttpResponse.json(literalSearchResults)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Token runbook")).toBeInTheDocument();
    await expect(canvas.getByText(/Rotate a personal access token/i)).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
  },
};

export const ScopedVaultSearch: Story = {
  name: "Scoped vault / semantic results",
  parameters: {
    router: { initialEntries: ["/vault/akb/search?q=guide"] },
    msw: {
      handlers: [
        ...vaultShellHandlers,
        defaultVaultInfoHandler,
        http.get(`${API}/search`, () => HttpResponse.json(denseSearchResults)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
    await expect(await canvas.findByRole("tree", { name: "akb explorer" })).toBeInTheDocument();
    await expect(canvas.getByText("Search all vaults")).toBeInTheDocument();
  },
};
