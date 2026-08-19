import { http, HttpResponse } from "msw";
import type { PublicationResponse } from "@/lib/api";

export const API = "/api/v1";

export const localAuthConfig = {
  schema_version: 2,
  auth_mode: "local",
  local_auth: { enabled: true },
  keycloak: {
    enabled: false,
    browser_session_ready: false,
  },
  providers: [],
  mcp_oauth: { enabled: false },
};

export const stagedSsoAuthConfig = {
  schema_version: 2,
  auth_mode: "sso",
  local_auth: { enabled: false },
  keycloak: {
    enabled: true,
    browser_session_ready: false,
  },
  providers: [{
    provider_type: "keycloak-oidc",
    alias: "workforce",
    display_name: "Company SSO",
    login_url: null,
  }],
  mcp_oauth: { enabled: true },
};

export const templates = [
  {
    name: "engineering",
    display_name: "Engineering workspace",
    description: "Creates overview, decisions, runbooks, and specs collections.",
    collection_count: 4,
    collections: [
      { path: "overview", name: "Overview" },
      { path: "decisions", name: "Decisions" },
      { path: "runbooks", name: "Runbooks" },
      { path: "specs", name: "Specs" },
    ],
  },
];

export const vaultSkillDoc = {
  id: "d-story",
  title: "Storybook rollout",
  type: "skill",
  path: "overview/vault-skill.md",
  uri: "akb://akb/doc/overview/vault-skill.md",
  current_commit: "story-skill",
  created_by_name: "AKB Team",
  updated_at: "2026-07-06T04:12:00.000Z",
  summary: "Document stories cover rendered, raw, and agent guide views.",
  tags: ["storybook", "agent"],
  content: [
    "# Storybook rollout",
    "",
    "Document stories cover rendered, raw, and agent guide views.",
    "",
    "## Agent workflow",
    "",
    "Agents read this guide before writing to the vault.",
    "",
    "```md",
    "akb://akb/overview/vault-skill.md",
    "```",
  ].join("\n"),
};

export const regularDoc = {
  id: "d-note",
  title: "Release note",
  type: "note",
  path: "notes/release.md",
  uri: "akb://akb/doc/notes/release.md",
  current_commit: "story-note",
  created_by_name: "AKB Team",
  updated_at: "2026-07-05T22:48:00.000Z",
  summary: "A regular document hides the Agent tab and focuses on rendered/raw reading.",
  tags: ["release"],
  content: [
    "# Release note",
    "",
    "A regular document hides the Agent tab and focuses on rendered/raw reading.",
  ].join("\n"),
};

export const denseSearchResults = {
  query: "guide",
  results: [
    {
      source_type: "document",
      uri: "akb://akb/doc/overview/vault-skill.md",
      vault: "akb",
      path: "overview/vault-skill.md",
      title: "AKB Guide",
      collection: "overview",
      doc_type: "skill",
      summary: "Instructions that agents read before writing into the vault.",
      matched_section: "Agent workflow",
      score: 0.91,
    },
    {
      source_type: "table",
      uri: "akb://akb/table/releases",
      vault: "akb",
      path: "releases",
      title: "releases",
      collection: null,
      score: 0.72,
    },
    {
      source_type: "file",
      uri: "akb://research/file/f-123",
      vault: "research",
      path: "figures/storybook.png",
      title: "storybook.png",
      collection: "figures",
      score: 0.63,
    },
  ],
  total: 3,
  total_matches: 9,
  returned: 3,
  truncated: false,
  degraded: false,
};

export const emptySearchResults = {
  query: "missing",
  results: [],
  total: 0,
  total_matches: 0,
  returned: 0,
  truncated: false,
  degraded: false,
};

export const degradedSearchResults = {
  query: "guide",
  results: [],
  total: 0,
  total_matches: 0,
  returned: 0,
  truncated: false,
  degraded: true,
  degradation_reason: "vector_store_unavailable",
};

export const literalSearchResults = {
  pattern: "token",
  regex: false,
  returned_docs: 2,
  returned_matches: 4,
  total_docs: 2,
  total_matches: 4,
  truncated: false,
  results: [
    {
      uri: "akb://akb/doc/runbooks/tokens.md",
      vault: "akb",
      path: "runbooks/tokens.md",
      title: "Token runbook",
      matches: [
        { section: "Rotation", text: "Rotate a personal access token every 90 days." },
        { section: "Scope", text: "Use the narrowest token scope that still works." },
      ],
    },
    {
      uri: "akb://research/doc/notes/oauth.md",
      vault: "research",
      path: "notes/oauth.md",
      title: "OAuth notes",
      matches: [
        { section: null, text: "The MCP OAuth token is stored by the client." },
        { section: null, text: "Refresh the token before it expires." },
      ],
    },
  ],
};

export const vaultInfo = {
  name: "akb",
  description: "Platform knowledge base for product, engineering, and agent memory.",
  role: "owner",
  member_count: 7,
  owner_display_name: "AKB Team",
  created_at: "2026-01-05T09:00:00.000Z",
  last_activity: "2026-07-06T04:10:00.000Z",
  collection_count: 6,
  document_count: 42,
  table_count: 2,
  file_count: 5,
  edge_count: 18,
  tables: [
    {
      name: "releases",
      row_count: 12,
      columns: [
        { name: "version", type: "text" },
        { name: "date", type: "date" },
      ],
    },
    {
      name: "tokens",
      row_count: 8,
      columns: [
        { name: "name", type: "text" },
        { name: "scope", type: "text" },
      ],
    },
  ],
};

export const emptyVaultInfo = {
  ...vaultInfo,
  name: "empty",
  description: "A newly created vault with only the starter guide.",
  role: "writer",
  member_count: 1,
  collection_count: 1,
  document_count: 1,
  table_count: 0,
  file_count: 0,
  edge_count: 0,
  tables: [],
};

export const archivedVaultInfo = {
  ...vaultInfo,
  name: "archive",
  role: "admin",
  is_archived: true,
  description: "Read-only archive of completed migration notes.",
};

export const recentChanges = {
  changes: [
    {
      doc_id: "d-guide",
      vault: "akb",
      path: "overview/vault-skill.md",
      title: "AKB Guide",
      type: "skill",
      commit: "9ad1204",
      changed_at: "2026-07-06T04:12:00.000Z",
    },
    {
      doc_id: "d-runbook",
      vault: "akb",
      path: "runbooks/deploy.md",
      title: "Deploy runbook",
      type: "runbook",
      commit: "7cc091a",
      changed_at: "2026-07-05T22:48:00.000Z",
    },
  ],
};

export const vaultHealth = {
  vector_store: {
    reachable: true,
    backfill: {
      upsert: { pending: 2, abandoned: 0 },
    },
  },
  metadata_backfill: { pending: 1 },
};

export const currentUser = {
  user_id: "u-jylkim",
  username: "jylkim",
  email: "jylkim@example.test",
  display_name: "JY Kim",
  is_admin: true,
};

export const nonAdminUser = {
  ...currentUser,
  user_id: "u-writer",
  username: "writer",
  email: "writer@example.test",
  display_name: "Story Writer",
  is_admin: false,
};

export const vaultSummaries = [
  { id: "v-akb", name: "akb", role: "owner" },
  { id: "v-research", name: "research", role: "writer" },
  { id: "v-archive", name: "archive", role: "reader" },
];

export const vaultBrowseItems = [
  { type: "collection", name: "overview", path: "overview" },
  { type: "collection", name: "runbooks", path: "runbooks" },
  {
    type: "document",
    name: "AKB Guide",
    path: "overview/vault-skill.md",
    uri: "akb://akb/coll/overview/doc/vault-skill.md",
    doc_type: "skill",
    summary: "Instructions that agents read before writing into the vault.",
  },
  {
    type: "document",
    name: "Deploy runbook",
    path: "runbooks/deploy.md",
    uri: "akb://akb/coll/runbooks/doc/deploy.md",
    doc_type: "runbook",
    summary: "Deployment checklist for the AKB stack.",
  },
  {
    type: "document",
    name: "Release note",
    path: "notes/release.md",
    uri: "akb://akb/coll/notes/doc/release.md",
    doc_type: "note",
    summary: "A regular document used by page stories.",
  },
  {
    type: "table",
    name: "releases",
    path: "releases",
    uri: "akb://akb/table/releases",
    row_count: 12,
  },
  {
    type: "file",
    name: "storybook.png",
    path: "figures/storybook.png",
    uri: "akb://akb/file/f-storybook",
    mime_type: "image/png",
  },
];

export const appChromeHandlers = [
  http.get("/health", () => HttpResponse.json(vaultHealth)),
  http.get(`${API}/auth/me`, () => HttpResponse.json(currentUser)),
  http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
  http.get(`${API}/auth/tokens`, () => HttpResponse.json({ tokens: [] })),
];

export const defaultVaultListHandler = http.get(`${API}/my/vaults`, () =>
  HttpResponse.json({ vaults: vaultSummaries }),
);

export const defaultBrowseHandler = http.get(`${API}/browse/:vault`, ({ params }) =>
  HttpResponse.json({
    vault: params.vault,
    path: "",
    items: vaultBrowseItems.map((item) => ({
      ...item,
      uri: typeof item.uri === "string" ? item.uri.replace("akb://akb/", `akb://${params.vault}/`) : item.uri,
    })),
  }),
);

export const defaultVaultInfoHandler = http.get(`${API}/vaults/akb/info`, () =>
  HttpResponse.json(vaultInfo),
);

export const defaultAnyVaultInfoHandler = http.get(`${API}/vaults/:vault/info`, ({ params }) =>
  HttpResponse.json({
    ...vaultInfo,
    name: params.vault,
    description: `${params.vault} story vault`,
  }),
);

export const defaultDocumentSupportHandlers = [
  http.get(`${API}/relations`, () => HttpResponse.json({ uri: "akb://akb/doc/overview/vault-skill.md", relations: [] })),
  http.get(`${API}/activity/:vault`, ({ params }) =>
    HttpResponse.json({ vault: params.vault, total: 0, activity: [] }),
  ),
];

export const appLayoutHandlers = [
  ...appChromeHandlers,
  defaultVaultListHandler,
];

// The vault page compares the stored guide against this to decide its
// template-vs-customized chip. Left unhandled, the SPA fallback answers with
// HTML and every guide reads as "customized".
export const skillTemplateHandler = http.get(`${API}/help/skill-template`, () =>
  HttpResponse.text("# {vault} Guide\n\n(Describe what this vault is for.)\n"),
);

export const vaultShellHandlers = [
  ...appLayoutHandlers,
  defaultBrowseHandler,
  skillTemplateHandler,
];

export const activePatTokens = [
  {
    token_id: "pat-1",
    name: "storybook-agent",
    prefix: "akb_pat_story",
    created_at: "2026-07-01T09:00:00.000Z",
    last_used_at: "2026-07-06T03:44:00.000Z",
  },
];

export const adminUsers = [
  {
    id: "u-jylkim",
    username: "jylkim",
    display_name: "JY Kim",
    email: "jylkim@example.test",
    is_admin: true,
    created_at: "2026-01-02T00:00:00.000Z",
    owned_vaults: 2,
  },
  {
    id: "u-writer",
    username: "writer",
    display_name: "Story Writer",
    email: "writer@example.test",
    is_admin: false,
    created_at: "2026-03-20T00:00:00.000Z",
    owned_vaults: 0,
  },
];

export const vaultMembers = [
  {
    username: "jylkim",
    display_name: "JY Kim",
    email: "jylkim@example.test",
    role: "owner",
    since: "2026-01-05T09:00:00.000Z",
  },
  {
    username: "writer",
    display_name: "Story Writer",
    email: "writer@example.test",
    role: "writer",
    since: "2026-02-11T10:30:00.000Z",
  },
  {
    username: "reader",
    display_name: "Story Reader",
    email: "reader@example.test",
    role: "reader",
    since: "2026-05-04T14:20:00.000Z",
  },
];

export const storyPublications = [
  {
    slug: "storybook-guide",
    share_url: "https://akb.example.test/p/storybook-guide",
    resource_type: "document",
    resource_uri: "akb://akb/doc/overview/vault-skill.md",
    vault: "akb",
    title: "Storybook rollout",
    mode: "live",
    expires_at: null,
    max_views: null,
    view_count: 128,
    allow_embed: true,
    section_filter: null,
    password_protected: false,
    created_at: "2026-07-06T04:30:00.000Z",
    snapshot_at: null,
  },
  {
    slug: "release-note",
    share_url: "https://akb.example.test/p/release-note",
    resource_type: "document",
    resource_uri: "akb://akb/doc/notes/release.md",
    vault: "akb",
    title: "Release note",
    mode: "snapshot",
    expires_at: "2026-08-01T00:00:00.000Z",
    max_views: 500,
    view_count: 12,
    allow_embed: false,
    section_filter: null,
    password_protected: true,
    created_at: "2026-07-05T20:00:00.000Z",
    snapshot_at: "2026-07-05T20:00:00.000Z",
  },
];

export const tableCatalog = [
  {
    name: "releases",
    description: "Release train status tracked from planning to ship.",
    row_count: 3,
    columns: [
      { name: "version", type: "text", primary_key: true },
      { name: "date", type: "date", required: true },
      { name: "status", type: "text" },
      { name: "owner", type: "text" },
    ],
  },
];

export const tableRows = [
  { version: "0.5.0", date: "2026-07-01", status: "shipped", owner: "platform" },
  { version: "0.6.0", date: "2026-07-15", status: "candidate", owner: "frontend" },
  { version: "0.7.0", date: "2026-08-02", status: "planned", owner: "search" },
];

export const storyFiles = [
  {
    uri: "akb://akb/file/f-storybook",
    name: "storybook.png",
    collection: "figures",
    description: "Screenshot captured from the Storybook rollout.",
    mime_type: "image/png",
    size_bytes: 184320,
    created_by: "u-jylkim",
    created_at: "2026-07-06T04:00:00.000Z",
  },
];

export const graphOverview = {
  nodes: [
    {
      uri: "akb://akb/doc/overview/vault-skill.md",
      name: "Storybook rollout",
      resource_type: "document",
    },
    {
      uri: "akb://akb/doc/runbooks/deploy.md",
      name: "Deploy runbook",
      resource_type: "document",
    },
    {
      uri: "akb://akb/table/releases",
      name: "releases",
      resource_type: "table",
    },
  ],
  edges: [
    {
      source: "akb://akb/doc/overview/vault-skill.md",
      target: "akb://akb/doc/runbooks/deploy.md",
      relation: "references",
      kind: "explicit",
    },
    {
      source: "akb://akb/doc/runbooks/deploy.md",
      target: "akb://akb/table/releases",
      relation: "depends_on",
      kind: "explicit",
    },
  ],
  nodes_total: 6,
  edges_total: 2,
  returned: 3,
  truncated: true,
  orphans_returned: 1,
  orphans_truncated: false,
};

export const emptyGraphOverview = {
  nodes: [],
  edges: [],
  nodes_total: 0,
  edges_total: 0,
  returned: 0,
  truncated: false,
};

export const publicDocument: PublicationResponse = {
  resource_type: "document",
  title: "Public Storybook Guide",
  type: "guide",
  domain: "frontend",
  created_by_name: "AKB Team",
  updated_at: "2026-07-06T04:00:00.000Z",
  tags: ["storybook", "design"],
  summary: "A public rendering of the AKB Storybook setup notes.",
  content: "# Public Storybook Guide\n\nThis is a public document render.",
};

export const publicSectionWarning: PublicationResponse = {
  ...publicDocument,
  title: "Section filtered guide",
  section_filter: "Missing section",
  section_not_found: true,
};

export const publicContentUnavailable: PublicationResponse = {
  ...publicDocument,
  title: "Removed document share",
  content_unavailable: true,
  content: "",
};
