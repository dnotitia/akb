import { setupWorker } from "msw/browser";
import { http, HttpResponse } from "msw";
import {
  currentUser as fixtureUser,
  localAuthConfig,
  vaultHealth,
} from "@/stories/page-story-fixtures";
import {
  DOCUMENT_EDIT_DRAFT_EDITOR_VERSION,
  DOCUMENT_EDIT_DRAFT_MARKDOWN_PROFILE,
  createDocumentEditDraftId,
  documentEditDraftStorageKey,
  documentEditDraftTabId,
} from "@/lib/document-draft";

const API = "/api/v1";
const MOCK_TOKEN = "akb-mock-browser-token";

type MockUser = typeof fixtureUser & { auth_method: "local" };

const initialUser: MockUser = { ...fixtureUser, auth_method: "local" };
let user: MockUser = { ...initialUser };
let appliedResetGeneration = 0;
let activeScenario = "empty";
let appliedRefetchGeneration = 0;
let appliedExpireDraftGeneration = 0;
let fixturePoll: number | null = null;

const documentScenarioAuthConfig = {
  schema_version: 2 as const,
  auth_mode: "sso" as const,
  local_auth: { enabled: false },
  keycloak: { enabled: true, browser_session_ready: true },
  providers: [],
  mcp_oauth: { enabled: false },
};

type FixtureDocument = {
  title: string;
  content: string;
  current_commit: string;
  updated_at: string;
};

type FixtureState = {
  scenario?: string;
  identity?: {
    user_id?: string;
    vault?: string;
    document_path?: string;
    document_uri?: string;
  };
  document?: FixtureDocument;
  refetch_generation?: number;
  expire_draft_generation?: number;
  faults?: { save?: number; upload?: number };
  assets?: Array<{ id: string; filename: string; status: string }>;
};

let fixtureState: FixtureState = {};

const baseDocument = {
  kind: "document" as const,
  uri: "akb://fixture/coll/notes/doc/recovery.md",
  vault: "fixture",
  path: "notes/recovery.md",
  title: "Recovery document",
  type: "note",
  status: "active",
  summary: "A source-neutral document recovery fixture.",
  domain: null,
  created_by: "u-jylkim",
  created_by_name: "JY Kim",
  created_at: "2026-09-08T00:00:00.000Z",
  updated_at: "2026-09-08T00:00:00.000Z",
  current_commit: "fixture-base-0001",
  content_hash: null,
  hash_algorithm: "sha256",
  tags: ["fixture"],
  content: "# Recovery document\n\nThe original body is safe to edit.",
  is_public: false,
  public_slug: null,
  metadata_is_current: true,
};

function documentForState(): typeof baseDocument {
  const remote = fixtureState.document;
  return remote
    ? { ...baseDocument, ...remote }
    : { ...baseDocument };
}

function isDocumentScenario(): boolean {
  return activeScenario === "document-edit-recovery";
}

function documentIdentity(): string {
  return fixtureState.identity?.document_uri || baseDocument.uri;
}

async function consumeFixtureFault(operation: "save" | "upload"): Promise<boolean> {
  try {
    const response = await fetch("/__akb_mock__/fixture/consume-fault", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operation }),
    });
    if (!response.ok) return false;
    const body = (await response.json()) as { consumed?: unknown };
    return body.consumed === true;
  } catch {
    return false;
  }
}

async function syncFixtureState(): Promise<FixtureState> {
  try {
    const response = await fetch("/__akb_mock__/fixture/state", { cache: "no-store" });
    if (!response.ok) return fixtureState;
    const next = (await response.json()) as FixtureState & { reset_generation?: unknown };
    const resetGeneration = next.reset_generation;
    if (
      typeof resetGeneration === "number" &&
      Number.isInteger(resetGeneration) &&
      resetGeneration > appliedResetGeneration
    ) {
      resetState();
      appliedResetGeneration = resetGeneration;
      appliedRefetchGeneration = 0;
      appliedExpireDraftGeneration = 0;
    }
    fixtureState = next;
    const refetchGeneration = next.refetch_generation;
    if (
      isDocumentScenario() &&
      typeof refetchGeneration === "number" &&
      refetchGeneration > appliedRefetchGeneration
    ) {
      appliedRefetchGeneration = refetchGeneration;
      window.dispatchEvent(
        new CustomEvent("akb:mock-document-refetch", {
          detail: { vault: "fixture", document: "notes/recovery.md" },
        }),
      );
    }
    const expireGeneration = next.expire_draft_generation;
    if (
      isDocumentScenario() &&
      typeof expireGeneration === "number" &&
      expireGeneration > appliedExpireDraftGeneration
    ) {
      appliedExpireDraftGeneration = expireGeneration;
      seedExpiredDraft();
    }
    return fixtureState;
  } catch {
    return fixtureState;
  }
}

function seedExpiredDraft() {
  if (!isDocumentScenario()) return;
  const document = documentForState();
  const tabId = documentEditDraftTabId();
  const draft = {
    version: 1,
    kind: "document-edit",
    draftId: createDocumentEditDraftId(),
    tabId,
    userId: fixtureState.identity?.user_id || initialUser.user_id,
    vault: "fixture",
    document: documentIdentity(),
    baseCommit: document.current_commit,
    baseTitle: document.title,
    baseBody: document.content,
    title: document.title,
    body: `${document.content}\n\nExpired draft recovery text.`,
    assetIds: [],
    editorVersion: DOCUMENT_EDIT_DRAFT_EDITOR_VERSION,
    markdownProfile: DOCUMENT_EDIT_DRAFT_MARKDOWN_PROFILE,
    updatedAt: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
    expiresAt: new Date(Date.now() - 60_000).toISOString(),
  };
  try {
    window.localStorage.setItem(
      documentEditDraftStorageKey("u-jylkim", "fixture", documentIdentity(), tabId, draft.draftId),
      JSON.stringify(draft),
    );
  } catch {
    // The browser will surface the same local-storage-unavailable state as a
    // real user when the test fixture cannot write a draft.
  }
}

function resetState() {
  user = { ...initialUser };
}

async function syncPublicReset() {
  await syncFixtureState();
}

async function jsonBody(request: Request): Promise<Record<string, unknown>> {
  return (await request.json()) as Record<string, unknown>;
}

const handlers = [
  http.get(`${API}/auth/config`, async () => {
    await syncPublicReset();
    return HttpResponse.json(isDocumentScenario() ? documentScenarioAuthConfig : localAuthConfig);
  }),
  http.get(`${API}/auth/me`, async () => {
    await syncPublicReset();
    return HttpResponse.json(user);
  }),
  http.post(`${API}/auth/register`, async ({ request }) => {
    await syncPublicReset();
    const body = await jsonBody(request);
    user = {
      ...user,
      username: typeof body.username === "string" ? body.username : user.username,
      email: typeof body.email === "string" ? body.email : user.email,
      display_name:
        typeof body.display_name === "string" ? body.display_name : user.display_name,
    };
    return HttpResponse.json({ token: MOCK_TOKEN });
  }),
  http.post(`${API}/auth/login`, async () => {
    await syncPublicReset();
    return HttpResponse.json({ token: MOCK_TOKEN });
  }),
  http.patch(`${API}/auth/me`, async ({ request }) => {
    await syncPublicReset();
    const body = await jsonBody(request);
    user = {
      ...user,
      display_name:
        typeof body.display_name === "string" ? body.display_name : user.display_name,
      email: typeof body.email === "string" ? body.email : user.email,
    };
    return HttpResponse.json({
      updated: true,
      username: user.username,
      display_name: user.display_name,
      email: user.email,
    });
  }),
  http.get(`${API}/my/vaults`, async () => {
    await syncPublicReset();
    return HttpResponse.json({
      vaults: isDocumentScenario()
        ? [{ name: "fixture", role: "owner", description: "Document recovery fixture" }]
        : [],
    });
  }),
  http.get(`${API}/vaults/fixture/info`, async () => {
    await syncPublicReset();
    return HttpResponse.json({
      name: "fixture",
      description: "Document recovery fixture",
      role: "owner",
      member_count: 1,
      owner_display_name: "JY Kim",
      collection_count: 1,
      document_count: 1,
      table_count: 0,
      file_count: 0,
      edge_count: 0,
      is_archived: false,
      is_external_git: false,
      tables: [],
    });
  }),
  http.get(/\/api\/v1\/browse\/fixture(?:\?.*)?$/, async () => {
    await syncPublicReset();
    return HttpResponse.json({
      vault: "fixture",
      path: "",
      items: [
        { type: "collection", name: "notes", path: "notes", doc_count: 1 },
        { type: "document", name: documentForState().title, path: "notes/recovery.md" },
      ],
    });
  }),
  http.get(/\/api\/v1\/documents\/fixture\/.+$/, async () => {
    await syncPublicReset();
    return HttpResponse.json(documentForState());
  }),
  http.patch(/\/api\/v1\/documents\/fixture\/.+$/, async ({ request }) => {
    await syncPublicReset();
    if (await consumeFixtureFault("save")) {
      return HttpResponse.json(
        { detail: { message: "Temporary fixture server failure", code: "fixture_failure" } },
        { status: 503 },
      );
    }
    const body = await jsonBody(request);
    const latest = documentForState();
    if (
      typeof body.expected_commit === "string" &&
      body.expected_commit !== latest.current_commit
    ) {
      return HttpResponse.json(
        { detail: { message: "current_commit moved", code: "conflict" } },
        { status: 409 },
      );
    }
    const assetIds = typeof body.content === "string"
      ? [...body.content.matchAll(/\/api\/assets\/([A-Za-z0-9-]+)/g)].map((match) => match[1])
      : [];
    try {
      const commit = await fetch("/__akb_mock__/fixture/commit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, asset_ids: assetIds }),
      });
      if (!commit.ok) {
        const error = await commit.json().catch(() => ({ error: "fixture_commit_failed" }));
        return HttpResponse.json(error, { status: commit.status });
      }
      const result = (await commit.json()) as { document?: FixtureDocument };
      return HttpResponse.json({
        kind: "document_write",
        path: "notes/recovery.md",
        current_commit: result.document?.current_commit,
        commit_hash: result.document?.current_commit,
      });
    } catch {
      return HttpResponse.json({ error: "fixture_commit_failed" }, { status: 503 });
    }
  }),
  http.get(/\/api\/v1\/history\/fixture\/.+$/, async () => {
    await syncPublicReset();
    const document = documentForState();
    return HttpResponse.json({
      kind: "document_history",
      uri: document.uri || baseDocument.uri,
      history: [
        {
          hash: document.current_commit,
          message: "Fixture document revision",
          author: "fixture",
          author_name: "Fixture editor",
          date: document.updated_at,
        },
      ],
    });
  }),
  http.get(`${API}/relations`, async () => {
    await syncPublicReset();
    return HttpResponse.json({ uri: baseDocument.uri, relations: [] });
  }),
  http.post(`${API}/assets/fixture`, async ({ request }) => {
    await syncPublicReset();
    if (await consumeFixtureFault("upload")) {
      return HttpResponse.json(
        { detail: { message: "Temporary fixture upload failure", code: "fixture_failure" } },
        { status: 503 },
      );
    }
    const url = new URL(request.url);
    const filename = url.searchParams.get("filename") || "fixture.png";
    const id = `00000000-0000-4000-8000-${String(Date.now() % 1_000_000_000_000).padStart(12, "0")}`;
    try {
      await fetch("/__akb_mock__/fixture/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, filename }),
      });
    } catch {
      return HttpResponse.json({ error: "fixture_upload_failed" }, { status: 503 });
    }
    return HttpResponse.json({
      id,
      url: `/api/assets/${id}`,
      name: filename,
      mime_type: request.headers.get("content-type") || "image/png",
      size_bytes: Number(request.headers.get("content-length") || 0),
    });
  }),
  http.delete(/\/api\/v1\/assets\/fixture\/.+$/, async ({ request }) => {
    await syncPublicReset();
    const id = new URL(request.url).pathname.split("/").at(-1) || "";
    const response = await fetch("/__akb_mock__/fixture/discard", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    return HttpResponse.json(await response.json());
  }),
  http.get(/\/api\/assets\/.+$/, async ({ request }) => {
    await syncPublicReset();
    const id = new URL(request.url).pathname.split("/").at(-1) || "";
    const asset = fixtureState.assets?.find((candidate) => candidate.id === id);
    if (!asset || asset.status === "discarded") {
      return new HttpResponse(null, { status: 404 });
    }
    return new HttpResponse(new Blob(["fixture-image"], { type: "image/png" }), {
      headers: { "Content-Type": "image/png" },
    });
  }),
  http.get(`${API}/recent`, () => HttpResponse.json({ changes: [] })),
  http.get(`${API}/auth/tokens`, () => HttpResponse.json({ tokens: [] })),
  http.get(`${API}/search`, ({ request }) => {
    const url = new URL(request.url);
    const title = url.searchParams.has("doc_types")
      ? "Report outside initial top 25"
      : "Initial result";
    return HttpResponse.json({
      query: "deployment",
      archive_scope: url.searchParams.get("archive_scope") || "unarchived",
      total: 1,
      returned: 1,
      total_matches: 30,
      truncated: true,
      results: [
        {
          title,
          uri: "akb://fixture/doc/report.md",
          vault: "fixture",
          path: "report.md",
          doc_type: "report",
          source_type: "document",
          score: 1,
        },
      ],
    });
  }),
  http.get(`${API}/grep`, ({ request }) => {
    const url = new URL(request.url);
    return HttpResponse.json({
      pattern: "deployment",
      archive_scope: url.searchParams.get("archive_scope") || "unarchived",
      regex: url.searchParams.get("regex") === "true",
      total_docs: 0,
      total_matches: 0,
      returned_docs: 0,
      returned_matches: 0,
      results: [],
    });
  }),
  http.get("/health", () => HttpResponse.json(vaultHealth)),
  http.get(/\/health\/vault\/fixture(?:\?.*)?$/, () => HttpResponse.json(vaultHealth)),
  // Mock product APIs never fall through to a real backend.
  http.all(`${API}/*`, () =>
    HttpResponse.json({ error: "unhandled_mock_api_request" }, { status: 501 }),
  ),
];

const worker = setupWorker(...handlers);

export async function startMockWorker() {
  resetState();
  appliedResetGeneration = 0;
  appliedRefetchGeneration = 0;
  appliedExpireDraftGeneration = 0;
  fixtureState = {};
  try {
    const discovery = await fetch("/__akb_mock__/discover", { cache: "no-store" });
    if (discovery.ok) {
      const descriptor = (await discovery.json()) as { scenario?: unknown };
      activeScenario = typeof descriptor.scenario === "string" ? descriptor.scenario : "empty";
    }
  } catch {
    activeScenario = "empty";
  }
  await worker.start({
    onUnhandledRequest: "bypass",
    serviceWorker: { url: "/mockServiceWorker.js" },
  });
  await syncFixtureState();
  if (fixturePoll !== null) window.clearInterval(fixturePoll);
  fixturePoll = window.setInterval(() => {
    void syncFixtureState();
  }, 250);
}
