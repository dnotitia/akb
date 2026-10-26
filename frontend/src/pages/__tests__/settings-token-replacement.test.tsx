import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Activity } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { configureAuthTransport, setToken } from "@/lib/api";
import { TokensSection } from "../settings/tokens-section";

const uid = "00000000-0000-0000-0000-000000000001", oldId = "00000000-0000-0000-0000-000000000002", newId = "00000000-0000-0000-0000-000000000003";
const caps = { contract_version: 1, user_id: uid, name_max_length: 255, permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"], vault_scope_semantics: "write_restriction_sql_read_write" };
const original = { token_id: oldId, name: "original", prefix: "akb_old", key_class: "pat", scopes: ["read"], vault_scope: { prefixes: ["team-"], extra_vaults: ["project"] }, expires_at: "2031-01-01T00:00:00.123456Z" };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const fetchMock = vi.fn();
beforeEach(() => { configureAuthTransport("local"); setToken("fixture-session"); fetchMock.mockReset(); fetchMock.mockImplementation(url => Promise.resolve(json(String(url).endsWith("/vaults") ? { vaults: [] } : caps))); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => { cleanup(); setToken(null); configureAuthTransport(null); vi.unstubAllGlobals(); });
function mount(pats = [original]) { render(<TokensSection pats={pats} patsError={false} mcpOauthEnabled={false} onReloadPats={vi.fn()} />); }

it("preserves the same-account replacement secret while checking and discards it on account-key replacement", async () => {
  const view = (id: string, active: boolean) => <Activity mode={active ? "visible" : "hidden"}><TokensSection key={id} pats={[original]} patsError={false} mcpOauthEnabled={false} onReloadPats={vi.fn()} /></Activity>;
  const { rerender } = render(view("a", true));
  fireEvent.click(screen.getByRole("button", { name: "Replace token original" }));
  const dialog = screen.getByRole("dialog", { name: "Replace token original" });
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "Create replacement token" })).toBeEnabled());
  fetchMock.mockImplementation(() => Promise.resolve(json({ contract_version: 1, user_id: uid, token_id: newId, name: "original", token: "akb_retained_secret", prefix: "akb_retained", issued_at: "2026-09-01T00:00:00.123456Z", key_class: "pat", scopes: original.scopes, vault_scope: original.vault_scope, expires_at: original.expires_at })));
  fireEvent.click(within(dialog).getByRole("button", { name: "Create replacement token" }));
  await within(dialog).findByText("Token created — save it now");
  rerender(view("a", false));
  expect(screen.queryByRole("dialog", { name: "Replace token original" })).not.toBeInTheDocument();
  expect(dialog).not.toBeVisible();
  expect(dialog.textContent).toContain("akb_retained_secret");
  rerender(view("a", true));
  expect(screen.getByRole("dialog", { name: "Replace token original" })).toBe(dialog);
  expect(dialog.textContent).toContain("akb_retained_secret");
  setToken("account-b");
  rerender(view("b", true));
  expect(document.body.textContent).not.toContain("akb_retained_secret");
  expect(screen.queryByRole("dialog", { name: "Replace token original" })).not.toBeInTheDocument();
});

it("keeps the original active if replacement creation fails", async () => {
  mount(); fireEvent.click(screen.getByRole("button", { name: "Replace token original" }));
  const dialog = screen.getByRole("dialog", { name: "Replace token original" });
  expect(within(dialog).getByText(`UTC: ${original.expires_at}`)).toBeVisible();
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "Create replacement token" })).toBeEnabled());
  fetchMock.mockImplementation(() => Promise.resolve(json({}, 503)));
  fireEvent.click(within(dialog).getByRole("button", { name: "Create replacement token" }));
  await within(dialog).findByText(/Creation result needs checking/);
  expect(fetchMock.mock.calls.filter(([, init]) => init.method === "DELETE")).toHaveLength(0);
  expect(screen.queryByRole("button", { name: "Revoke original token" })).not.toBeInTheDocument();
});

it("preserves restrictions and exact expiry, and keeps the new secret after original revocation fails", async () => {
  mount(); fireEvent.click(screen.getByRole("button", { name: "Replace token original" }));
  const dialog = screen.getByRole("dialog", { name: "Replace token original" });
  expect(within(dialog).getByText(`UTC: ${original.expires_at}`)).toBeVisible();
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "Create replacement token" })).toBeEnabled());
  fetchMock.mockImplementation((url, init) => Promise.resolve(init.method === "DELETE" ? json({ detail: "Temporary revocation failure" }, 503) : json({ contract_version: 1, user_id: uid, token_id: newId, name: "original", token: "akb_new_secret", prefix: "akb_new", issued_at: "2026-09-01T00:00:00.123456Z", key_class: "pat", scopes: original.scopes, vault_scope: original.vault_scope, expires_at: original.expires_at })));
  fireEvent.click(within(dialog).getByRole("button", { name: "Create replacement token" }));
  await screen.findByText("Token created — save it now");
  const posts = fetchMock.mock.calls.filter(([, init]) => init.method === "POST");
  expect(posts).toHaveLength(1);
  const submitted = JSON.parse(posts[0][1].body);
  expect(submitted.scopes).toEqual(original.scopes); expect(submitted.vault_scope).toEqual(original.vault_scope); expect(submitted.expires_at).toBe(original.expires_at);
  expect(fetchMock.mock.calls.filter(([, init]) => init.method === "DELETE")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Revoke original token" }));
  fireEvent.click(screen.getByRole("button", { name: "Revoke token" }));
  await screen.findByText("Temporary revocation failure");
  expect(document.body.textContent).toContain("akb_new_secret");
  const deletes = fetchMock.mock.calls.filter(([, init]) => init.method === "DELETE");
  expect(deletes).toHaveLength(1); expect(deletes[0][0]).toBe(`/api/v1/auth/tokens/${oldId}`);
});

it("disables replacement when a legacy token omitted its scope metadata", () => {
  render(<TokensSection pats={[{ token_id: oldId, name: "legacy", prefix: "akb_old" }]} patsError={false} mcpOauthEnabled={false} onReloadPats={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Replace token legacy" })).toBeDisabled();
  expect(screen.getByText(/Vault write restriction unknown/)).toBeVisible();
});


it("retains token rows during refresh failure and gives retry a busy state", () => {
  const reload = vi.fn();
  const view = (loading: boolean) => <TokensSection pats={[original]} patsError mcpOauthEnabled={false} patsLoading={loading} onReloadPats={reload} />;
  const { rerender } = render(view(false));
  expect(screen.getByRole("button", { name: "Revoke token original" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(reload).toHaveBeenCalledTimes(1);
  rerender(view(true));
  expect(screen.getByRole("button", { name: "Retry" })).toBeDisabled();
  expect(screen.getByRole("region", { name: "Saved tokens" })).toHaveAttribute("aria-busy", "true");
});
