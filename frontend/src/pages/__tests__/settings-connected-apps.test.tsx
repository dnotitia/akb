import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { configureAuthTransport, setToken } from "@/lib/api";
import { notificationPresentation } from "@/lib/notification-presentation";
import { TokensSection } from "../settings/tokens-section";

const uid = "00000000-0000-0000-0000-000000000001";
const keyId = "00000000-0000-0000-0000-0000000000a1";
const unlistedKeyId = "00000000-0000-0000-0000-0000000000a2";
const caps = { contract_version: 1, user_id: uid, name_max_length: 255, permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"], vault_scope_semantics: "write_restriction_sql_read_write" };
const laptopKey = { token_id: keyId, name: "laptop", prefix: "akb_lap", key_class: "pat", scopes: ["read", "write"], vault_scope: null, expires_at: null, created_at: "2026-10-01T00:00:00Z", last_used_at: null };
const oauthApp = { id: "c-oauth", client_name: "claude-code", client_version: "2.1.0", auth_kind: "oauth", token_id: null, token_prefix: null, token_name: null, first_seen_at: "2026-11-01T09:00:00Z", last_seen_at: "2026-11-15T09:00:00Z" };
const keyApp = { id: "c-pat", client_name: "codex", client_version: "0.46.0", auth_kind: "pat", token_id: keyId, token_prefix: "akb_lap", token_name: "laptop", first_seen_at: "2026-11-02T09:00:00Z", last_seen_at: "2026-11-14T09:00:00Z" };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

const fetchMock = vi.fn();
let connections: unknown[] = [];
let connectionsStatus = 200;
function route(url: string, init?: RequestInit) {
  if (url.endsWith("/auth/connections")) return json(connectionsStatus === 200 ? { connections } : { detail: "Not Found" }, connectionsStatus);
  if (init?.method === "DELETE") { connections = connections.filter(item => (item as { token_id: string | null }).token_id !== keyId); return json({ revoked: true }); }
  if (url.endsWith("/vaults")) return json({ vaults: [] });
  return json(caps);
}
beforeEach(() => {
  configureAuthTransport("local"); setToken("fixture-session");
  connections = [oauthApp, keyApp]; connectionsStatus = 200;
  fetchMock.mockReset(); fetchMock.mockImplementation((url, init) => Promise.resolve(route(String(url), init))); vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); setToken(null); configureAuthTransport(null); vi.unstubAllGlobals(); });

const connectionReads = () => fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/auth/connections"));
function mount(pats = [laptopKey], onReloadPats = vi.fn()) {
  return render(<TokensSection initialView="apps" pats={pats} patsError={false} mcpOauthEnabled={false} onReloadPats={onReloadPats} />);
}

it("lists the AI apps that used this account, most recent first, with how each connects", async () => {
  mount();
  const section = await screen.findByRole("region", { name: "Connected apps" });
  await within(section).findByText("claude-code");
  const rows = within(within(section).getByRole("list", { name: "Connected apps" })).getAllByRole("listitem");
  expect(rows).toHaveLength(2);
  expect(within(rows[0]).getByText("claude-code")).toBeVisible();
  expect(within(rows[0]).getByText("2.1.0")).toBeVisible();
  expect(within(rows[0]).getByText("Sign-in")).toBeVisible();
  // Sign-in apps are not disconnected from here; key-based ones are.
  expect(within(rows[0]).queryByRole("button", { name: /Disconnect/ })).not.toBeInTheDocument();
  expect(within(rows[1]).getByText("codex")).toBeVisible();
  expect(within(rows[1]).getByText("Connection key")).toBeVisible();
  expect(within(rows[1]).getByText(/Key laptop/)).toBeVisible();
  expect(within(rows[1]).getByRole("button", { name: "Disconnect codex" })).toBeEnabled();
  expect(within(section).getByText("2")).toBeVisible();
  expect(connectionReads()).toHaveLength(1);
});

it("disconnects a key-based app by revoking its key through the existing confirmation", async () => {
  const reload = vi.fn().mockResolvedValue(true);
  mount([laptopKey], reload);
  fireEvent.click(await screen.findByRole("button", { name: "Disconnect codex" }));
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(0);
  const confirm = screen.getByRole("dialog", { name: 'Revoke "laptop"?' });
  fireEvent.click(within(confirm).getByRole("button", { name: "Revoke key" }));
  await waitFor(() => expect(screen.queryByText("codex")).not.toBeInTheDocument());
  const deletes = fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE");
  expect(deletes).toHaveLength(1);
  expect(deletes[0][0]).toBe(`/api/v1/auth/tokens/${keyId}`);
  expect(reload).toHaveBeenCalled();
  await waitFor(() => expect(connectionReads()).toHaveLength(2));
  expect(screen.getByText("claude-code")).toBeVisible();
});

it("does not offer to disconnect a key that is not in the key list", async () => {
  connections = [{ ...keyApp, token_id: unlistedKeyId }];
  mount([laptopKey]);
  expect(await screen.findByRole("button", { name: "Disconnect codex" })).toBeDisabled();
});

it("shows an empty state before any app has connected", async () => {
  connections = [];
  mount();
  expect(await screen.findByText("No AI apps connected yet")).toBeVisible();
});

it("explains an older server without the registry instead of failing", async () => {
  connectionsStatus = 404;
  mount();
  expect(await screen.findByText("Connected apps unavailable")).toBeVisible();
  expect(screen.getByText("Connected apps are not available on this server yet.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
});

it("loads only when the Connected apps view is shown", async () => {
  render(<TokensSection initialView="keys" pats={[laptopKey]} patsError={false} mcpOauthEnabled={false} onReloadPats={vi.fn()} />);
  await screen.findByRole("button", { name: "Revoke key laptop" });
  expect(connectionReads()).toHaveLength(0);
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Connected apps" }));
  expect(await screen.findByText("claude-code")).toBeVisible();
  expect(connectionReads()).toHaveLength(1);
});

it("labels a new-connection notice", () => {
  expect(notificationPresentation("account.ai_connection").label).toBe("New AI app");
});
