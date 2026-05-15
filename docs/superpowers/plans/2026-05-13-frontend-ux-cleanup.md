# Frontend UX cleanup Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship five small UX fixes bundled into one feature pass — per-vault sidebar visibility, `role_source` clarity badge for public vaults, vault-new history-back Cancel, dedicated back arrow in TitleBar, and inline role change with Undo toast on the members page.

**Architecture:** All but one change are frontend-only. One backend touch adds a `role_source: "member" | "public"` field to `GET /api/v1/vaults/{vault}/info` so the frontend can distinguish role-via-membership from role-via-public-access. Existing routes and types are left as-is otherwise.

**Tech Stack:** Python 3.11 / FastAPI / asyncpg; React 19 + TypeScript + Vite + Vitest + Tailwind v4.

**Spec:** `docs/superpowers/specs/2026-05-13-frontend-ux-cleanup-design.md`

---

## File Map

**Backend — modified**:
- `backend/app/services/access_service.py` — `check_vault_access` returns `role_source`; `get_vault_info` passes it through.
- `backend/tests/test_security_edge_e2e.sh` — add 2 assertions (member vs public).

**Frontend — modified**:
- `frontend/src/components/vault-shell.tsx` — per-vault `localStorage` key + one-time legacy migration.
- `frontend/src/components/title-bar.tsx` — dedicated back-arrow button.
- `frontend/src/pages/vault-new.tsx` — `navigate(-1)` Cancel + ESC key.
- `frontend/src/lib/api.ts` — extend `VaultInfo` type with `role_source`.
- `frontend/src/pages/vault-members.tsx` — wire `currentUser`, swap static `RoleBadge` for `RoleSelect`, add Undo toast, render `role_source` badge in the header.

**Frontend — new**:
- `frontend/src/components/role-select.tsx`
- Vitest companions:
  - `frontend/src/components/__tests__/role-select.test.tsx`
  - `frontend/src/components/__tests__/vault-shell-visibility.test.tsx` (or extension of existing)
  - `frontend/src/components/__tests__/title-bar-back.test.tsx`
  - `frontend/src/pages/__tests__/vault-new-cancel.test.tsx`

---

## Task 1 — Backend `role_source` field

**Files:**
- Modify: `backend/app/services/access_service.py` (`check_vault_access` lines 44-100, `get_vault_info` returns)
- Modify: `backend/tests/test_security_edge_e2e.sh` — extend with `role_source` assertions

**Context:** `check_vault_access` already branches on owner / public_access / vault_access membership. Each branch returns a dict; we add `role_source` to that dict. `get_vault_info` reads `access["role"]` and composes the response — we add `role_source` next to it. Frontend reads `info.role_source`.

`list_accessible_vaults` is **NOT** modified — frontend doesn't consume `role_source` from the list endpoint.

- [ ] **Step 1: Extend the e2e suite first (failing assertion)**

Append at the bottom of `backend/tests/test_security_edge_e2e.sh`:

```bash
# role_source: member vs public
echo ""
echo "▸ role_source field on /vaults/{vault}/info"

# Owner (member) of their own vault
INFO=$(curl -sk "$BASE_URL/api/v1/vaults/$VAULT/info" -H "Authorization: Bearer $JWT_OWNER")
RS=$(echo "$INFO" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("role_source","MISSING"))')
[ "$RS" = "member" ] && pass "owner sees role_source=member" \
  || fail "role_source owner" "got $RS"

# Set vault to public-writer
curl -sk -X PATCH "$BASE_URL/api/v1/vaults/$VAULT" \
  -H "Authorization: Bearer $JWT_OWNER" \
  -H 'Content-Type: application/json' \
  -d '{"public_access":"writer"}' >/dev/null

# Bootstrap a non-member
USER2="role-src-other-$(date +%s)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER2\",\"email\":\"$USER2@t.dev\",\"password\":\"orig-12345\"}" >/dev/null
JWT2=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER2\",\"password\":\"orig-12345\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# Non-member of a public-writer vault → role_source=public
INFO=$(curl -sk "$BASE_URL/api/v1/vaults/$VAULT/info" -H "Authorization: Bearer $JWT2")
RS=$(echo "$INFO" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("role_source","MISSING"))')
[ "$RS" = "public" ] && pass "non-member sees role_source=public" \
  || fail "role_source public" "got $RS"
```

(Adjust to whatever the existing `JWT_OWNER` / `VAULT` variables are in `test_security_edge_e2e.sh` — read 20 lines around the existing assertions to match.)

- [ ] **Step 2: Run, see fail**

```bash
AKB_URL=http://localhost:8000 bash backend/tests/test_security_edge_e2e.sh 2>&1 | tail -15
```

Expected: the two new assertions fail (`got MISSING`). The rest of the suite still passes.

- [ ] **Step 3: Modify `check_vault_access`**

In `backend/app/services/access_service.py`, four return paths each add `"role_source": ...`. **Read lines 76-100 before editing** — the membership branch is NOT an `if access:` block but the final `return` after a `ForbiddenError` gate. The actual code shape is:

```python
# 1. System admin bypass (around line 76-78):
is_admin = await conn.fetchval("SELECT is_admin FROM users WHERE id = $1", uid)
if is_admin:
    return {"vault_id": vault["id"], "role": "owner", "status": vault["status"], "role_source": "member"}

# 2. Owner branch (around line 81-82):
if vault["owner_id"] == uid:
    return {"vault_id": vault["id"], "role": "owner", "status": vault["status"], "role_source": "member"}

# 3. Public-access branch (around line 86-88):
public_access = vault.get("public_access", "none")
if public_access != "none" and _role_level(required_role) <= _role_level(public_access):
    return {"vault_id": vault["id"], "role": public_access, "status": vault["status"], "role_source": "public"}

# 4. Final membership return (line 100) — this is the membership-success path.
#    DO NOT wrap in `if access:` — leave the existing `user_role = access["role"] if access else None`
#    and ForbiddenError gate intact; just modify the final `return` statement:
return {"vault_id": vault["id"], "role": user_role, "status": vault["status"], "role_source": "member"}
```

Every existing dict return gets `role_source`; nothing else changes. The membership path's `user_role` came from a real `vault_access` row, so it's `"member"`.

- [ ] **Step 4: Modify `get_vault_info`**

In the same file, find `get_vault_info` (line ~273). After `access = await check_vault_access(...)` it has `caller_role = access["role"]`. Add:

```python
caller_role = access["role"]
role_source = access["role_source"]
```

Then in the final return dict (search for the response composition; it's after the parallel `_q` / `_r` calls), add `"role_source": role_source` next to `"role": caller_role`.

- [ ] **Step 5: Run, see pass**

Need the backend container restarted to pick up changes:

```bash
cd /Users/kwoo2/Desktop/storage/akb && docker compose up -d --build backend
until curl -sf http://localhost:8000/livez >/dev/null 2>&1; do sleep 2; done
AKB_URL=http://localhost:8000 bash backend/tests/test_security_edge_e2e.sh 2>&1 | tail -10
```

Expected: all assertions pass including the two new ones.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/access_service.py backend/tests/test_security_edge_e2e.sh
git commit -m "feat(access): role_source field on vault info (member vs public)"
```

---

## Task 2 — Per-vault sidebar visibility

**Files:**
- Modify: `frontend/src/components/vault-shell.tsx`
- Create: `frontend/src/components/__tests__/vault-shell-visibility.test.tsx`

**Context:** `vault-shell.tsx:9-27` defines `STORAGE_KEY = "akb-explorer-visible"` and reads it once on mount. We switch to `storageKey(vault) → akb-explorer-visible:${vault}` and migrate from the legacy global key on first read per vault.

- [ ] **Step 1: Inspect current shape**

```bash
sed -n '1,45p' frontend/src/components/vault-shell.tsx
```

Confirm the `STORAGE_KEY` constant + the `useState(() => ...)` initializer + the `useEffect(() => localStorage.setItem(STORAGE_KEY, ...))`.

- [ ] **Step 2: Write the failing test**

Create `frontend/src/components/__tests__/vault-shell-visibility.test.tsx`:

```typescript
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { storageKey, readInitialVisible, migrateLegacyKey } from "../vault-shell";
// NOTE: these helpers must be exported from vault-shell.tsx (see Step 3).
// If unable to export them cleanly without breaking the component, inline
// the same logic and adapt the test imports accordingly.

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe("vault-shell visibility storage", () => {
  it("storageKey is per-vault", () => {
    expect(storageKey("alpha")).toBe("akb-explorer-visible:alpha");
    expect(storageKey("beta")).not.toBe(storageKey("alpha"));
  });

  it("defaults to true (open) for a fresh vault with no key", () => {
    expect(readInitialVisible("fresh-vault")).toBe(true);
  });

  it("reads vault-scoped value when present", () => {
    localStorage.setItem("akb-explorer-visible:alpha", "0");
    expect(readInitialVisible("alpha")).toBe(false);
  });

  it("migrates legacy global key when vault-scoped is absent", () => {
    localStorage.setItem("akb-explorer-visible", "0");  // legacy
    migrateLegacyKey("alpha");
    expect(localStorage.getItem("akb-explorer-visible:alpha")).toBe("0");
    expect(readInitialVisible("alpha")).toBe(false);
  });

  it("does NOT overwrite an existing vault-scoped key with legacy value", () => {
    localStorage.setItem("akb-explorer-visible", "0");  // legacy
    localStorage.setItem("akb-explorer-visible:alpha", "1");  // user's choice
    migrateLegacyKey("alpha");
    expect(localStorage.getItem("akb-explorer-visible:alpha")).toBe("1");
  });
});
```

- [ ] **Step 3: Run, see fail**

```bash
cd /Users/kwoo2/Desktop/storage/akb/frontend && pnpm vitest run components/__tests__/vault-shell-visibility
```

Expected: import errors (`storageKey` / `readInitialVisible` / `migrateLegacyKey` not exported).

- [ ] **Step 4: Refactor `vault-shell.tsx`**

Replace lines 9-27 (the `STORAGE_KEY` constant + the inline `useState` initializer + the `useEffect` that persists):

```typescript
const LEGACY_STORAGE_KEY = "akb-explorer-visible";

export function storageKey(vault: string): string {
  return `${LEGACY_STORAGE_KEY}:${vault}`;
}

export function readInitialVisible(vault: string): boolean {
  try {
    const v = localStorage.getItem(storageKey(vault));
    if (v !== null) return v !== "0";
    const legacy = localStorage.getItem(LEGACY_STORAGE_KEY);
    if (legacy !== null) return legacy !== "0";
    return true;  // default open
  } catch {
    return true;
  }
}

export function migrateLegacyKey(vault: string): void {
  try {
    if (localStorage.getItem(storageKey(vault)) !== null) return;
    const legacy = localStorage.getItem(LEGACY_STORAGE_KEY);
    if (legacy !== null) {
      localStorage.setItem(storageKey(vault), legacy);
    }
  } catch {}
}
```

In `VaultShell` (component body):

```typescript
const { name } = useParams<{ name: string }>();
// ...

useEffect(() => {
  if (name) migrateLegacyKey(name);
}, [name]);

const [visible, setVisible] = useState<boolean>(() =>
  name ? readInitialVisible(name) : true,
);

useEffect(() => {
  if (!name) return;
  try {
    localStorage.setItem(storageKey(name), visible ? "1" : "0");
  } catch {}
}, [name, visible]);

useEffect(() => {
  // Re-read on vault change (vault A → vault B)
  if (name) setVisible(readInitialVisible(name));
}, [name]);
```

(Two `useEffect` on `name` looks redundant — collapse if your judgment says so. The split here is for readability: one migrates, one re-reads. Combine into a single effect if cleaner.)

- [ ] **Step 5: Run, see pass**

```bash
cd frontend && pnpm vitest run components/__tests__/vault-shell-visibility
```

Expected: 5 passed.

- [ ] **Step 6: Run full suite + tsc**

```bash
cd frontend && pnpm vitest run && pnpm tsc --noEmit
```

Expected: all green, no regressions.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/vault-shell.tsx \
        frontend/src/components/__tests__/vault-shell-visibility.test.tsx
git commit -m "feat(ui): per-vault sidebar visibility with legacy-key migration"
```

---

## Task 3 — Vault-new Cancel uses history back

**Files:**
- Modify: `frontend/src/pages/vault-new.tsx`
- Create: `frontend/src/pages/__tests__/vault-new-cancel.test.tsx`

**Context:** Current Cancel button is a `<Link to="/">`. We replace with a `<Button>` that calls `navigate(-1)` with a safe fallback. Add an ESC keydown handler.

- [ ] **Step 1: Inspect the current Cancel**

```bash
grep -n "Cancel\|Link.*to=\"/\"" frontend/src/pages/vault-new.tsx | head -10
```

Locate the Cancel link (likely near the submit button at the bottom of the form).

- [ ] **Step 2: Write the failing test**

Create `frontend/src/pages/__tests__/vault-new-cancel.test.tsx`:

```typescript
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import VaultNewPage from "../vault-new";

vi.mock("@/lib/api", () => ({
  createVault: vi.fn(),
  listVaultTemplates: vi.fn().mockResolvedValue([]),
}));

afterEach(cleanup);

function renderAtWithHistory(entries: string[]) {
  return render(
    <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
      <Routes>
        <Route path="/" element={<div data-testid="home" />} />
        <Route path="/vault/:name" element={<div data-testid="vault-page" />} />
        <Route path="/vault/new" element={<VaultNewPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("VaultNewPage Cancel + ESC", () => {
  it("Cancel goes back when history has prior entry", () => {
    renderAtWithHistory(["/vault/foo", "/vault/new"]);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.getByTestId("vault-page")).toBeInTheDocument();
  });

  it("Cancel falls back to / when no prior history", () => {
    renderAtWithHistory(["/vault/new"]);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.getByTestId("home")).toBeInTheDocument();
  });

  it("ESC key triggers the same cancel", () => {
    renderAtWithHistory(["/vault/foo", "/vault/new"]);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByTestId("vault-page")).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run, see fail**

```bash
cd frontend && pnpm vitest run pages/__tests__/vault-new-cancel
```

Expected: failures — the Cancel button is currently a Link that pushes to `/` so the "go back" test fails; the ESC test fails because no handler exists.

**MemoryRouter note**: `window.history.length` is 0 inside JSDOM's MemoryRouter, so the implementation must use `navigate(-1)` and let React Router handle the no-prior-entry case (it stays put), then **also** detect that and fall back to `/`. React Router 6's `useNavigate()` returns a function; calling `navigate(-1)` on the first entry is a no-op. The test setup uses `initialIndex` to provide a prior entry; for the "no prior" case we rely on the implementation calling `navigate("/")` when `history.length <= 1`.

If the test still flakes because `window.history.length` is unreliable in JSDOM, adapt the implementation to use a small ref tracking whether navigation occurred and fall back. The simpler correct path is to always call `navigate(-1)` and let the user see the route flicker if history is missing — but the spec asks for the explicit fallback, so keep the `window.history.length` guard and accept that JSDOM may need a fixture nudge.

- [ ] **Step 4: Modify `vault-new.tsx`**

Add imports if missing:

```typescript
import { useEffect } from "react";
import { ArrowLeft } from "lucide-react";
```

Add handler in the component:

```typescript
function handleCancel() {
  if (typeof window !== "undefined" && window.history.length > 1) {
    navigate(-1);
  } else {
    navigate("/");
  }
}

useEffect(() => {
  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape" && !creating) handleCancel();
  }
  window.addEventListener("keydown", onKey);
  return () => window.removeEventListener("keydown", onKey);
}, [creating]);
```

Replace the existing Cancel `<Button asChild variant="outline"><Link to="/">Cancel</Link></Button>` (or similar; verify exact JSX) with:

```tsx
<Button type="button" variant="outline" onClick={handleCancel}>
  <ArrowLeft className="h-4 w-4" aria-hidden /> Cancel
</Button>
```

- [ ] **Step 5: Run, see pass**

```bash
cd frontend && pnpm vitest run pages/__tests__/vault-new-cancel
```

Expected: 3 passed. If the "no prior history" case fails due to JSDOM behaviour, adjust the test's `entries` array or the implementation's guard — but the spec mandates the fallback exists.

- [ ] **Step 6: Run full suite**

```bash
cd frontend && pnpm vitest run && pnpm tsc --noEmit
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/vault-new.tsx \
        frontend/src/pages/__tests__/vault-new-cancel.test.tsx
git commit -m "feat(ui): vault-new Cancel + ESC use history back"
```

---

## Task 4 — Dedicated back arrow in TitleBar

**Files:**
- Modify: `frontend/src/components/title-bar.tsx`
- Create: `frontend/src/components/__tests__/title-bar-back.test.tsx`

**Context:** TitleBar currently renders accent dot + "AKB" link + crumbs. Add a back button at the very left (before the dot), 36×36 px tap target, disabled when on home.

- [ ] **Step 1: Inspect current TitleBar**

```bash
sed -n '20,65p' frontend/src/components/title-bar.tsx
```

Confirm the JSX shape.

- [ ] **Step 2: Write the failing test**

Create `frontend/src/components/__tests__/title-bar-back.test.tsx`:

```typescript
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { TitleBar } from "../title-bar";

afterEach(cleanup);

function renderWithRoutes(currentPath: string, history: string[]) {
  return render(
    <MemoryRouter initialEntries={history} initialIndex={history.length - 1}>
      <Routes>
        <Route path="/" element={
          <TitleBar crumbs={[{ label: "ROOT" }]} />
        } />
        <Route path="/vault/:name" element={
          <TitleBar crumbs={[{ label: "vault", to: "/vault/foo" }]} />
        } />
        <Route path="*" element={
          <TitleBar crumbs={[{ label: "X" }]} />
        } />
      </Routes>
    </MemoryRouter>,
  );
}

describe("TitleBar back button", () => {
  it("renders with aria-label 'Go back'", () => {
    renderWithRoutes("/vault/foo", ["/", "/vault/foo"]);
    expect(screen.getByRole("button", { name: /go back/i })).toBeInTheDocument();
  });

  it("is disabled on the home route", () => {
    renderWithRoutes("/", ["/"]);
    expect(screen.getByRole("button", { name: /go back/i })).toBeDisabled();
  });

  it("is enabled when there is prior history and we are NOT on /", () => {
    renderWithRoutes("/vault/foo", ["/", "/vault/foo"]);
    expect(screen.getByRole("button", { name: /go back/i })).not.toBeDisabled();
  });
});
```

- [ ] **Step 3: Run, see fail**

```bash
cd frontend && pnpm vitest run components/__tests__/title-bar-back
```

Expected: `Unable to find an accessible element with the role "button" and name /go back/i`.

- [ ] **Step 4: Modify TitleBar**

In `frontend/src/components/title-bar.tsx`:

```typescript
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, Compass, GitGraph, Search as SearchIcon, Share2 } from "lucide-react";
// (existing imports plus useLocation, useNavigate, ArrowLeft)

export function TitleBar({ crumbs, right, className }: { ... }) {
  const navigate = useNavigate();
  const location = useLocation();

  const canBack =
    typeof window !== "undefined" &&
    window.history.length > 1 &&
    location.pathname !== "/";

  function handleBack() {
    if (canBack) navigate(-1);
  }

  return (
    <div className={cn(
      "flex items-center gap-2.5 h-9 px-4 border-b border-border bg-surface",
      "font-mono text-[10px] uppercase tracking-wider text-foreground-muted",
      className,
    )}>
      <button
        type="button"
        onClick={handleBack}
        disabled={!canBack}
        aria-label="Go back"
        title="Go back"
        className={cn(
          "inline-flex items-center justify-center h-9 w-9 -ml-2",
          "text-foreground-muted hover:text-foreground hover:bg-surface-muted",
          "active:scale-95",
          "disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent",
          "transition-colors duration-150",
          "motion-reduce:transition-none motion-reduce:active:scale-100",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface",
          "cursor-pointer",
        )}
      >
        <ArrowLeft className="h-4 w-4" aria-hidden />
      </button>
      <span className="inline-block h-2 w-2 rounded-full bg-accent" aria-hidden />
      {/* …rest of existing JSX unchanged… */}
    </div>
  );
}
```

`-ml-2` pulls the button into the existing px-4 padding so the back arrow visually starts at the bar's left edge.

- [ ] **Step 5: Run, see pass**

```bash
cd frontend && pnpm vitest run components/__tests__/title-bar-back
```

Expected: 3 passed. If `window.history.length` behaves oddly in JSDOM, the second test ("disabled on home") needs the entries to be exactly `["/"]` so history length is 1.

- [ ] **Step 6: Run full suite**

```bash
cd frontend && pnpm vitest run && pnpm tsc --noEmit
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/title-bar.tsx \
        frontend/src/components/__tests__/title-bar-back.test.tsx
git commit -m "feat(ui): TitleBar back arrow with disabled-on-home state"
```

---

## Task 5 — `role_source` consumed in api.ts + vault-members header badge

**Files:**
- Modify: `frontend/src/lib/api.ts` — extend `VaultInfo` type
- Modify: `frontend/src/pages/vault-members.tsx` — render badge on header

**Context:** Backend (Task 1) ships `role_source: "member" | "public"`. Frontend type extension + one visible-render path.

- [ ] **Step 1: Extend the API type**

In `frontend/src/lib/api.ts`, find the `VaultInfo` interface (grep for `interface VaultInfo`):

```typescript
export interface VaultInfo {
  // existing fields...
  role?: "owner" | "admin" | "writer" | "reader";
  role_source?: "member" | "public";  // NEW — optional for backwards compat
}
```

**Also extend the local interface** at `frontend/src/pages/vault-members.tsx:25-29` — it declares its own `VaultInfo` shape inline:

```typescript
interface VaultInfo {
  name: string;
  description?: string;
  role?: "owner" | "admin" | "writer" | "reader";
  role_source?: "member" | "public";  // NEW
}
```

Both the `api.ts` `VaultInfo` and the page-local `VaultInfo` need the new field. Don't replace the local one with an import — that's a wider refactor; just mirror the field.

- [ ] **Step 2: Update the members page header**

In `frontend/src/pages/vault-members.tsx` (around line 93 where `{info?.role && <RoleBadge role={info.role} />}` lives):

```tsx
{info?.role && (
  info.role_source === "public" ? (
    <span
      className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wider border border-warning/40 bg-warning/10 text-warning"
      title="This role is granted by the vault's public_access setting, not by direct membership. Contact the owner if this was unintended."
      aria-label={`Public ${info.role}`}
    >
      PUBLIC · {info.role.toUpperCase()}
    </span>
  ) : (
    <RoleBadge role={info.role} />
  )
)}
```

`text-warning` / `border-warning/40` / `bg-warning/10` — the `warning` semantic token is already defined at `frontend/src/index.css:39` (light: `#ca8a04`) and `:91` (dark variant) and is used by `badge.tsx`, `document.tsx`, `publications.tsx`. Use it directly — no theme additions or fallbacks needed.

- [ ] **Step 3: Typecheck + run suite**

```bash
cd frontend && pnpm tsc --noEmit && pnpm vitest run
```

Expected: clean + all green. No new tests in this task — the badge is a single conditional render; visual smoke is done in Task 7 (manual smoke).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/pages/vault-members.tsx
git commit -m "feat(ui): PUBLIC role badge when role_source=public"
```

---

## Task 6 — RoleSelect component + Undo toast on vault-members

**Files:**
- Create: `frontend/src/components/role-select.tsx`
- Create: `frontend/src/components/__tests__/role-select.test.tsx`
- Modify: `frontend/src/pages/vault-members.tsx`

**Context:** Backend `grantAccess(vault, user, role)` is already idempotent (`ON CONFLICT DO UPDATE`). New `RoleSelect` does optimistic UI + calls `grantAccess` + parent shows Undo toast. Owner row + self row use the read-only `RoleBadge`.

- [ ] **Step 1: Write the failing component test**

Create `frontend/src/components/__tests__/role-select.test.tsx`:

```typescript
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { RoleSelect } from "../role-select";
import * as api from "@/lib/api";

vi.mock("@/lib/api", () => ({ grantAccess: vi.fn() }));

afterEach(cleanup);

const baseMember = {
  username: "alice",
  display_name: "Alice",
  email: "alice@t.dev",
  role: "reader" as const,
  since: null,
};

describe("RoleSelect", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders three role options", () => {
    render(<RoleSelect vault="v" member={baseMember} onChanged={() => {}} />);
    const select = screen.getByLabelText(/change role for alice/i) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      "reader", "writer", "admin",
    ]);
    expect(select.value).toBe("reader");
  });

  it("calls grantAccess on change and reports prev/next", async () => {
    (api.grantAccess as any).mockResolvedValue({});
    const onChanged = vi.fn();
    render(<RoleSelect vault="v" member={baseMember} onChanged={onChanged} />);
    fireEvent.change(screen.getByLabelText(/change role for alice/i), {
      target: { value: "writer" },
    });
    await waitFor(() => expect(api.grantAccess).toHaveBeenCalledWith("v", "alice", "writer"));
    expect(onChanged).toHaveBeenCalledWith("reader", "writer");
  });

  it("surfaces inline error on rejection", async () => {
    (api.grantAccess as any).mockRejectedValue(new Error("boom"));
    render(<RoleSelect vault="v" member={baseMember} onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/change role for alice/i), {
      target: { value: "writer" },
    });
    expect(await screen.findByText(/boom/i)).toBeInTheDocument();
  });

  it("ignores same-value change", () => {
    render(<RoleSelect vault="v" member={baseMember} onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/change role for alice/i), {
      target: { value: "reader" },
    });
    expect(api.grantAccess).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run, see fail**

```bash
cd frontend && pnpm vitest run components/__tests__/role-select
```

Expected: module not found.

- [ ] **Step 3: Implement RoleSelect**

Create `frontend/src/components/role-select.tsx`:

```tsx
import { useState } from "react";
import { Loader2 } from "lucide-react";
import { grantAccess } from "@/lib/api";

export interface MemberLike {
  username: string;
  role: "reader" | "writer" | "admin" | "owner";
}

interface Props {
  vault: string;
  member: MemberLike;
  onChanged: (prev: string, next: string) => void;
}

const OPTIONS: Array<"reader" | "writer" | "admin"> = ["reader", "writer", "admin"];

export function RoleSelect({ vault, member, onChanged }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const next = e.target.value;
    const prev = member.role;
    if (next === prev) return;
    setBusy(true);
    setError(null);
    try {
      await grantAccess(vault, member.username, next);
      onChanged(prev, next);
    } catch (err: any) {
      setError(err?.message || "Failed to change role");
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
        className="appearance-none font-mono text-xs uppercase tracking-wider px-2 py-1 pr-6 border border-border bg-surface text-foreground hover:border-accent transition-colors duration-150 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface cursor-pointer"
      >
        {OPTIONS.map((r) => (
          <option key={r} value={r}>{r.toUpperCase()}</option>
        ))}
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

- [ ] **Step 4: Run, see pass**

```bash
cd frontend && pnpm vitest run components/__tests__/role-select
```

Expected: 4 passed.

- [ ] **Step 5: Wire into vault-members**

In `frontend/src/pages/vault-members.tsx`:

1. Add `currentUser` state:

```typescript
import { getMe } from "@/lib/api";
import { useEffect, useState } from "react";
// ...

const [currentUser, setCurrentUser] = useState<{ username: string } | null>(null);
useEffect(() => {
  getMe().then((u) => setCurrentUser({ username: u.username })).catch(() => setCurrentUser(null));
}, []);
```

2. Import `RoleSelect`:

```typescript
import { RoleSelect } from "@/components/role-select";
```

3. Replace `<RoleBadge role={m.role} />` (line ~164) with:

```tsx
{canManage && m.role !== "owner" && currentUser && m.username !== currentUser.username ? (
  <RoleSelect
    vault={name!}
    member={m}
    onChanged={(prev, next) => handleRoleChanged(m, prev, next)}
  />
) : (
  <RoleBadge role={m.role} />
)}
```

4. Add the `handleRoleChanged` function and Undo toast plumbing:

```typescript
import { grantAccess } from "@/lib/api";
// ...

const [undoTarget, setUndoTarget] = useState<{
  username: string;
  prev: string;
  next: string;
} | null>(null);

async function handleRoleChanged(m: Member, prev: string, next: string) {
  await refresh();  // get the updated member list
  setUndoTarget({ username: m.username, prev, next });
  // auto-clear after 5s
  setTimeout(() => {
    setUndoTarget((cur) =>
      cur && cur.username === m.username && cur.next === next ? null : cur,
    );
  }, 5000);
}

async function handleUndo() {
  if (!undoTarget) return;
  const { username, prev } = undoTarget;
  setUndoTarget(null);
  try {
    await grantAccess(name!, username, prev);
    await refresh();
  } catch (e: any) {
    setUndoError(e?.message || "Undo failed");
  }
}

// add Undo banner near top of the page (above the member list):
{undoTarget && (
  <div role="status" className="flex items-center gap-3 px-3 py-2 border border-border bg-surface-muted">
    <span className="text-sm text-foreground">
      Changed {undoTarget.username} from {undoTarget.prev.toUpperCase()} to {undoTarget.next.toUpperCase()}.
    </span>
    <button
      type="button"
      onClick={handleUndo}
      className="text-xs font-mono uppercase tracking-wider text-accent hover:underline"
    >
      Undo
    </button>
  </div>
)}
```

(The existing `refresh()` function in `vault-members.tsx:45` is the member-list re-fetcher — call it directly. No extraction needed.)

5. Verify the `Member` interface stays compatible with `RoleSelect`'s `MemberLike` (both have `username` and `role` — that's the contract).

- [ ] **Step 6: Run full suite + tsc**

```bash
cd frontend && pnpm vitest run && pnpm tsc --noEmit
```

Expected: all green. No regression in any existing test.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/role-select.tsx \
        frontend/src/components/__tests__/role-select.test.tsx \
        frontend/src/pages/vault-members.tsx
git commit -m "feat(ui): inline role change with Undo on members page"
```

---

## Task 7 — Integration verification + deploy

**Files:** (no source changes)

- [ ] **Step 1: Backend e2e sweep**

```bash
AKB_URL=http://localhost:8000 bash backend/tests/test_security_edge_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_auth_password_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_mcp_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_collection_lifecycle_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_vault_templates_e2e.sh
```

Expected: all green.

- [ ] **Step 2: Frontend full suite + tsc**

```bash
cd frontend && pnpm vitest run && pnpm tsc --noEmit
```

Expected: all green.

- [ ] **Step 3: Manual smoke against local backend**

If Vite dev is running on `:5173`:

1. **Issue 1**: Click vault A → tree opens. Close it. Click vault B → tree opens (vault B's default). Click vault A again → tree closed (vault A's saved state).
2. **Issue 2**: As owner, set a vault `public_access = "writer"`. Log out, register a second user, log in, browse to that vault's `/members` page → header shows `PUBLIC · WRITER` badge with hover tooltip.
3. **Issue 3**: On any vault page, click "New vault" → fill form → click Cancel → land back on the originating vault (not home). Then directly load `/vault/new` (no prior history) → Cancel goes to `/`.
4. **Issue 4**: Navigate around — top-left now has an `←` arrow button that's clearly clickable. On `/` it's disabled.
5. **Issue 5**: As admin/owner, go to `/vault/<name>/members`. Pick a non-owner non-self row → use the role `<select>` → change reader to writer → see Undo banner with 5s timeout → click Undo → role reverts.

- [ ] **Step 4: Push + deploy**

```bash
git push origin main
bash deploy/k8s/internal/deploy-internal.sh
until curl -sf <your-prod-host>/livez >/dev/null 2>&1; do sleep 5; done
curl -sk <your-prod-host>/livez
```

Expected: `{"status":"alive"}` from production.

- [ ] **Step 5: Production smoke**

Repeat Step 3's manual scenarios against `<your-prod-host>`.

---

## Notes for the executing engineer

- Backend container caches the codebase via `pip install --no-cache-dir .` — Task 1's `docker compose up -d --build backend` is necessary to pick up `role_source`. Subsequent frontend-only tasks don't need rebuilds; just `pnpm dev` for live edits or rely on the Vite container.
- `window.history.length` is genuinely a soft signal. JSDOM (Vitest) may report 0 for `MemoryRouter` cases regardless of `initialEntries`. If a test on this guard fails for reasons unrelated to your implementation, adjust the test fixture (more entries, different `initialIndex`) before changing the code.
- The Undo toast in Task 6 uses a simple in-page banner (not a global toast primitive) because the project doesn't currently have one. If a `Toaster` component exists elsewhere (grep `Toaster\|toast(` in `frontend/src/`), prefer it.
- `text-warning` semantic class — if the existing tailwind theme has only `text-accent` / `text-destructive` / `text-foreground-muted`, define `--warning` in `frontend/src/index.css` (or wherever the theme lives) before Task 5, or use `text-accent` as the visual cue. Pick whichever requires fewer file touches.
- Issue 2's badge currently lands only on the members page header. The vault overview page (`/vault/<name>`) may also have a role pill — verify in `frontend/src/pages/vault.tsx`; if yes, mirror the conditional render there with one extra commit. If no, leave it.
