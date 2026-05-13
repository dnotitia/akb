# Frontend UX cleanup — design

**Date**: 2026-05-13
**Status**: Approved (brainstorming)
**Owner**: kwoo24
**Related**: `vault-shell.tsx`, `document.tsx`, `vault-new.tsx`, `title-bar.tsx`, `vault-members.tsx`, `access_service.py` (one backend touch)

## Problem

Five distinct UX gaps surfaced after the recent feature work landed in production. They are independent in code paths but share the same theme — small frontend rough edges that compound to make the product feel inconsistent. Bundled into one spec because each fix is small, the audit was a single review pass, and shipping them together keeps the user-visible change coherent.

1. **Vault click does not auto-expand the collection sidebar.** The sidebar's visibility is persisted to `localStorage` at one global key (`akb-explorer-visible`). A user who collapsed it in vault A finds vault B also collapsed; new-vault discoverability suffers and the sidebar's purpose ("show what's in this vault") is invisible to first-time visitors.

2. **Destructive actions appear enabled in vaults where the user thinks they have no permission.** `document.tsx` correctly gates the "Delete document" button on `vaultRole ∈ {writer, admin, owner}`. The confusion is upstream: `access_service.check_vault_access` returns the vault's `public_access` level as the user's `role` when `public_access` is `writer` or `reader` (see `access_service.py:86-88`). The user sees themselves as having writer permission even though they're not a member. From the system's point of view this is correct (public-writer grants write to everyone); from the user's point of view the affordance is unexplained.

3. **The vault-create page's Cancel and breadcrumb both route to `/`.** A user who navigated to `/vault/new` from inside `/vault/foo` and decides to cancel ends up at the home page instead of back inside `foo`. Predictable navigation requires `back` to mean "return to wherever I came from."

4. **The top-level back affordance is weak.** `title-bar.tsx` has a small accent dot + an `AKB` text link + breadcrumb segments rendered in `text-[10px] uppercase` mono. There is no dedicated back arrow. The breadcrumb dot is decorative, not interactive. Users perceive the layout as "rushed" and back-navigation as hard to find.

5. **The members page has no way to change a member's role inline.** `vault-members.tsx` renders each member's role as a static `<RoleBadge>`. Editing requires `revoke + re-invite`. The backend (`access_service.grantAccess`) already supports `ON CONFLICT DO UPDATE` so the role-change capability is available; only the UI is missing.

## Non-goals

- **Email/MFA/SSO additions.** Out of scope; tracked separately.
- **Refactoring `title-bar.tsx` beyond adding a back button.** The breadcrumb pattern stays. Visual treatment of the dot and "AKB" link is unchanged.
- **Replacing the existing transfer-ownership flow.** Transfer remains its own button on the members row; only role-change for non-owner roles becomes inline.
- **Vault-tree visibility shortcut keys / accessibility deep-dive on the sidebar toggle.** The `cmd+\` shortcut in `vault-shell.tsx:30-39` stays untouched.
- **Restoring scroll/filter state on back.** Browser handles this for most cases; we are not adding a state-preservation layer.
- **Restricting public-writer behavior at the API level.** Public-writer is a deliberate vault setting; this spec only adds clarity to the UI, not policy enforcement.
- **`role_source` propagation to MCP tools.** The new field is exposed via the REST `getVaultInfo` only; MCP responses keep their current shape.

## Issue 1 — Per-vault sidebar visibility with auto-expand on first visit

### Architecture

```
vault-shell.tsx
  ├── reads:  localStorage[`akb-explorer-visible:${vaultName}`] ?? "1"   (default open)
  ├── writes: same key on toggle
  └── transition: 200ms ease-out (transform/opacity only)

prefers-reduced-motion → transition: none (immediate snap)
```

Storage key becomes vault-scoped (`akb-explorer-visible:engineering`, `akb-explorer-visible:gnu`, etc.). Missing key = default `"1"` (open). The existing global key (`akb-explorer-visible`) is migrated once on read — if the new vault-scoped key is missing **and** the global key exists, copy the global value to the new key. The global key is then orphaned (left in place — no cleanup, it's cheap).

### UX rules applied

- §9 `state-preservation` — per-vault key preserves intent within a vault.
- §7 `state-transition`, §7 `duration-timing` — 200ms ease-out slide, transform-only (no width animation), so no layout thrashing.
- §1 `reduced-motion` — `@media (prefers-reduced-motion: reduce)` disables the animation.

### Component changes

`frontend/src/components/vault-shell.tsx`:

- Replace constant `STORAGE_KEY` with a function `storageKey(name) => \`akb-explorer-visible:${name}\``.
- Initial `useState` reads `localStorage[storageKey(name)] ?? globalLegacyValue ?? "1"`.
- Migration: if the vault-scoped key is absent and the legacy global key has a value, write the legacy value into the vault-scoped key on first mount.
- Toggle writes to the vault-scoped key.
- The tree column's `<div>` gets `transition-transform duration-200 ease-out motion-reduce:transition-none` and slides via `transform: translateX(0)` ↔ `translateX(-100%)` rather than CSS grid recompute.

The `cmd+\` keyboard toggle and the show/hide button placement do not change.

### Risk

Animating `translateX` on a flex/grid child can clip neighbouring content if the parent doesn't have `overflow: hidden`. The current shell uses a CSS grid with fixed column widths; the simplest safe option is to keep the grid-column-width approach **and** add `transition: width 200ms` on the column — `transition: width` is on the no-go list per §7 `transform-performance` because it triggers layout. So we use the standard mobile-app sidebar pattern instead: keep the grid column **always** sized at 260px, but translate the tree's internal wrapper `translateX(-100%)` when hidden, with the grid column then `display: none` after the animation. Two-state CSS, no width animation.

Alternative if the above complicates the existing layout: skip the animation entirely. The user-visible win is "sidebar opens on vault click," not "slides in smoothly." YAGNI says: ship the per-vault key first; add transition in a follow-up if it doesn't feel right.

**Decision**: ship without slide animation in v1. The per-vault default-open behavior is the main fix. Transition is deferred — note in the plan.

## Issue 2 — Role-source clarity for public-access vaults

### Architecture

```
backend  access_service.check_vault_access
   │
   └─► returns {vault_id, role, status, role_source}
                                   ▲
                                   │ NEW: "member" | "public"
                                   │
       (server already knows which branch fired; just surface it)

frontend  getVaultInfo → role_source available to UI
            ▼
   vault-overview / vault-settings headers display badge:
     · role_source === "member" → existing role pill
     · role_source === "public" → "PUBLIC · <role>" pill (warning bg)
            ▼
   destructive actions ARE NOT hidden; their contract is honoured.
   Clarity is delivered via the badge + a popover explainer.
```

### Why not hide destructive actions

Public-writer **is** a real write grant. Hiding delete would silently break the vault owner's intent. The complaint is "I don't realize I have write permission" — that's solved by surfacing the source, not by gating the action.

### Backend change

`backend/app/services/access_service.py`:

The existing `check_vault_access` (line 44) and `list_accessible_vaults` (line ~240) both compute the role via two branches: membership row in `vault_access`, or fallback to `vaults.public_access`. Add a sibling field `role_source: Literal["member", "public"]` to whichever response model `getVaultInfo` returns.

`getVaultInfo` corresponds to `GET /api/v1/vaults/{vault}/info` (already exists at `access.py:56`). The handler returns a dict; we add `"role_source"` to it.

For owners, `role_source = "member"` (owner is a special member, not derived from public_access).

For non-member readers of a `public_access = "reader"` vault, `role_source = "public"`.

For non-member writers of a `public_access = "writer"` vault, `role_source = "public"`.

For admin/owner members, `role_source = "member"`.

The MCP path (`akb_whoami`, `akb_browse`, etc.) does not surface this field — only the REST `getVaultInfo` does. MCP clients are agents and don't need the UI badge.

**`list_accessible_vaults` is NOT modified.** It currently returns role via a SQL COALESCE that derives from owner / public_access / membership; frontend does not consume `role_source` from the list endpoint. Leave the query and response shape untouched. The single new field lives only on `getVaultInfo`'s response.

### Frontend change

`frontend/src/lib/api.ts` — extend the `getVaultInfo` return type with `role_source?: "member" | "public"` (optional for backwards compat; an old backend response without the field is treated as `"member"`).

**Badge location — decided**: render in the **vault-members page header** (`vault-members.tsx:93` already renders the viewer's role pill via `{info?.role && <RoleBadge role={info.role} />}`). Replace that pill with one that switches treatment based on `info?.role_source`. Same pattern can apply to the vault-overview page header if it has its own role pill — verify and mirror; otherwise leave alone. **Do not** put the badge in `title-bar.tsx` — TitleBar stays role-agnostic.

Pill design:

```tsx
{role_source === "public" && (
  <span
    className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-mono
               uppercase tracking-wider rounded-sm
               border border-warning/40 bg-warning/10 text-warning"
    title="This role is granted by the vault's public_access setting,
not by direct membership. Contact the owner if this was unintended."
  >
    PUBLIC · {role}
  </span>
)}
```

Uses `text-warning` semantic token (define if absent; reuse existing destructive/warning palette). Tooltip explains the source.

### UX rules applied

- §9 `empty-nav-state` — don't silently hide; explain.
- §1 `color-not-only` — "PUBLIC" text accompanies the color difference.
- §1 `aria-labels` — the `title` provides the explanation for screen readers via fallback or, better, an associated `aria-describedby`.

### Risk

If destructive action handlers ever silently rely on `vaultRole`, the badge alone won't change behaviour. That's intentional — the contract says public-writer can delete. If a future product decision flips this (public-writer cannot delete others' docs), it becomes a backend authorization change, not a UI gate.

## Issue 3 — Vault-create Cancel returns to previous page

### Architecture

```
vault-new.tsx:
  Cancel button onClick:
    if window.history.length > 1 → navigate(-1)
    else                        → navigate("/")

  ESC key (Document-level listener while on /vault/new):
    same as Cancel.

  Breadcrumb "HOME":
    keep as `to="/"` (semantic: literal home link, not a back).

  Form submit (success):
    navigate(`/vault/${name}`) — unchanged.
```

### Why history-back is safe here

The form is a fresh-create with no draft persistence. Going back loses the user's input — but that's the existing Cancel semantics anyway. If they truly typed in a long description and want to keep it, they can refrain from clicking Cancel.

`window.history.length > 1` is a defensive guard — direct-loaded `/vault/new` (e.g., from a bookmark) has `length === 1` and the fallback `/` is the only sensible target.

### Component changes

`frontend/src/pages/vault-new.tsx`:

```tsx
import { useNavigate } from "react-router-dom";
// already imported

const navigate = useNavigate();

function handleCancel() {
  if (window.history.length > 1) navigate(-1);
  else navigate("/");
}

// Replace existing <Link to="/" ...>Cancel</Link> with:
<Button asChild variant="outline" onClick={handleCancel}>
  <span><ArrowLeft className="h-4 w-4" aria-hidden /> Cancel</span>
</Button>
```

(Verify the existing Cancel is a `<Link>` vs `<Button asChild>`. The change is to make it a regular `<button>` triggering navigate(-1), with the back-arrow icon.)

ESC listener:

```tsx
useEffect(() => {
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape" && !creating) handleCancel();
  };
  window.addEventListener("keydown", onKey);
  return () => window.removeEventListener("keydown", onKey);
}, [creating]);
```

`!creating` prevents ESC from cancelling mid-submit.

### UX rules applied

- §9 `back-behavior` — predictable: Cancel returns to where you came from.
- §9 `escape-routes` — ESC key also exits.
- §1 `escape-routes` (keyboard access) — same.

## Issue 4 — Top-level back arrow in TitleBar

### Architecture

```
TitleBar layout (new):

  [← back arrow]  [• dot]  [AKB]  ›  [crumb 1]  ›  [crumb N]   …right slot
       ▲
       │
   new dedicated back button, 36×36 px tap target,
   ArrowLeft icon, disabled when history.length === 1.
```

The breadcrumb keeps its existing visual treatment. The back arrow sits to the left of the dot.

### Component changes

`frontend/src/components/title-bar.tsx`:

Add imports and the back button before the dot:

```tsx
import { useNavigate, useLocation } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

export function TitleBar({ crumbs, right, className }: { ... }) {
  const navigate = useNavigate();
  const location = useLocation();

  const canBack = window.history.length > 1 && location.pathname !== "/";

  function handleBack() {
    if (canBack) navigate(-1);
  }

  // Document-level ESC handler is NOT in TitleBar — too many false fires.
  // Back arrow is click-only.

  return (
    <div className={cn(...)}>
      <button
        type="button"
        onClick={handleBack}
        disabled={!canBack}
        aria-label="Go back"
        title="Go back"
        className="inline-flex items-center justify-center h-9 w-9 -ml-2
                   text-foreground-muted hover:text-foreground hover:bg-surface-muted
                   active:scale-95
                   disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent
                   transition-colors duration-150
                   motion-reduce:transition-none motion-reduce:active:scale-100
                   focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface
                   cursor-pointer"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden />
      </button>
      <span className="inline-block h-2 w-2 rounded-full bg-accent" aria-hidden />
      <Link to="/" ...>AKB</Link>
      {/* crumbs unchanged */}
      {right && <div className="ml-auto ...">{right}</div>}
    </div>
  );
}
```

`-ml-2` pulls the button into the existing px-4 padding so the visual left-edge of the bar isn't pushed further right. h-9 matches the TitleBar height.

### UX rules applied

- §2 `touch-target-size` — 36×36 with full padding around a 16×16 icon = 36 effective. Web context, ≥36 is acceptable per existing project patterns (matches the explorer's other icon buttons). For touch-first contexts 44 would be ideal; this is a desktop-first product.
- §1 `aria-labels` — explicit "Go back".
- §1 `focus-states` — `focus-visible:ring-2` consistent with neighbouring buttons.
- §4 `state-clarity` — hover bg, active scale, disabled opacity all visually distinct.
- §7 `scale-feedback` — `active:scale-95` press feedback, respects reduced-motion.
- §9 `back-behavior` — consistent with Issue 3's `navigate(-1)` fallback.

### Risk

`window.history.length` is unreliable across SPAs that have pushed many entries. It only counts entries the browser has seen, including pre-app pages. In practice for our app, the user lands on `/auth` then navigates inside the app — `length > 1` is almost always true. The `disabled` state mainly matters at first paint of the very first route after a hard refresh.

## Issue 5 — Inline role change on members page

### Architecture

```
vault-members.tsx member row:

   [avatar] [username · display_name]
            [email]
            [role select ▾]   [since: 3 days ago]   [Transfer] [Revoke]
              ▲
              │
   - admin/owner viewing: native <select> rendered
   - owner row OR self row: select disabled (cursor: not-allowed, opacity 0.5)
   - on change: optimistic UI + toast with Undo
   - in-flight: select disabled with spinner overlay
```

### Component changes

`frontend/src/pages/vault-members.tsx`:

1. Replace the existing `<RoleBadge role={m.role} />` block with a conditional render:

```tsx
{canManage && m.role !== "owner" && m.username !== currentUser?.username ? (
  <RoleSelect
    member={m}
    onChanged={(prev, next) => showUndoToast(m, prev, next)}
  />
) : (
  <RoleBadge role={m.role} />
)}
```

**Note on identifiers**: the existing `Member` interface in `vault-members.tsx:17-23` only exposes `username`, not `user_id`. Use `username` for the self-check. `currentUser` comes from a new `getMe()` fetch on mount (Settings page already uses the same call); store in a `useState<User | null>(null)` and gate the select on `currentUser` being non-null.

2. New component `frontend/src/components/role-select.tsx`:

```tsx
import { useState } from "react";
import { Loader2 } from "lucide-react";
import { grantAccess } from "@/lib/api";

interface Props {
  member: VaultMember;
  onChanged: (prev: string, next: string) => void;
}

export function RoleSelect({ member, onChanged }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const next = e.target.value;
    const prev = member.role;
    if (next === prev) return;
    setBusy(true);
    setError(null);
    try {
      await grantAccess(member.vault, member.username, next);
      onChanged(prev, next);  // parent triggers refetch + undo toast
    } catch (err: any) {
      setError(err?.message || "Failed to change role");
      // revert select to prev via re-render (parent should not have refetched yet)
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative">
      <select
        value={member.role}
        onChange={handleChange}
        disabled={busy}
        aria-label={`Change role for ${member.username}`}
        className="appearance-none font-mono text-xs uppercase tracking-wider
                   px-2 py-1 pr-6 border border-border bg-surface text-foreground
                   hover:border-accent transition-colors duration-150
                   disabled:opacity-50 disabled:cursor-not-allowed
                   focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface
                   cursor-pointer"
      >
        <option value="reader">READER</option>
        <option value="writer">WRITER</option>
        <option value="admin">ADMIN</option>
      </select>
      {busy && (
        <Loader2 className="absolute right-1 top-1/2 -translate-y-1/2 h-3 w-3 animate-spin text-foreground-muted" aria-hidden />
      )}
      {error && (
        <p role="alert" className="text-[10px] text-destructive mt-1">{error}</p>
      )}
    </div>
  );
}
```

3. Undo toast (parent in `vault-members.tsx`):

```tsx
async function showUndoToast(member: VaultMember, prev: string, next: string) {
  // optimistic UI: members list already reflects the change via refetch or local update
  await refetch();  // pulls fresh list
  toast({
    title: `Changed ${member.username} from ${prev} to ${next}.`,
    action: {
      label: "Undo",
      onClick: async () => {
        try {
          await grantAccess(vault, member.username, prev);
          await refetch();
          toast({ title: "Reverted." });
        } catch (e: any) {
          toast({ title: "Undo failed", description: e?.message, variant: "destructive" });
        }
      },
    },
    duration: 5000,
  });
}
```

Reuse the project's existing toast primitive (verify location during implementation; if no shared toast exists, fall back to an inline banner above the members list with a "Undo" button + a `setTimeout` to auto-clear).

### Why no confirm dialog

For admin workflows, confirming every role change is friction. Modern Gmail-style **Undo toast** is the standard for reversible-within-seconds actions. Owner-downgrade is impossible by design (transfer is its own flow). Self-downgrade is blocked at the UI level (`m.user_id !== currentUser.user_id`).

### UX rules applied

- §8 `undo-support` — 5s Undo toast for every change.
- §8 `disabled-states` — owner row and self row use the read-only `RoleBadge`; in-flight selects have opacity-50 + cursor-not-allowed + spinner overlay.
- §8 `error-recovery` — failed change shows inline error with the existing message; "Undo failed" surfaces in its own toast.
- §1 `aria-labels` — `aria-label="Change role for {username}"`.
- §2 `loading-buttons` — disabled select + spinner during the API call.

### Backend

No change. `grantAccess(vault, user, role)` is already idempotent via `ON CONFLICT DO UPDATE SET role = $4` (see `access_service.py:147`). The existing route accepts admin+owner callers; that's our `canManage` predicate.

## Error handling

| Scenario | Behavior |
|---|---|
| Issue 1: missing vault-scoped key on first visit | Default to open. Migrate from legacy global key once. |
| Issue 1: localStorage write fails (quota / disabled) | Silently swallow; state lives in-memory for the session. |
| Issue 2: `getVaultInfo` returns response without `role_source` (old backend) | Treat as `"member"` — current behaviour, no badge. |
| Issue 3: `navigate(-1)` lands on an invalid page (rare) | React Router handles routing; user sees the destination. If `history.length === 1`, fallback to `/`. |
| Issue 4: same as Issue 3. |
| Issue 5: `grantAccess` 403 (caller lost admin role mid-session) | Select reverts to previous value, inline error "Permission denied". |
| Issue 5: Undo grantAccess fails | Toast "Undo failed — try again" with the underlying error. |
| Issue 5: network timeout | Standard `api()` wrapper throws; `RoleSelect` catches and surfaces inline. |

## Testing

### Backend

`backend/tests/test_access_service.py` (extend or create) — unit test that `check_vault_access` returns `role_source: "public"` for non-members accessing public-reader/writer vaults and `role_source: "member"` for actual members and owners. Live test via `backend/tests/test_security_edge_e2e.sh` extension: bootstrap a public-writer vault, register a second user who is NOT a member, call `GET /api/v1/vaults/{vault}/info` as that user, assert response has `role: "writer"` AND `role_source: "public"`.

**Route-level smoke** (small but worth adding) — same e2e file, one extra block:
```bash
# Member of a private vault → role_source must be "member"
JWT_OWNER=...
R=$(curl -sk "$BASE_URL/api/v1/vaults/$VAULT/info" -H "Authorization: Bearer $JWT_OWNER")
echo "$R" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["role_source"]=="member",d'
```
This catches serializer regressions where the field is computed correctly in the service but dropped before it reaches the JSON response.

### Frontend

Vitest:
- `vault-shell.test.tsx` — per-vault localStorage key reading/writing; legacy-key migration; default-open behavior for a fresh vault.
- `title-bar.test.tsx` — back button renders disabled when `history.length === 1`, calls `navigate(-1)` on click, has `aria-label`, has `focus-visible` classes.
- `vault-new.test.tsx` — Cancel triggers `navigate(-1)` when history exists, `navigate('/')` when not. ESC key closes.
- `role-select.test.tsx` (new) — renders three options, calls `grantAccess` on change, shows spinner during the call, surfaces error on rejection, reverts on error.
- `vault-members.test.tsx` (extend) — role select visible for non-owner non-self when admin views; read-only badge for owner row and self row.

No new E2E shell suites — these are pure UI behaviours best covered by Vitest. Manual verification on the production URL is the gating check.

## Files touched

**Backend — modified**:
- `backend/app/services/access_service.py` — `role_source` field on `check_vault_access` return (and probably the route handler that surfaces it via `GET /api/v1/vaults/{vault}/info`).
- `backend/tests/test_security_edge_e2e.sh` (extend) — `role_source` assertion for public vault.

**Frontend — modified**:
- `frontend/src/components/vault-shell.tsx` — per-vault storage key + legacy-key migration.
- `frontend/src/lib/api.ts` — `VaultInfo` type extended with optional `role_source`.
- `frontend/src/components/title-bar.tsx` — back-arrow button.
- `frontend/src/pages/vault-new.tsx` — Cancel → `navigate(-1)`, ESC handler, ArrowLeft icon.
- `frontend/src/pages/vault-members.tsx` — `RoleSelect` integration, owner/self lockout, undo toast.
- (Wherever the vault page header lives — possibly `frontend/src/pages/vault.tsx` or extended `title-bar.tsx`) — `PUBLIC · {role}` badge rendering.

**Frontend — new**:
- `frontend/src/components/role-select.tsx`
- Vitest companions for vault-shell, title-bar, vault-new, role-select, vault-members updates.
