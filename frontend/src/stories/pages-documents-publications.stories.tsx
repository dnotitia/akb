import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { http, HttpResponse } from "msw";
import {
  API,
  defaultDocumentSupportHandlers,
  defaultVaultInfoHandler,
  publicContentUnavailable,
  publicDocument,
  publicSectionWarning,
  regularDoc,
  vaultShellHandlers,
  vaultSkillDoc,
} from "./page-story-fixtures";
import { AkbRouteTree } from "./page-route-shell";

const meta = {
  title: "Pages/Documents & Publications",
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const documentPageHandlers = [
  ...vaultShellHandlers,
  defaultVaultInfoHandler,
  ...defaultDocumentSupportHandlers,
];

export const DocumentRendered: Story = {
  name: "Document read / rendered markdown",
  parameters: {
    router: { initialEntries: ["/vault/akb/doc/overview%2Fvault-skill.md"] },
    msw: {
      handlers: [
        ...documentPageHandlers,
        http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () => HttpResponse.json(vaultSkillDoc)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { level: 1, name: "Storybook rollout" })).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
    await expect(await canvas.findByRole("tree", { name: "akb explorer" })).toBeInTheDocument();
  },
};

export const DocumentRawTab: Story = {
  name: "Document read / raw markdown tab",
  parameters: {
    router: { initialEntries: ["/vault/akb/doc/overview%2Fvault-skill.md"] },
    msw: {
      handlers: [
        ...documentPageHandlers,
        http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () => HttpResponse.json(vaultSkillDoc)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("tab", { name: "Raw" })).toBeInTheDocument();
    await userEvent.click(canvas.getByRole("tab", { name: "Raw" }));
    await expect(canvas.getByTestId("doc-raw")).toHaveTextContent("akb://akb");
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
  },
};

export const DocumentAgentPreview: Story = {
  name: "Document read / skill agent preview",
  parameters: {
    router: { initialEntries: ["/vault/akb/doc/overview%2Fvault-skill.md"] },
    msw: {
      handlers: [
        ...documentPageHandlers,
        http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () => HttpResponse.json(vaultSkillDoc)),
        http.get(`${API}/help/vault-skill-preview/akb`, () =>
          HttpResponse.text("Agents read this guide first."),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(await canvas.findByRole("tab", { name: "Agent" }));
    await expect(await canvas.findByText("Agents read this guide first.")).toBeInTheDocument();
    await expect(await canvas.findByRole("tree", { name: "akb explorer" })).toBeInTheDocument();
  },
};

export const DocumentRegularNote: Story = {
  name: "Document read / regular note",
  parameters: {
    router: { initialEntries: ["/vault/akb/doc/notes%2Frelease.md"] },
    msw: {
      handlers: [
        ...documentPageHandlers,
        http.get(`${API}/documents/akb/notes%2Frelease.md`, () => HttpResponse.json(regularDoc)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { level: 1, name: "Release note" })).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
    await expect(canvas.queryByRole("tab", { name: "Agent" })).not.toBeInTheDocument();
  },
};

export const DocumentLoadError: Story = {
  name: "Document read / load error",
  parameters: {
    router: { initialEntries: ["/vault/akb/doc/missing.md"] },
    msw: {
      handlers: [
        ...documentPageHandlers,
        http.get(`${API}/documents/akb/missing.md`, () =>
          HttpResponse.json({ detail: "Document not found" }, { status: 404 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/Document not found/i)).toBeInTheDocument();
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
  },
};

export const PublicDocument: Story = {
  name: "Publication / public document",
  parameters: {
    authToken: false,
    router: { initialEntries: ["/p/storybook-guide"] },
    msw: {
      handlers: [
        http.get(`${API}/public/storybook-guide`, () => HttpResponse.json(publicDocument)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(
      await canvas.findByRole("heading", { level: 1, name: "Public Storybook Guide" }),
    ).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

export const PublicSectionNotFound: Story = {
  name: "Publication / section filter not found",
  parameters: {
    authToken: false,
    router: { initialEntries: ["/p/section-warning"] },
    msw: {
      handlers: [
        http.get(`${API}/public/section-warning`, () => HttpResponse.json(publicSectionWarning)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Section not found")).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

export const PublicContentUnavailable: Story = {
  name: "Publication / source removed",
  parameters: {
    authToken: false,
    router: { initialEntries: ["/p/source-removed"] },
    msw: {
      handlers: [
        http.get(`${API}/public/source-removed`, () => HttpResponse.json(publicContentUnavailable)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Content unavailable")).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

export const PublicPasswordRequired: Story = {
  name: "Publication / password required",
  parameters: {
    authToken: false,
    router: { initialEntries: ["/p/protected-guide"] },
    msw: {
      handlers: [
        http.get(`${API}/public/protected-guide`, () =>
          HttpResponse.json({ detail: "Password required", password_required: true }, { status: 403 }),
        ),
        http.post(`${API}/public/protected-guide/auth`, () =>
          HttpResponse.json({ detail: "Invalid password" }, { status: 401 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "Protected publication" })).toBeInTheDocument();
    await userEvent.type(canvas.getByPlaceholderText("Password"), "wrong");
    await userEvent.click(canvas.getByRole("button", { name: "Unlock" }));
    await expect(await canvas.findByText("Invalid password")).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};

export const PublicExpired: Story = {
  name: "Publication / expired link",
  parameters: {
    authToken: false,
    router: { initialEntries: ["/p/expired-guide"] },
    msw: {
      handlers: [
        http.get(`${API}/public/expired-guide`, () =>
          HttpResponse.json({ detail: "This publication has expired" }, { status: 410 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("This publication has expired")).toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();
    await expect(canvas.queryByRole("navigation", { name: "Vaults" })).not.toBeInTheDocument();
  },
};
