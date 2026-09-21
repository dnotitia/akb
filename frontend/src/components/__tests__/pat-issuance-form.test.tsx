import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { configureAuthTransport, setToken } from "@/lib/api";
import { defaultPatDraft } from "@/lib/pat-draft";
import { PatIssuanceForm } from "../pat-issuance-form";
import { ConnectionSetup } from "../connection-setup";

const uid = "00000000-0000-0000-0000-000000000001", tid = "00000000-0000-0000-0000-000000000002";
const caps = { contract_version: 1, user_id: uid, name_max_length: 255, permission_presets: [["read"], ["read", "write"]], expiration_modes: ["none", "days", "absolute"], vault_scope_semantics: "write_restriction_sql_read_write" };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const fetchMock = vi.fn();
const posts = () => fetchMock.mock.calls.filter(([, init]) => init.method === "POST");
beforeEach(() => { configureAuthTransport("local"); setToken("fixture-session"); fetchMock.mockReset(); fetchMock.mockImplementation(() => Promise.resolve(json(caps))); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => { setToken(null); configureAuthTransport(null); vi.unstubAllGlobals(); });

it("keeps selected SQL restrictions when choosing read only and retains the collapsed summary", async () => {
  const user = userEvent.setup();
  render(<PatIssuanceForm initial={{ ...defaultPatDraft(), name: "x", restricted: true, prefixes: ["team-"] }} onCreated={vi.fn()} />);
  await user.click(screen.getByLabelText("Permissions"));
  await user.click(screen.getByRole("menuitemradio", { name: "Read only" }));
  await user.click(screen.getByRole("button", { name: "Advanced options" }));
  expect(screen.getByRole("button", { name: "Advanced options" })).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText(/No expiration · Read only · Writes: prefix team-/)).toBeVisible();
  expect(screen.getByText(/Writes are disabled/)).toHaveTextContent("Ordinary document reads are not restricted");
});

it("keeps selections and supports manual prefixes when the accessible Vault list fails", async () => {
  fetchMock.mockImplementation(url => Promise.resolve(String(url).endsWith("/vaults") ? json({}, 503) : json(caps)));
  const user = userEvent.setup();
  render(<PatIssuanceForm initial={{ ...defaultPatDraft(), name: "x", restricted: true, vaults: ["kept-vault"] }} onCreated={vi.fn()} />);
  await screen.findByText(/Could not load Vaults/);
  await user.type(screen.getByLabelText("Name prefix"), "future-");
  await user.click(screen.getByRole("button", { name: "Add prefix" }));
  expect(screen.getByText(/No expiration · Read and write · Writes: kept-vault OR prefix future-/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Remove Vault kept-vault" })).toBeVisible();
});

it("requires explicit reset of a restricted legacy draft and sends only name", async () => {
  fetchMock.mockImplementation(url => Promise.resolve(String(url).includes("capabilities") ? json({}, 404) : json({ token_id: tid, name: "x", token: "akb_legacy_secret", prefix: "akb_legacy" })));
  const user = userEvent.setup();
  const onCreated = vi.fn();
  render(<PatIssuanceForm initial={{ ...defaultPatDraft(), name: "x", permissions: "read" }} onCreated={onCreated} />);
  expect(await screen.findByRole("button", { name: "Create with server defaults" })).toBeDisabled();
  expect(posts()).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Reset to server defaults" }));
  await user.click(screen.getByRole("button", { name: "Create with server defaults" }));
  await waitFor(() => expect(onCreated).toHaveBeenCalled());
  expect(posts()).toHaveLength(1); expect(JSON.parse(posts()[0][1].body)).toEqual({ name: "x" });
  expect(onCreated.mock.calls[0][0].verified).toBe(false);
});

it("does not fall back when the issuance request lands on an old server", async () => {
  fetchMock.mockImplementation((url, init) => Promise.resolve(init.method === "POST" ? json({ detail: "Not found" }, 404) : json(caps)));
  render(<PatIssuanceForm initial={{ ...defaultPatDraft(), name: "x", permissions: "read" }} onCreated={vi.fn()} />);
  const create = await screen.findByRole("button", { name: "Create token" });
  await waitFor(() => expect(create).toBeEnabled()); fireEvent.click(create);
  await screen.findByText("Not found");
  expect(posts()).toHaveLength(1); expect(posts()[0][0]).toContain("/issuance");
  expect(screen.getByLabelText("Permissions")).toHaveTextContent("Read only");
});

it("maps stable field errors to controls and focuses the linked error summary", async () => {
  fetchMock.mockImplementation((url, init) => Promise.resolve(init.method === "POST" ? json({ message: "Invalid request", code: "token_issuance_validation", details: { fields: [{ field: "expires_at", code: "past", message: "Choose a future expiration." }] } }, 422) : json(caps)));
  render(<PatIssuanceForm initial={{ ...defaultPatDraft(), name: "x", expiration: "30" }} onCreated={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Create token" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Create token" }));
  await screen.findByRole("link", { name: "Choose a future expiration." });
  expect(screen.getByLabelText("Expiration")).toHaveAttribute("aria-invalid", "true");
  expect(document.activeElement?.textContent).toContain("Invalid request");
});

it("prevents duplicate issuance and requires checking an uncertain result before another creation", async () => {
  let resolve!: (response: Response) => void;
  fetchMock.mockImplementation((url, init) => init.method === "POST" ? new Promise<Response>(r => { resolve = r; }) : Promise.resolve(json(caps)));
  render(<PatIssuanceForm initial={{ ...defaultPatDraft(), name: "x" }} onCreated={vi.fn()} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Create token" })).toBeEnabled());
  const form = screen.getByLabelText("Token name").closest("form")!;
  fireEvent.submit(form); fireEvent.submit(form);
  expect(posts()).toHaveLength(1);
  await act(async () => resolve(json({}, 503)));
  expect(screen.getByRole("link", { name: "Review token list" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Create token" })).not.toBeInTheDocument();
});

it("removes masked secrets from the DOM and clears token-bearing snippets after a session change", async () => {
  fetchMock.mockImplementation((url, init) => Promise.resolve(init.method === "POST" ? json({ contract_version: 1, user_id: uid, token_id: tid, name: "x", token: "akb_one_time_secret", prefix: "akb_one", issued_at: "2030-01-01T00:00:00Z", key_class: "pat", scopes: ["read", "write"], vault_scope: null, expires_at: null }) : json(caps)));
  const user = userEvent.setup();
  render(<ConnectionSetup mcpOauthEnabled={false} />);
  await user.type(screen.getByLabelText("Token name"), "x");
  await user.click(screen.getByRole("button", { name: "Create token" }));
  await screen.findByText("Token created — save it now");
  await user.click(screen.getByRole("button", { name: "Hide token and configuration" }));
  expect(document.body.textContent).not.toContain("akb_one_time_secret");
  await user.click(screen.getByRole("button", { name: "Show token and configuration" }));
  expect(document.body.textContent).toContain("akb_one_time_secret");
  setToken("new-same-account-session"); fireEvent.focus(window);
  await screen.findByText(/Previous secrets and configuration have been cleared/);
  expect(document.body.textContent).not.toContain("akb_one_time_secret");
});

// These switches must not reset the issuance session or bypass uncertain-result review.
it.each(["saved token", "OAuth"])("preserves the draft and uncertain result across %s switches", async mode => {
  const user = userEvent.setup();
  const dirty = vi.fn();
  render(<ConnectionSetup mcpOauthEnabled={mode === "OAuth"} onDirtyChange={dirty}
    initialDraft={{ ...defaultPatDraft(), name: "limited", permissions: "read", expiration: "30", restricted: true, prefixes: ["team-"] }} />);
  if (mode === "OAuth") {
    await user.click(screen.getByLabelText("Sign-in method"));
    await user.click(screen.getByRole("menuitemradio", { name: "Access token" }));
  }
  await waitFor(() => expect(screen.getByRole("button", { name: "Create token" })).toBeEnabled());
  fetchMock.mockImplementation((url, init) => Promise.resolve(init.method === "POST" ? json({}, 503) : String(url).endsWith("/vaults") ? json({ vaults: [] }) : json(caps)));
  await user.click(screen.getByRole("button", { name: "Create token" }));
  await screen.findByText(/Creation result needs checking/);
  const switchLabel = mode === "OAuth" ? "Sign-in method" : "Access token";
  await user.click(screen.getByLabelText(switchLabel, { exact: true }));
  await user.click(screen.getByRole("menuitemradio", { name: mode === "OAuth" ? "Browser sign-in (OAuth)" : "Use a saved token" }));
  expect(dirty).toHaveBeenLastCalledWith(true);
  expect(screen.queryByRole("button", { name: "I've checked — review another creation" })).not.toBeInTheDocument();
  await user.click(screen.getByLabelText(switchLabel, { exact: true }));
  await user.click(screen.getByRole("menuitemradio", { name: mode === "OAuth" ? "Access token" : "Create a new token" }));
  expect(screen.getByText(/Creation result needs checking/)).toBeVisible();
  expect(screen.getByLabelText("Token name")).toHaveValue("limited");
  expect(screen.getByText(/Expires 30 × 24 hours after issuance · Read only · Writes: prefix team-/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "Create token" })).not.toBeInTheDocument();
  expect(posts()).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "I've checked — review another creation" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Create token" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "Create token" }));
  await waitFor(() => expect(posts()).toHaveLength(2));
  expect(JSON.parse(posts()[1][1].body)).toMatchObject({ name: "limited", scopes: ["read"], expires_days: 30, vault_scope: { prefixes: ["team-"], extra_vaults: [] } });
});
