import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { http, HttpResponse } from "msw";
import { useLocation } from "react-router-dom";
import {
  API,
  defaultDocumentSupportHandlers,
  defaultVaultInfoHandler,
  publicContentUnavailable,
  publicDocument,
  publicSectionWarning,
  regularDoc,
  vaultShellHandlers,
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

function DocumentRouteProbe() {
  const location = useLocation();
  return <output data-testid="document-route" hidden>{location.pathname}{location.search}</output>;
}

// Regular-document fixtures exercise the shared reader independently of guide editing.
export const DocumentRawTab: Story = {
  name: "Document read / raw markdown switch",
  parameters: {
    router: { initialEntries: ["/vault/akb/doc/notes%2Frelease.md"] },
    msw: {
      handlers: [
        ...documentPageHandlers,
        http.get(`${API}/documents/akb/notes%2Frelease.md`, () => HttpResponse.json(regularDoc)),
      ],
    },
  },
  render: () => <><AkbRouteTree /><DocumentRouteProbe /></>,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    // Enter the browser iframe before the action: foreground access proof can
    // replace a loaded surface while an initial pointer gesture is in flight.
    await userEvent.click(await canvas.findByRole("heading", { level: 1, name: "Release note" }));
    await expect(await canvas.findByRole("radio", { name: "Raw markdown" })).toBeInTheDocument();
    await userEvent.click(canvas.getByRole("radio", { name: "Raw markdown" }));
    await expect((await canvas.findByTestId("doc-raw")).textContent).toBe(regularDoc.content);
    await expect(canvas.getByRole("radio", { name: "Raw markdown" })).toHaveAttribute("aria-checked", "true");
    await expect(canvas.getByTestId("document-route").textContent).toBe("/vault/akb/doc/notes%2Frelease.md?view=raw");
    await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
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

export const DocumentLongSummary: Story = {
  name: "Document read / long summary preview",
  parameters: {
    router: { initialEntries: ["/vault/akb/doc/notes%2Frelease.md"] },
    msw: {
      handlers: [
        ...documentPageHandlers,
        http.get(`${API}/documents/akb/notes%2Frelease.md`, () => HttpResponse.json({
          ...regularDoc,
          content: "# Release note\n\nThis release brings a calmer document reading experience, with the tools you need kept within reach.\n\n## Reading documents\n\nSwitch between the formatted preview and Markdown source without leaving your place. The summary appears below the title, with motion only when its text overflows.\n\n## Working together\n\nEdit the document when you need to make changes. Properties, revision history and related documents share a single details panel.\n\n> Sharing stays explicit: review public-link options before publishing a document.\n\n## Verification\n\n- Preview and source stay aligned.\n- Revision history keeps its existing comparisons.\n- Keyboard navigation and small screens remain supported.\n",
          summary: "This release improves workspace sign-in, document permissions and file upload recovery. Profile photos can be added, replaced and removed. The rollout preserves existing accounts and documents, and verifies reading, editing, revision history and search. This note records the deployment sequence, verification results and remaining follow-up checks.",
        })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
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
