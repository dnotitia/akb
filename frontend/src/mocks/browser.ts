import { setupWorker } from "msw/browser";
import { http, HttpResponse } from "msw";
import {
  currentUser as fixtureUser,
  localAuthConfig,
  vaultHealth,
} from "@/stories/page-story-fixtures";

const API = "/api/v1";
const MOCK_TOKEN = "akb-mock-browser-token";

type MockUser = typeof fixtureUser & { auth_method: "local" };

const initialUser: MockUser = { ...fixtureUser, auth_method: "local" };
let user: MockUser = { ...initialUser };

function resetState() {
  user = { ...initialUser };
}

async function jsonBody(request: Request): Promise<Record<string, unknown>> {
  return (await request.json()) as Record<string, unknown>;
}

const handlers = [
  http.get("/__akb_mock__/health", () =>
    HttpResponse.json({ status: "ready", mode: "mock" }),
  ),
  http.get("/__akb_mock__/discover", () =>
    HttpResponse.json({
      mode: "mock",
      status: "ready",
      worker: { url: "/mockServiceWorker.js", unhandled_api: "error" },
      reset: { method: "POST", path: "/__akb_mock__/reset" },
    }),
  ),
  http.post("/__akb_mock__/reset", () => {
    resetState();
    return HttpResponse.json({ status: "ready", mode: "mock" });
  }),
  http.get(`${API}/auth/config`, () => HttpResponse.json(localAuthConfig)),
  http.get(`${API}/auth/me`, () => HttpResponse.json(user)),
  http.post(`${API}/auth/register`, async ({ request }) => {
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
  http.post(`${API}/auth/login`, () => HttpResponse.json({ token: MOCK_TOKEN })),
  http.patch(`${API}/auth/me`, async ({ request }) => {
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
  http.get(`${API}/my/vaults`, () => HttpResponse.json({ vaults: [] })),
  http.get(`${API}/recent`, () => HttpResponse.json({ changes: [] })),
  http.get(`${API}/auth/tokens`, () => HttpResponse.json({ tokens: [] })),
  http.get(`${API}/search`, ({ request }) => {
    const url = new URL(request.url);
    const title = url.searchParams.has("doc_types")
      ? "Report outside initial top 25"
      : "Initial result";
    return HttpResponse.json({
      query: "deployment",
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
      regex: url.searchParams.get("regex") === "true",
      total_docs: 0,
      total_matches: 0,
      returned_docs: 0,
      returned_matches: 0,
      results: [],
    });
  }),
  http.get("/health", () => HttpResponse.json(vaultHealth)),
  // Mock product APIs never fall through to a real backend.
  http.all(`${API}/*`, () =>
    HttpResponse.json({ error: "unhandled_mock_api_request" }, { status: 501 }),
  ),
];

const worker = setupWorker(...handlers);

export async function startMockWorker() {
  resetState();
  await worker.start({
    onUnhandledRequest: "error",
    serviceWorker: { url: "/mockServiceWorker.js" },
  });
}
