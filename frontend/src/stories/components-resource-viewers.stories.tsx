import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, within } from "storybook/test";
import { http, HttpResponse } from "msw";
import { FileViewer } from "@/components/file-viewer";
import { TableViewer } from "@/components/table-viewer";
import { DocumentOutline } from "@/components/doc-outline";
import { RelationsPanel } from "@/components/relations/relations-panel";
import { Panel, PanelHeader } from "@/components/ui/panel";
import type { PublicationResponse, RelationRow } from "@/lib/api";

const meta = {
  title: "Components/Resource viewers",
  parameters: {
    layout: "fullscreen",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const API = "/api/v1";

const tableData: PublicationResponse = {
  resource_type: "table_query",
  title: "Revenue by collection",
  mode: "live",
  total: 4,
  columns: ["collection", "documents", "owner", "freshness"],
  rows: [
    { collection: "overview", documents: 28, owner: "platform", freshness: "fresh" },
    { collection: "runbooks", documents: 17, owner: "sre", freshness: "stale" },
    { collection: "decisions", documents: 43, owner: "architecture", freshness: "fresh" },
    { collection: "incidents", documents: 9, owner: "ops", freshness: "hot" },
  ],
  query_params: {
    since: { type: "text", default: "2026-01-01", required: true },
    min_docs: { type: "number", default: 5 },
  },
};

const emptyTableData: PublicationResponse = {
  resource_type: "table_query",
  title: "Expired token audit",
  mode: "snapshot",
  snapshot_at: "2026-07-06T04:00:00.000Z",
  total: 0,
  columns: ["token", "owner", "expired_at"],
  rows: [],
};

const textFile: PublicationResponse = {
  resource_type: "file",
  title: "Deploy note",
  name: "deploy-note.txt",
  mime_type: "text/plain",
  size_bytes: 1880,
  collection: "runbooks",
  download_url: `${API}/public/text-file/download`,
};

const jsonFile: PublicationResponse = {
  resource_type: "file",
  title: "Search payload",
  name: "search-payload.json",
  mime_type: "application/json",
  size_bytes: 632,
  collection: "fixtures",
  download_url: `${API}/public/json-file/download`,
};

const binaryFile: PublicationResponse = {
  resource_type: "file",
  title: "Archive bundle",
  name: "agent-bundle.zip",
  mime_type: "application/zip",
  size_bytes: 42_420,
  collection: "exports",
  download_url: `${API}/public/binary-file/download`,
};

const relations: RelationRow[] = [
  {
    direction: "outgoing",
    relation: "depends_on",
    uri: "akb://akb/doc/runbooks/deploy.md",
    resource_type: "document",
    name: "runbooks/deploy.md",
  },
  {
    direction: "incoming",
    relation: "implements",
    uri: "akb://akb/doc/decisions/storybook-rollout.md",
    resource_type: "document",
    name: "decisions/storybook-rollout.md",
  },
  {
    direction: "outgoing",
    relation: "links_to",
    uri: "akb://akb/file/f-123",
    resource_type: "file",
    name: "figures/storybook.png",
  },
];

function StoryFrame({ children }: { children: ReactNode }) {
  return <main className="mx-auto max-w-6xl p-6">{children}</main>;
}

function OutlineFixture() {
  const articleRef = useRef<HTMLElement | null>(null);
  const [articleEl, setArticleEl] = useState<HTMLElement | null>(null);
  const markdown = [
    "# Storybook rollout",
    "## Scope",
    "### Providers",
    "## MSW contracts",
    "## Verification",
  ].join("\n");

  useEffect(() => {
    setArticleEl(articleRef.current);
  }, []);

  return (
    <StoryFrame>
      <div className="grid gap-6 lg:grid-cols-[1fr_240px]">
        <article
          ref={articleRef}
          className="prose prose-sm max-w-none rounded-[var(--radius-lg)] border border-border bg-surface p-6"
        >
          <h1 id="storybook-rollout">Storybook rollout</h1>
          <h2 id="scope">Scope</h2>
          <p>Stories are organized from primitives to routed page states.</p>
          <h3 id="providers">Providers</h3>
          <p>Preview decorators supply router, query client, auth token, MSW, and theme globals.</p>
          <h2 id="msw-contracts">MSW contracts</h2>
          <p>Network-heavy stories should mirror backend response shapes.</p>
          <h2 id="verification">Verification</h2>
          <p>Build, interaction tests, and browser checks close the loop.</p>
        </article>
        <Panel>
          <PanelHeader label="Document outline" count={4} />
          <div className="p-3">
            <DocumentOutline markdown={markdown} articleEl={articleEl} />
          </div>
        </Panel>
      </div>
    </StoryFrame>
  );
}

export const TableWithParameters: Story = {
  parameters: {
    msw: {
      handlers: [
        http.get(`${API}/public/table-live`, () => HttpResponse.json(tableData)),
      ],
    },
  },
  render: () => (
    <StoryFrame>
      <TableViewer slug="table-live" initialData={tableData} />
    </StoryFrame>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("Revenue by collection")).toBeInTheDocument();
    await expect(canvas.getByLabelText(/since/i)).toHaveValue("2026-01-01");
  },
};

export const EmptySnapshotTable: Story = {
  render: () => (
    <StoryFrame>
      <TableViewer slug="table-empty" initialData={emptyTableData} />
    </StoryFrame>
  ),
};

export const TextFilePreview: Story = {
  parameters: {
    msw: {
      handlers: [
        http.get(`${API}/public/text-file/raw`, () =>
          HttpResponse.text(
            [
              "# deploy-note.txt",
              "",
              "1. Confirm backend readiness.",
              "2. Publish Storybook static build.",
              "3. Record the release note in AKB.",
            ].join("\n"),
          ),
        ),
      ],
    },
  },
  render: () => (
    <StoryFrame>
      <FileViewer slug="text-file" data={textFile} />
    </StoryFrame>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText(/Confirm backend readiness/i)).toBeInTheDocument();
  },
};

export const JsonFilePreview: Story = {
  parameters: {
    msw: {
      handlers: [
        http.get(`${API}/public/json-file/raw`, () =>
          HttpResponse.json({
            query: "storybook",
            vaults: ["akb", "research"],
            degraded: false,
            results: [{ title: "AKB Guide", score: 0.91 }],
          }),
        ),
      ],
    },
  },
  render: () => (
    <StoryFrame>
      <FileViewer slug="json-file" data={jsonFile} />
    </StoryFrame>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("button", { name: "object, 4 items" })).toBeInTheDocument();
    await expect(canvas.getByText("false")).toBeInTheDocument();
  },
};

export const UnsupportedBinaryFile: Story = {
  render: () => (
    <StoryFrame>
      <FileViewer slug="binary-file" data={binaryFile} />
    </StoryFrame>
  ),
};

export const DocumentOutlineStates: Story = {
  render: () => <OutlineFixture />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByRole("navigation", { name: "Document outline" })).toBeInTheDocument();
  },
};

export const RelationsLinked: Story = {
  render: () => (
    <StoryFrame>
      <Panel className="max-w-xl">
        <PanelHeader label="Relations" count={relations.length} />
        <div className="p-4">
          <RelationsPanel
            vault="akb"
            sourceUri="akb://akb/doc/overview/storybook.md"
            relations={relations}
            relationsError={false}
            graphHref="/vault/akb/graph?uri=akb%3A%2F%2Fakb%2Fdoc%2Foverview%2Fstorybook.md"
            onReload={() => {}}
          />
        </div>
      </Panel>
    </StoryFrame>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await expect(await canvas.findByText("depends_on")).toBeInTheDocument();
    await expect(canvas.getByText("runbooks/deploy.md", { exact: false })).toBeInTheDocument();
  },
};

export const RelationsEmptyAndError: Story = {
  render: () => (
    <StoryFrame>
      <div className="grid gap-4 md:grid-cols-2">
        <Panel>
          <PanelHeader label="Empty relations" count={0} />
          <div className="p-4">
            <RelationsPanel
              vault="akb"
              sourceUri="akb://akb/doc/overview/empty.md"
              relations={[]}
              relationsError={false}
              graphHref="/vault/akb/graph"
              onReload={() => {}}
            />
          </div>
        </Panel>
        <Panel>
          <PanelHeader label="Relation error" />
          <div className="p-4">
            <RelationsPanel
              vault="akb"
              sourceUri="akb://akb/doc/overview/error.md"
              relations={[]}
              relationsError
              graphHref="/vault/akb/graph"
              onReload={() => {}}
            />
          </div>
        </Panel>
      </div>
    </StoryFrame>
  ),
};
