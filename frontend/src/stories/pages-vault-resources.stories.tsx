import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { delay, http, HttpResponse } from "msw";
import {
  API,
  defaultDocumentSupportHandlers,
  defaultVaultInfoHandler,
  emptyGraphOverview,
  graphOverview,
  storyFiles,
  tableCatalog,
  tableRows,
  vaultHealth,
  vaultShellHandlers,
  vaultSkillDoc,
} from "./page-story-fixtures";
import { AkbRouteTree } from "./page-route-shell";

const meta = {
  title: "Pages/Vault Resources",
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const resourceShellHandlers = [
  ...vaultShellHandlers,
  defaultVaultInfoHandler,
  http.get("/health/vault/akb", () => HttpResponse.json(vaultHealth)),
];

const storyFilePreviewUrl = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(`
  <svg xmlns="http://www.w3.org/2000/svg" width="1200" height="720" viewBox="0 0 1200 720">
    <rect width="1200" height="720" fill="#edf5f7"/>
    <rect x="96" y="88" width="1008" height="544" rx="28" fill="#ffffff" stroke="#c8ced6" stroke-width="4"/>
    <rect x="140" y="136" width="920" height="72" rx="14" fill="#004059"/>
    <rect x="140" y="248" width="312" height="304" rx="18" fill="#e0eef2"/>
    <rect x="488" y="248" width="572" height="44" rx="12" fill="#dfe3e8"/>
    <rect x="488" y="320" width="492" height="28" rx="10" fill="#ebeef2"/>
    <rect x="488" y="372" width="536" height="28" rx="10" fill="#ebeef2"/>
    <rect x="488" y="448" width="196" height="56" rx="14" fill="#c44a1e"/>
  </svg>
`)}`;

async function expectVaultShell(canvasElement: HTMLElement) {
  const canvas = within(canvasElement);
  await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
  await expect(await canvas.findByRole("tree", { name: "akb explorer" })).toBeInTheDocument();
}

async function expectGraphVaultShell(canvasElement: HTMLElement) {
  const canvas = within(canvasElement);
  await expect(await canvas.findByRole("navigation", { name: "Vaults" })).toBeInTheDocument();
  await expect(canvas.queryByRole("tree", { name: "akb explorer" })).not.toBeInTheDocument();
}

export const NewDocumentBlank: Story = {
  name: "Document create / blank draft",
  parameters: {
    router: { initialEntries: ["/vault/akb/doc/new"] },
    msw: {
      handlers: resourceShellHandlers,
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "New document." })).toBeInTheDocument();
    await expect(canvas.getByRole("button", { name: /Create document/i })).toBeDisabled();
    await expectVaultShell(canvasElement);
  },
};

export const NewDocumentPrefilledCollection: Story = {
  name: "Document create / prefilled collection",
  parameters: {
    // `overview` is the reserved system collection and is filtered out of the
    // picker, so the prefill fixture uses an ordinary collection instead.
    router: { initialEntries: ["/vault/akb/doc/new?collection=runbooks"] },
    msw: {
      handlers: resourceShellHandlers,
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByDisplayValue("runbooks")).toBeInTheDocument();
    await expect(await canvas.findByText("Existing collection")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const SkillRedirectToSettings: Story = {
  name: "Skill redirect / guide editor in settings",
  parameters: {
    router: { initialEntries: ["/vault/akb/skill"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        ...defaultDocumentSupportHandlers,
        http.get(`${API}/documents/akb/overview%2Fvault-skill.md`, () => HttpResponse.json(vaultSkillDoc)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const TablePreview: Story = {
  name: "Table / preview rows",
  parameters: {
    router: { initialEntries: ["/vault/akb/table/releases"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/tables/akb`, () => HttpResponse.json({ items: tableCatalog })),
        http.post(`${API}/tables/akb/sql`, () =>
          HttpResponse.json({
            columns: ["version", "date", "status", "owner"],
            items: tableRows,
            total: tableRows.length,
          }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "releases" })).toBeInTheDocument();
    await expect(await canvas.findByText("0.6.0")).toBeInTheDocument();
    await userEvent.click(canvas.getByRole("button", { name: "Schema" }));
    const schema = canvas.getByRole("complementary", { name: "Table schema" });
    const schemaCanvas = within(schema);
    await expect(schema).toBeVisible();
    await expect(schemaCanvas.getByRole("table")).toBeVisible();
    await expect(schemaCanvas.getByRole("columnheader", { name: "Column" })).toBeVisible();
    await expect(schemaCanvas.getByRole("columnheader", { name: "Data type" })).toBeVisible();
    await expect(schemaCanvas.getByRole("columnheader", { name: "Constraints" })).toBeVisible();
    await expect(schemaCanvas.getByText("Primary key")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await expect(canvas.queryByRole("complementary", { name: "Table schema" })).not.toBeInTheDocument();
    await expect(canvas.getByRole("button", { name: "Schema" })).toHaveFocus();
    await expectVaultShell(canvasElement);
  },
};

export const TableEmpty: Story = {
  name: "Table / empty rows",
  parameters: {
    router: { initialEntries: ["/vault/akb/table/releases"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/tables/akb`, () =>
          HttpResponse.json({ items: [{ ...tableCatalog[0], row_count: 0 }] }),
        ),
        http.post(`${API}/tables/akb/sql`, () =>
          HttpResponse.json({ columns: ["version", "date"], items: [], total: 0 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("No rows yet")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const TableLoading: Story = {
  name: "Table / loading rows",
  parameters: {
    router: { initialEntries: ["/vault/akb/table/releases"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/tables/akb`, () => HttpResponse.json({ items: tableCatalog })),
        http.post(`${API}/tables/akb/sql`, async () => {
          await delay("infinite");
          return HttpResponse.json({ columns: [], items: [], total: 0 });
        }),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("status", { name: "Loading rows" })).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const TableQueryError: Story = {
  name: "Table / query error",
  parameters: {
    router: { initialEntries: ["/vault/akb/table/releases"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/tables/akb`, () => HttpResponse.json({ items: tableCatalog })),
        http.post(`${API}/tables/akb/sql`, () =>
          HttpResponse.json({ detail: "permission denied for table releases" }, { status: 403 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/permission denied/i)).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const FileReady: Story = {
  name: "File / ready",
  parameters: {
    router: { initialEntries: ["/vault/akb/file/f-storybook"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/files/akb`, () => HttpResponse.json({ items: storyFiles })),
        http.get(`${API}/files/akb/f-storybook/download`, () =>
          HttpResponse.json({
            download_url: storyFilePreviewUrl,
            mime_type: "image/png",
            size_bytes: 184320,
          }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("heading", { name: "storybook.png" })).toBeInTheDocument();
    await expect(await canvas.findByRole("img", { name: "storybook.png" })).toBeInTheDocument();
    await expect(canvas.queryByRole("button", { name: "Schema" })).not.toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const FileLoading: Story = {
  name: "File / loading",
  parameters: {
    router: { initialEntries: ["/vault/akb/file/f-storybook"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/files/akb`, async () => {
          await delay("infinite");
          return HttpResponse.json({ items: storyFiles });
        }),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("status", { name: "Loading file" })).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const FileNotFound: Story = {
  name: "File / not found",
  parameters: {
    router: { initialEntries: ["/vault/akb/file/f-missing"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/files/akb`, () => HttpResponse.json({ items: storyFiles })),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("File not found in vault.")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const FileListError: Story = {
  name: "File / list error",
  parameters: {
    router: { initialEntries: ["/vault/akb/file/f-storybook"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/files/akb`, () =>
          HttpResponse.json({ detail: "File service unavailable" }, { status: 500 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Couldn't load the file list (500).")).toBeInTheDocument();
    await expectVaultShell(canvasElement);
  },
};

export const GraphOverview: Story = {
  name: "Graph / overview",
  parameters: {
    router: { initialEntries: ["/vault/akb/graph"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/graph/overview`, () => HttpResponse.json(graphOverview)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("complementary", { name: "Graph controls" })).toBeInTheDocument();
    await expect(await canvas.findByText(/Showing the 3 most-connected of 6 nodes/i)).toBeInTheDocument();
    await expectGraphVaultShell(canvasElement);
  },
};

export const GraphEmpty: Story = {
  name: "Graph / empty",
  parameters: {
    router: { initialEntries: ["/vault/akb/graph?types=file"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/graph/overview`, () => HttpResponse.json(emptyGraphOverview)),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Empty graph")).toBeInTheDocument();
    await expectGraphVaultShell(canvasElement);
  },
};

export const GraphError: Story = {
  name: "Graph / API error",
  parameters: {
    router: { initialEntries: ["/vault/akb/graph"] },
    msw: {
      handlers: [
        ...resourceShellHandlers,
        http.get(`${API}/graph/overview`, () =>
          HttpResponse.json({ detail: "Graph backend unavailable" }, { status: 503 }),
        ),
      ],
    },
  },
  render: () => <AkbRouteTree />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Failed to load graph")).toBeInTheDocument();
    await expect(await canvas.findByText(/Graph backend unavailable/i)).toBeInTheDocument();
    await expectGraphVaultShell(canvasElement);
  },
};
