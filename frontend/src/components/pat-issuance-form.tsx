import { useEffect, useId, useRef, useState } from "react";
import { ApiError, authSessionSnapshot, isCurrentAuthSession, type AuthSessionSnapshot } from "@/lib/api";
import { getPatCapabilities, issueLegacyPat, issuePat, listIssuanceVaults, revokeIssuedPat, UnverifiedPatReceipt, type PatCapabilities, type PatReceipt } from "@/lib/api-pat-issuance";
import { defaultPatDraft, draftSummary, customExpiration, validatePatDraft, type PatDraft, type PatFieldErrors } from "@/lib/pat-draft";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SelectMenu } from "@/components/ui/select-menu";

export function PatIssuanceForm({ initial, replacement = false, onCreated, onMetadataChanged, onDirtyChange, onBusyChange }: {
  initial?: PatDraft; replacement?: boolean; onCreated: (receipt: PatReceipt, snapshot: AuthSessionSnapshot) => void;
  onMetadataChanged?: () => void; onDirtyChange?: (dirty: boolean) => void; onBusyChange?: (busy: boolean) => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState<PatDraft>(() => initial ?? defaultPatDraft());
  const [advanced, setAdvanced] = useState(!!initial);
  const [capability, setCapability] = useState<{ data: PatCapabilities | "legacy"; snapshot: AuthSessionSnapshot } | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const mounted = useRef(true);
  const discovery = useRef(0);
  const errorSummary = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");
  const [fields, setFields] = useState<PatFieldErrors>({});
  const [uncertain, setUncertain] = useState(false);
  const [createdCandidate, setCreatedCandidate] = useState<{ id: string; snapshot: AuthSessionSnapshot } | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [vaultNames, setVaultNames] = useState<string[] | null>(null);
  const [vaultError, setVaultError] = useState(false);
  const [vaultLoading, setVaultLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [exact, setExact] = useState("");
  const [prefix, setPrefix] = useState("");
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const dirty = JSON.stringify(draft) !== JSON.stringify(defaultPatDraft()) || !!exact || !!prefix || uncertain;
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => () => { onDirtyChange?.(false); }, [onDirtyChange]);
  async function discover() {
    const sequence = ++discovery.current;
    const snapshot = authSessionSnapshot();
    setLoading(true); setError(""); setCapability(null);
    try {
      const data = await getPatCapabilities(snapshot);
      if (mounted.current && sequence === discovery.current && isCurrentAuthSession(snapshot)) setCapability({ data, snapshot });
    } catch (e) { if (mounted.current && sequence === discovery.current) setError(e instanceof Error ? e.message : "Could not verify token issuance support."); }
    finally { if (mounted.current && sequence === discovery.current) setLoading(false); }
  }
  useEffect(() => { mounted.current = true; void discover(); return () => { mounted.current = false; discovery.current++; }; }, []);
  async function loadVaults() {
    setVaultLoading(true); setVaultError(false);
    const snapshot = authSessionSnapshot();
    try { const names = await listIssuanceVaults(snapshot); if (mounted.current && isCurrentAuthSession(snapshot)) setVaultNames(names); }
    catch { if (mounted.current) setVaultError(true); }
    finally { if (mounted.current) setVaultLoading(false); }
  }
  useEffect(() => { if (draft.restricted && vaultNames === null && !vaultError && !vaultLoading) void loadVaults(); }, [draft.restricted, vaultNames, vaultError, vaultLoading]);
  useEffect(() => { if (error || Object.keys(fields).length) errorSummary.current?.focus(); }, [error, fields]);
  function update(patch: Partial<PatDraft>) { setDraft(current => ({ ...current, ...patch })); setFields({}); }
  function add(kind: "vaults" | "prefixes", value: string) {
    const clean = value.trim();
    if (!/^[a-z0-9][a-z0-9-]*$/.test(clean)) { setFields({ vault_scope: "Use lowercase letters, digits and hyphens. Prefixes are literal; wildcards and slashes are not allowed." }); return; }
    update({ [kind]: [...new Set([...draft[kind], clean])] });
    if (kind === "vaults") setExact(""); else setPrefix("");
  }
  const basic = draft.permissions === "read-write" && draft.expiration === "none" && !draft.restricted;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (pending.current || !capability || uncertain || (capability.data === "legacy" && (!basic || replacement))) return;
    const validation = validatePatDraft(draft, capability.data === "legacy" ? 255 : capability.data.name_max_length);
    if (draft.restricted && (exact.trim() || prefix.trim())) validation.errors.vault_scope = "Add the typed Vault name or prefix to the selection before creating, or clear it.";
    if (!validation.intent || Object.keys(validation.errors).length) { setFields(validation.errors); setAdvanced(true); return; }
    pending.current = true; setBusy(true); onBusyChange?.(true); setError(""); setFields({});
    const snapshot = capability.snapshot;
    try {
      const receipt = capability.data === "legacy" ? await issueLegacyPat(snapshot, validation.intent.name) : await issuePat(snapshot, capability.data, validation.intent);
      if (mounted.current && isCurrentAuthSession(snapshot)) { onCreated(receipt, snapshot); onMetadataChanged?.(); }
    } catch (e) {
      if (!mounted.current) return;
      if (!isCurrentAuthSession(snapshot)) { setCapability(null); setError("Your sign-in session changed. Review token creation again."); return; }
      if (e instanceof ApiError && e.status === 422) {
        const detail = e.detail as { details?: { fields?: { field?: unknown; message?: unknown }[] } } | null;
        const next: PatFieldErrors = {};
        for (const field of detail?.details?.fields ?? []) {
          if (typeof field.field !== "string" || typeof field.message !== "string") continue;
          const key = field.field.startsWith("vault_scope") ? "vault_scope" : field.field.startsWith("expires_") ? "expires_at" : field.field;
          if (["name", "scopes", "vault_scope", "expires_at"].includes(key)) next[key] = field.message;
        }
        setFields(next); setAdvanced(true); setError(e.message);
      } else if (e instanceof ApiError && e.status < 500) setError(e.message);
      else {
        setUncertain(true); setError("Creation result needs checking. Review your tokens before trying again; a token may have been created.");
        if (e instanceof UnverifiedPatReceipt && e.tokenId) setCreatedCandidate({ id: e.tokenId, snapshot });
        onMetadataChanged?.();
      }
    } finally { pending.current = false; if (mounted.current) { setBusy(false); onBusyChange?.(false); } }
  }
  const errorId = (field: string) => fields[field] ? `${id}-${field}-error` : undefined;
  const fieldError = (field: string) => fields[field] && <p id={errorId(field)} className="text-xs text-destructive">{fields[field]}</p>;
  const matches = vaultNames?.filter(name => draft.vaults.includes(name) || draft.prefixes.some(p => name.startsWith(p))) ?? [];
  return <form onSubmit={submit} className="min-w-0 space-y-3" aria-busy={busy || loading}>
    {(error || Object.keys(fields).length > 0) && <div ref={errorSummary} tabIndex={-1} className="rounded-[var(--radius-sm)] focus-visible:ring-2 focus-visible:ring-ring"><Alert variant="destructive"><p>{error || "Correct the highlighted fields."}</p><ul>{Object.entries(fields).map(([field, message]) => <li key={field}><a href={`#${id}-${field}`} className="underline">{message}</a></li>)}</ul></Alert></div>}
    {loading && <p role="status">Checking token issuance support…</p>}
    {!loading && !capability && <Button type="button" variant="outline" onClick={() => void discover()}>Retry token support</Button>}
    <div className="space-y-1.5"><Label htmlFor={`${id}-name`}>Token name</Label><Input id={`${id}-name`} value={draft.name} onChange={e => update({ name: e.target.value })} placeholder="For example, work laptop" disabled={busy} aria-invalid={!!fields.name} aria-describedby={errorId("name")} />{fieldError("name")}</div>
    <Button type="button" variant="ghost" aria-expanded={advanced} aria-controls={`${id}-advanced`} onClick={() => setAdvanced(!advanced)}>Advanced options</Button>
    <div id={`${id}-advanced`} hidden={!advanced} className="space-y-4">
      <div className="space-y-1.5"><Label htmlFor={`${id}-expires_at`}>Expiration</Label><SelectMenu id={`${id}-expires_at`} value={draft.expiration} disabled={busy} onValueChange={value => update({ expiration: value })} options={[{ value: "none", label: "No expiration" }, ...[7, 30, 90, 365].map(days => ({ value: String(days), label: `${days} days` })), { value: "custom", label: "Custom date and time" }]} aria-invalid={!!fields.expires_at} aria-describedby={errorId("expires_at")} />
        {draft.expiration === "custom" && <><Label htmlFor={`${id}-custom-time`}>Expiration date and time ({zone})</Label><Input id={`${id}-custom-time`} type="datetime-local" step="0.001" value={draft.customTime} disabled={busy} onChange={e => update({ customTime: e.target.value })} aria-describedby={errorId("expires_at")} /><p className="text-xs text-foreground-muted">UTC: {customExpiration(draft) ?? "Choose a valid, unambiguous time"}</p></>}{fieldError("expires_at")}</div>
      <div className="space-y-1.5"><Label htmlFor={`${id}-scopes`}>Permissions</Label><SelectMenu id={`${id}-scopes`} value={draft.permissions} disabled={busy} onValueChange={value => update({ permissions: value as PatDraft["permissions"] })} options={[...(draft.permissions === "unsupported" ? [{ value: "unsupported", label: `Unsupported: ${draft.originalScopes?.join(", ")}`, disabled: true }] : []), { value: "read", label: "Read only" }, { value: "read-write", label: "Read and write" }]} aria-invalid={!!fields.scopes} aria-describedby={errorId("scopes")} />{fieldError("scopes")}</div>
      <div className="space-y-2"><Label htmlFor={`${id}-vault_scope`}>Vault write restriction</Label><SelectMenu id={`${id}-vault_scope`} value={draft.restricted ? "selected" : "none"} disabled={busy} onValueChange={value => update({ restricted: value === "selected" })} options={[{ value: "none", label: "No additional restriction" }, { value: "selected", label: "Selected Vaults or name prefixes" }]} aria-invalid={!!fields.vault_scope} aria-describedby={errorId("vault_scope")} />{fieldError("vault_scope")}
        {draft.restricted && <div className="min-w-0 space-y-3 rounded-[var(--radius-sm)] border border-border p-3">
          <Label htmlFor={`${id}-vault-search`}>Search accessible Vaults</Label><Input id={`${id}-vault-search`} value={search} onChange={e => setSearch(e.target.value)} disabled={busy} />
          {vaultLoading && <p role="status">Loading accessible Vaults…</p>}
          {vaultError && <Alert variant="warning">Could not load Vaults. Keep your selection or enter exact names and prefixes manually. <Button type="button" variant="outline" onClick={() => void loadVaults()}>Retry Vault list</Button></Alert>}
          {vaultNames && <div className="max-h-40 space-y-1 overflow-y-auto">{vaultNames.filter(name => name.includes(search)).map(name => <label key={name} className="flex min-h-9 min-w-0 items-center gap-2 break-all"><input type="checkbox" checked={draft.vaults.includes(name)} disabled={busy} onChange={e => update({ vaults: e.target.checked ? [...draft.vaults, name] : draft.vaults.filter(v => v !== name) })} /><span>{name}</span></label>)}</div>}
          <Label htmlFor={`${id}-exact`}>Exact Vault name</Label><div className="flex min-w-0 gap-2"><Input id={`${id}-exact`} value={exact} disabled={busy} onChange={e => setExact(e.target.value)} /><Button type="button" variant="outline" disabled={busy} onClick={() => add("vaults", exact)}>Add Vault</Button></div>
          <Label htmlFor={`${id}-prefix`}>Name prefix</Label><div className="flex min-w-0 gap-2"><Input id={`${id}-prefix`} value={prefix} placeholder="team-" disabled={busy} onChange={e => setPrefix(e.target.value)} /><Button type="button" variant="outline" disabled={busy} onClick={() => add("prefixes", prefix)}>Add prefix</Button></div>
          <ul className="space-y-1">{(["vaults", "prefixes"] as const).flatMap(kind => draft[kind].map(value => <li key={`${kind}-${value}`} className="flex min-w-0 items-center gap-2"><span className="min-w-0 flex-1 break-all">{kind === "prefixes" ? "Prefix: " : "Vault: "}{value}</span><Button type="button" variant="ghost" disabled={busy} aria-label={`Remove ${kind === "prefixes" ? "prefix" : "Vault"} ${value}`} onClick={() => update({ [kind]: draft[kind].filter(v => v !== value) })}>Remove</Button></li>))}</ul>
          <p className="text-xs text-foreground-muted">Current accessible matches: {vaultNames === null ? "Unavailable" : matches.join(", ") || "None yet"}. Exact names and prefixes also cover future matching Vaults when your account has access. This preview does not grant permissions.</p>
        </div>}
      </div>
    </div>
    <div className="space-y-2 rounded-[var(--radius-sm)] bg-surface-2 p-3"><p className="font-medium">Before you create</p><p className="break-words">{draftSummary(draft)}</p><p className="text-xs text-foreground-muted">Your existing account permissions still apply. Choose an expiration and limit writes when possible.</p>{draft.restricted && <p className="text-xs text-foreground-muted">{draft.permissions === "read" ? "Writes are disabled. " : ""}Ordinary document reads are not restricted to this selection. SQL reads and writes are restricted by it.</p>}</div>
    {capability?.data === "legacy" && <Alert variant="warning">Advanced issuance is unavailable on this server. Only explicit server-default creation is available; its restrictions cannot be verified.{(!basic || replacement) && <p>{replacement ? "Create independently and revoke separately; this server cannot preserve a reviewed replacement." : "Your restricted draft is retained. Review and reset it before creating with server defaults."}</p>}{!basic && !replacement && <Button type="button" variant="outline" onClick={() => { setDraft({ ...defaultPatDraft(), name: draft.name }); setExact(""); setPrefix(""); }}>Reset to server defaults</Button>}</Alert>}
    {replacement && <Alert variant="info">Review the preserved settings and any changes. The original token remains active until you explicitly revoke it after creating the replacement.</Alert>}
    {uncertain ? <div className="space-y-2"><a className="text-link underline" href="/settings?tab=tokens">Review token list</a>{createdCandidate && <Button type="button" variant="destructive" onClick={() => setConfirmRevoke(true)}>Revoke identified new token</Button>}<Button type="button" variant="outline" onClick={() => { setUncertain(false); setCreatedCandidate(null); setError(""); void discover(); }}>I've checked — review another creation</Button></div> : <Button type="submit" variant="accent" loading={busy} disabled={loading || !capability || !draft.name.trim() || (capability.data === "legacy" && (!basic || replacement))}>{capability?.data === "legacy" ? "Create with server defaults" : replacement ? "Create replacement token" : "Create token"}</Button>}
    <ConfirmDialog open={confirmRevoke} onOpenChange={setConfirmRevoke} title="Revoke the identified new token?" description="This revokes only the new token ID returned by the failed verification. It does not revoke another token with the same name." confirmLabel="Revoke new token" variant="destructive" onConfirm={async () => { if (createdCandidate) { await revokeIssuedPat(createdCandidate.snapshot, createdCandidate.id); setCreatedCandidate(null); onMetadataChanged?.(); } }} />
  </form>;
}
