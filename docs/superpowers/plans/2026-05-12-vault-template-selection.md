# Vault template selection Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the existing YAML-driven vault-template feature to the web vault-create form, and de-duplicate the on-disk YAML / MCP enum / soon-to-exist UI options through a single `TemplateRegistry` module.

**Architecture:** A small startup-scanned registry module reads `backend/templates/vault-templates/*.yaml` once and serves three shapes (names list for the MCP enum, structured summaries for the new REST endpoint, full payload for the existing apply step). The vault-new page fetches the summaries from a new `GET /vaults/templates` endpoint and exposes a dropdown + preview.

**Tech Stack:** Python 3.11 + FastAPI + asyncpg + PyYAML; React 19 + TypeScript + Vite + Vitest.

**Spec:** `docs/superpowers/specs/2026-05-12-vault-template-selection-design.md`

---

## Spec deviation note (read before Task 1)

The spec was written based on an earlier exploration that reported "REST `POST /vaults` does not accept template". That was incomplete. Inspection of `backend/app/api/routes/documents.py:21-26` shows the route signature already includes `template: str | None = None` and threads it through to `DocumentService.create_vault`. So:

- **Already there**: `template` query param on `POST /api/v1/vaults`, and the apply step (`_apply_template` reads YAML from disk).
- **Still missing** (the actual work for this plan): the `TemplateRegistry` module, `GET /vaults/templates` endpoint, MCP enum becoming dynamic, validation of `template` against the registry at the REST layer, and the frontend dropdown + preview + API client update.

The spec's "data flow on create" section remains accurate; only the "REST surface" section over-described what needed adding.

---

## File Map

**Backend — new**
- `backend/app/services/template_registry.py` — module-level cache + accessors (`list_summaries`, `list_names`, `get`)
- `backend/tests/test_template_registry.py` — pytest unit tests for the registry (no DB, no async)
- `backend/tests/test_vault_templates_e2e.sh` — shell e2e against running backend

**Backend — modified**
- `backend/mcp_server/tools.py` — `akb_create_vault` schema's `template.enum` switches from hardcoded list to `template_registry.list_names()`
- `backend/app/services/document_service.py:825-863` — `_apply_template` reads from registry instead of opening the YAML file
- `backend/app/api/routes/documents.py:21-26` — `create_vault` route validates `template` against `template_registry.list_names()` before delegating to the service

**Backend — modified (route file housing the new GET endpoint)**
- `backend/app/api/routes/documents.py` — add `GET /vaults/templates` next to the existing `POST /vaults` and `GET /vaults`

**Frontend — modified**
- `frontend/src/lib/api.ts` — `listVaultTemplates()` added; `createVault(name, description?, template?)` accepts the third arg
- `frontend/src/pages/vault-new.tsx` — fetch templates on mount, dropdown + preview, pass `template` to `createVault`

**Frontend — new**
- `frontend/src/pages/__tests__/vault-new.test.tsx` — Vitest coverage for the dropdown, preview, submit wiring, fetch-failure fallback

---

## Task 1 — `TemplateRegistry` module + unit tests

**Files:**
- Create: `backend/app/services/template_registry.py`
- Create: `backend/tests/test_template_registry.py`

**Context:** No app dependencies. Module-level `_scan()` runs at import; consumers read from cached `_PAYLOADS` / `_SUMMARIES`. Match the existing `backend/tests/test_collection_repo.py` test style — tests with no Postgres dependency just use plain pytest; the existing `conftest.py` makes `app.config` importable.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_template_registry.py`:

```python
"""Unit tests for TemplateRegistry."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Point the registry at a temp directory and re-scan."""
    from app.services import template_registry as tr
    monkeypatch.setattr(tr, "_TEMPLATES_DIR", tmp_path)
    tr._scan()
    return tr


def _write(tmp_path: Path, name: str, payload: dict) -> None:
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")


def test_list_summaries_returns_sorted_by_display_name(isolated_registry, tmp_path):
    _write(tmp_path, "z", {"name": "z", "display_name": "Zeta",
                            "description": "", "collections": [{"path": "a"}]})
    _write(tmp_path, "a", {"name": "a", "display_name": "Alpha",
                            "description": "", "collections": [{"path": "x"}]})
    isolated_registry._scan()
    names = [s.display_name for s in isolated_registry.list_summaries()]
    assert names == ["Alpha", "Zeta"]


def test_get_returns_full_payload_with_guide(isolated_registry, tmp_path):
    _write(tmp_path, "eng", {
        "name": "eng", "display_name": "Engineering", "description": "",
        "collections": [{"path": "specs", "guide": "spec guide"}],
    })
    isolated_registry._scan()
    payload = isolated_registry.get("eng")
    assert payload is not None
    assert payload["collections"][0]["guide"] == "spec guide"


def test_malformed_yaml_is_skipped_not_raised(isolated_registry, tmp_path):
    (tmp_path / "bad.yaml").write_text(":\n- this is\nnot: valid: yaml\n", encoding="utf-8")
    _write(tmp_path, "good", {"name": "good", "display_name": "Good",
                                "description": "", "collections": [{"path": "x"}]})
    isolated_registry._scan()
    assert isolated_registry.list_names() == ["good"]


def test_missing_collections_field_is_skipped(isolated_registry, tmp_path):
    _write(tmp_path, "empty", {"name": "empty", "display_name": "Empty",
                                 "description": "no collections", "collections": []})
    _write(tmp_path, "good", {"name": "good", "display_name": "Good",
                                "description": "", "collections": [{"path": "x"}]})
    isolated_registry._scan()
    assert isolated_registry.list_names() == ["good"]


def test_empty_dir_yields_empty_lists(isolated_registry):
    assert isolated_registry.list_summaries() == []
    assert isolated_registry.list_names() == []
    assert isolated_registry.get("anything") is None


def test_missing_dir_does_not_raise(tmp_path, monkeypatch):
    from app.services import template_registry as tr
    monkeypatch.setattr(tr, "_TEMPLATES_DIR", tmp_path / "does-not-exist")
    tr._scan()  # must not raise
    assert tr.list_names() == []


def test_collection_summary_falls_back_to_path(isolated_registry, tmp_path):
    """When collection entry omits 'name', summary uses 'path' as fallback."""
    _write(tmp_path, "x", {"name": "x", "display_name": "X",
                             "description": "", "collections": [{"path": "specs"}]})
    isolated_registry._scan()
    summary = isolated_registry.list_summaries()[0]
    assert summary.collections[0].path == "specs"
    assert summary.collections[0].name == "specs"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/kwoo2/Desktop/storage/akb/backend && \
  AKB_TEST_DSN="postgresql://akb:akb@localhost:15432/akb" \
  uv run --extra dev pytest tests/test_template_registry.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.template_registry'`.

- [ ] **Step 3: Implement the registry**

Create `backend/app/services/template_registry.py`:

```python
"""Vault-template registry.

Single-source-of-truth adapter over backend/templates/vault-templates/*.yaml.
Loaded once at module import; mutating the directory at runtime requires a
process restart.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent / "templates" / "vault-templates"
)


@dataclass(frozen=True)
class CollectionSummary:
    path: str
    name: str


@dataclass(frozen=True)
class TemplateSummary:
    name: str
    display_name: str
    description: str
    collection_count: int
    collections: list[CollectionSummary]


_PAYLOADS: dict[str, dict] = {}
_SUMMARIES: list[TemplateSummary] = []


def _scan() -> None:
    """Read every *.yaml in the templates dir; populate caches."""
    payloads: dict[str, dict] = {}
    summaries: list[TemplateSummary] = []
    if not _TEMPLATES_DIR.exists():
        logger.warning("Vault templates dir missing: %s", _TEMPLATES_DIR)
        _PAYLOADS.clear()
        _SUMMARIES.clear()
        return
    for path in sorted(_TEMPLATES_DIR.glob("*.yaml")):
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            logger.warning("Skipping malformed template %s: %s", path.name, exc)
            continue
        if not isinstance(data, dict):
            logger.warning("Template %s is not a YAML mapping; skipping", path.name)
            continue
        name = data.get("name") or path.stem
        collections = data.get("collections") or []
        if not collections:
            logger.warning("Template %s has no collections; skipping", name)
            continue
        payloads[name] = data
        summaries.append(
            TemplateSummary(
                name=name,
                display_name=data.get("display_name", name),
                description=data.get("description", ""),
                collection_count=len(collections),
                collections=[
                    CollectionSummary(path=c["path"], name=c.get("name", c["path"]))
                    for c in collections
                    if isinstance(c, dict) and "path" in c
                ],
            )
        )
    summaries.sort(key=lambda s: s.display_name)
    _PAYLOADS.clear()
    _PAYLOADS.update(payloads)
    _SUMMARIES.clear()
    _SUMMARIES.extend(summaries)


def list_summaries() -> list[TemplateSummary]:
    return list(_SUMMARIES)


def list_names() -> list[str]:
    return [s.name for s in _SUMMARIES]


def get(name: str) -> dict | None:
    return _PAYLOADS.get(name)


# Scan once at module import.
_scan()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && AKB_TEST_DSN="postgresql://akb:akb@localhost:15432/akb" \
  uv run --extra dev pytest tests/test_template_registry.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Sanity-check against real templates**

```bash
cd backend && uv run --extra dev python -c \
  "from app.services import template_registry as tr; \
   print([s.name for s in tr.list_summaries()]); \
   print(tr.list_names())"
```
Expected: a non-empty list including `'engineering'`, `'qa'`, `'hr'`, `'finance'`, `'management'`, `'issue-tracking'`, `'product'` (alphabetical-by-display-name order, so likely `engineering, finance, hr, ...`).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/template_registry.py backend/tests/test_template_registry.py
git commit -m "feat(template-registry): scan vault-template YAML directory once at import"
```

---

## Task 2 — `_apply_template` reads from registry

**Files:**
- Modify: `backend/app/services/document_service.py:825-863`

**Context:** The current `_apply_template` opens the YAML file directly. After Task 1 it should call `template_registry.get(name)` instead. This is a behavior-preserving refactor — the YAML payload is the same dict either way. No new behavior; existing `test_mcp_e2e.sh` template assertion is the regression check.

- [ ] **Step 1: Read the current implementation**

```bash
sed -n '825,863p' backend/app/services/document_service.py
```

Confirm the structure matches the snippet pasted in the spec.

- [ ] **Step 2: Modify `_apply_template`**

Replace the body of `_apply_template` in `backend/app/services/document_service.py:825-863` so the YAML-from-disk read is replaced by a registry call. Keep the rest of the function (collection creation loop, guide file commit) byte-identical.

Before:

```python
async def _apply_template(self, vault_name: str, vault_id: uuid.UUID, template: str, coll_repo) -> None:
    """Apply a vault template by reading from _system vault or built-in defaults."""
    import yaml
    from pathlib import Path

    template_path = Path(__file__).parent.parent.parent / "templates" / "vault-templates" / f"{template}.yaml"
    if not template_path.exists():
        logger.warning("Template not found: %s", template)
        return

    with open(template_path) as f:
        tmpl = yaml.safe_load(f)

    now = datetime.now(timezone.utc)
    for coll in tmpl.get("collections", []):
        # … unchanged …
```

After:

```python
async def _apply_template(self, vault_name: str, vault_id: uuid.UUID, template: str, coll_repo) -> None:
    """Apply a vault template via the shared TemplateRegistry."""
    from app.services import template_registry

    tmpl = template_registry.get(template)
    if tmpl is None:
        logger.warning("Template not found: %s", template)
        return

    now = datetime.now(timezone.utc)
    for coll in tmpl.get("collections", []):
        # … unchanged …
```

Drop the two now-unused imports (`yaml`, `Path`) at the top of the function. (If they are also used elsewhere in the file, leave them.)

- [ ] **Step 3: Smoke-check the import path**

```bash
cd backend && uv run --extra dev python -c \
  "from app.services.document_service import DocumentService; print('ok')"
```
Expected: `ok`.

- [ ] **Step 4: Run existing MCP e2e to confirm no regression**

```bash
cd backend && AKB_URL=http://localhost:8000 bash tests/test_mcp_e2e.sh 2>&1 | tail -20
```

Expected: the suite passes including any template-related assertions. The backend must be running (`docker compose up -d` if local; the k8s instance also works if `AKB_URL` points at it).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/document_service.py
git commit -m "refactor(service): _apply_template reads from TemplateRegistry"
```

---

## Task 3 — MCP `akb_create_vault` enum is dynamic

**Files:**
- Modify: `backend/mcp_server/tools.py` (around lines 19-49, the `akb_create_vault` Tool definition)

**Context:** The hardcoded `enum: ["engineering", "qa", "hr", "finance", "management", "issue-tracking", "product"]` is replaced by a call to `template_registry.list_names()`. The MCP SDK stores `inputSchema` by value at module-import time, so this is a one-shot read; `tools.py` itself is imported once per process.

- [ ] **Step 1: Locate the existing enum**

```bash
grep -n "engineering.*qa.*hr" backend/mcp_server/tools.py
```

You should see one line in the `akb_create_vault` template-parameter schema.

- [ ] **Step 2: Replace the hardcoded enum**

In `backend/mcp_server/tools.py`, near the top of the file (with the other imports):

```python
from app.services import template_registry
```

In the `akb_create_vault` Tool's `inputSchema.properties.template` block, replace the hardcoded list:

```python
"template": {
    "type": "string",
    "enum": template_registry.list_names(),
    "description": (
        "Vault template to apply (pre-creates collections with guides). "
        "Ignored when external_git is set."
    ),
},
```

- [ ] **Step 3: Smoke-check the schema**

```bash
cd backend && uv run --extra dev python -c \
  "from mcp_server.tools import TOOLS; \
   t = next(x for x in TOOLS if x.name == 'akb_create_vault'); \
   print(t.inputSchema['properties']['template']['enum'])"
```
Expected: a list matching what `template_registry.list_names()` returned in Task 1 Step 5.

- [ ] **Step 4: Commit**

```bash
git add backend/mcp_server/tools.py
git commit -m "feat(mcp): akb_create_vault template enum derives from TemplateRegistry"
```

---

## Task 4 — REST `GET /vaults/templates` endpoint

**Files:**
- Modify: `backend/app/api/routes/documents.py` (add the new route alongside the existing `/vaults` routes around line 29)

**Context:** Returns a list of templates including a per-collection preview. Auth-gated to any logged-in user via the existing `get_current_user` dependency, matching the neighboring `/vaults` routes.

- [ ] **Step 1: Add the route + response model**

In `backend/app/api/routes/documents.py`, near the top imports add:

```python
from pydantic import BaseModel

from app.services import template_registry
```

Define the response shape near the top of the file (after the existing imports, before `router = APIRouter()`):

```python
class VaultTemplateCollection(BaseModel):
    path: str
    name: str


class VaultTemplate(BaseModel):
    name: str
    display_name: str
    description: str
    collection_count: int
    collections: list[VaultTemplateCollection]
```

Insert the route after the existing `GET /vaults` (around line 32) but before `POST /documents`:

```python
@router.get("/vaults/templates", response_model=list[VaultTemplate], summary="List available vault templates")
async def list_vault_templates(user: AuthenticatedUser = Depends(get_current_user)):
    return [
        VaultTemplate(
            name=s.name,
            display_name=s.display_name,
            description=s.description,
            collection_count=s.collection_count,
            collections=[
                VaultTemplateCollection(path=c.path, name=c.name)
                for c in s.collections
            ],
        )
        for s in template_registry.list_summaries()
    ]
```

**Route order matters:** the new route must be registered before any `GET /vaults/{vault}` catch-all (there isn't one in `documents.py` today, but `access.py` has `/vaults/{vault}/info`). FastAPI matches the more-specific literal path `/vaults/templates` first as long as it's registered. Verify with the smoke-check in Step 3.

- [ ] **Step 2: Smoke-check the route registers**

```bash
cd backend && uv run --extra dev python -c \
  "from app.main import app; \
   print([r.path for r in app.routes if 'template' in r.path.lower()])"
```
Expected output includes `'/api/v1/vaults/templates'`.

- [ ] **Step 3: Verify route ordering doesn't shadow**

```bash
cd backend && uv run --extra dev python -c \
  "from app.main import app; \
   paths = [r.path for r in app.routes if r.path.startswith('/api/v1/vaults')]; \
   print('\n'.join(paths))"
```

Expected: `/api/v1/vaults/templates` appears before any catch-all like `/api/v1/vaults/{vault}/info`. If it appears later (because of include-router order), the literal still wins thanks to FastAPI's matching policy — but eyeball the order to make sure no `/vaults/{name}` catch-all appears in `documents.py` itself with `templates` matching as a `name`.

- [ ] **Step 4: Smoke-check the response shape**

```bash
docker compose up -d backend  # if not already running
# or rely on the existing local stack at :8000
cd backend
# Use any logged-in PAT — bootstrap a fresh one quickly:
U="tpl-check-$(date +%s)"
curl -sk -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$U\",\"email\":\"$U@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(curl -sk -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$U\",\"password\":\"test1234\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -sk http://localhost:8000/api/v1/vaults/templates -H "Authorization: Bearer $JWT" | python3 -m json.tool
```

Expected: an array of 7 templates each with the expected fields. Sample first element should show `engineering` or `Engineering` as display_name and 6 collections.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes/documents.py
git commit -m "feat(api): GET /vaults/templates exposes registry summaries"
```

---

## Task 5 — REST `POST /vaults` validates `template` against registry

**Files:**
- Modify: `backend/app/api/routes/documents.py:21-26`

**Context:** Today the route accepts `template` as a query param but does not validate it — an unknown name silently produces an empty vault because `_apply_template` warns + returns when the YAML isn't found. The spec requires 400 on unknown template. Add validation before delegating to `DocumentService.create_vault`.

- [ ] **Step 1: Write the failing e2e assertion (will be tied to Task 6 too)**

We'll write the full e2e file in Task 6; this step is a placeholder reminder. Skip and proceed.

- [ ] **Step 2: Add validation in the route**

In `backend/app/api/routes/documents.py`, the existing import is `from fastapi import APIRouter, Depends`. **Update that line to also import `HTTPException` and `status`**:

```python
from fastapi import APIRouter, Depends, HTTPException, status


@router.post("/vaults", summary="Create a new vault")
async def create_vault(
    name: str,
    description: str = "",
    template: str | None = None,
    public_access: str = "none",
    user: AuthenticatedUser = Depends(get_current_user),
):
    if template is not None and template not in template_registry.list_names():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown template: {template}",
        )
    # ... rest of the existing route body unchanged

    name = to_nfc(name)
    description = to_nfc(description)
    vault_id = await doc_service.create_vault(
        name, description,
        owner_id=user.user_id, template=template, public_access=public_access,
    )
    return {
        "vault_id": vault_id, "name": name,
        "template": template, "public_access": public_access,
    }
```

(If `HTTPException` and `status` aren't yet imported in this file, add them to the existing FastAPI import line.)

- [ ] **Step 3: Smoke-check the validation**

Against the running backend (re-use the PAT from Task 4 Step 4):

```bash
curl -sk -X POST "http://localhost:8000/api/v1/vaults?name=tpl-bad-$(date +%s)&template=does-not-exist" \
  -H "Authorization: Bearer $JWT" -w "\nHTTP %{http_code}\n"
```

Expected: `HTTP 400` with `Unknown template: does-not-exist` in the body.

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/routes/documents.py
git commit -m "feat(api): POST /vaults rejects unknown template with 400"
```

---

## Task 6 — Backend E2E suite

**Files:**
- Create: `backend/tests/test_vault_templates_e2e.sh`

**Context:** Follows the project's shell e2e convention (self-contained, registers its own user, defines own `pass`/`fail` counters). Reference: `backend/tests/test_collection_lifecycle_e2e.sh`.

- [ ] **Step 1: Create the file**

Create `backend/tests/test_vault_templates_e2e.sh` (executable):

```bash
#!/usr/bin/env bash
# E2E for vault template selection: GET /vaults/templates response shape,
# POST /vaults?template=... applies collections, unknown template → 400,
# unauthenticated → 401.
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   Vault Template Selection E2E           ║"
echo "║   Target: $BASE_URL                       "
echo "╚══════════════════════════════════════════╝"

# 0. Bootstrap user + JWT
USER="tpl-e2e-$(date +%s)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
[ -n "$JWT" ] && pass "bootstrap user" || { fail "bootstrap" "no JWT"; exit 1; }

# 1. GET /vaults/templates (authenticated)
echo "▸ 1. List templates"
R=$(curl -sk "$BASE_URL/api/v1/vaults/templates" -H "Authorization: Bearer $JWT")
COUNT=$(echo "$R" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')
[ "$COUNT" -ge 1 ] 2>/dev/null && pass "GET /vaults/templates returns $COUNT templates" \
  || fail "GET /vaults/templates" "got $COUNT items"

HAS_ENG=$(echo "$R" | python3 -c \
  'import sys,json; ts=json.load(sys.stdin); print(any(t["name"]=="engineering" for t in ts))')
[ "$HAS_ENG" = "True" ] && pass "engineering template listed" \
  || fail "engineering missing" "$R"

ENG_COLLS=$(echo "$R" | python3 -c \
  'import sys,json; ts=json.load(sys.stdin); \
   t=next(x for x in ts if x["name"]=="engineering"); \
   print(t["collection_count"])')
[ "$ENG_COLLS" -ge 5 ] 2>/dev/null \
  && pass "engineering has $ENG_COLLS collections" \
  || fail "engineering collection_count" "got $ENG_COLLS"

# 2. GET /vaults/templates without auth → 401
echo "▸ 2. ACL"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/vaults/templates")
[ "$HTTP" = "401" ] && pass "unauthenticated → 401" || fail "no-auth check" "got $HTTP"

# 3. POST /vaults?template=engineering creates seeded collections
echo "▸ 3. Apply template"
VAULT="tpl-eng-$(date +%s)"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/vaults?name=$VAULT&template=engineering" \
  -H "Authorization: Bearer $JWT")
[ "$HTTP" = "200" ] && pass "POST /vaults?template=engineering" \
  || { fail "create with template" "got $HTTP"; }

# Browse and confirm engineering collections present
R=$(curl -sk "$BASE_URL/api/v1/browse/$VAULT" -H "Authorization: Bearer $JWT")
HAS_SPECS=$(echo "$R" | python3 -c \
  'import sys,json; d=json.load(sys.stdin); \
   print(any(i.get("name")=="specs" for i in d.get("items",[])))')
[ "$HAS_SPECS" = "True" ] && pass "engineering collections seeded" \
  || fail "seed check" "$R"

# 4. POST /vaults?template=garbage → 400
echo "▸ 4. Unknown template rejected"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/vaults?name=tpl-bad-$(date +%s)&template=does-not-exist" \
  -H "Authorization: Bearer $JWT")
[ "$HTTP" = "400" ] && pass "unknown template → 400" \
  || fail "validation" "got $HTTP"

# 5. POST /vaults with no template → empty vault still works
echo "▸ 5. No-template regression"
VAULT2="tpl-empty-$(date +%s)"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/vaults?name=$VAULT2" \
  -H "Authorization: Bearer $JWT")
[ "$HTTP" = "200" ] && pass "no-template POST /vaults" \
  || fail "no-template" "got $HTTP"

# Summary
echo ""
echo "═══════════════════════════════════"
if [ $FAIL -eq 0 ]; then
  echo "✓ All $PASS tests passed"
  exit 0
else
  echo "✗ $FAIL failures (of $((PASS+FAIL)) total)"
  printf '  - %s\n' "${ERRORS[@]}"
  exit 1
fi
```

- [ ] **Step 2: Make executable + syntax check**

```bash
chmod +x backend/tests/test_vault_templates_e2e.sh
bash -n backend/tests/test_vault_templates_e2e.sh && echo "SYNTAX OK"
```
Expected: `SYNTAX OK`.

- [ ] **Step 3: Run against live backend**

```bash
AKB_URL=http://localhost:8000 bash backend/tests/test_vault_templates_e2e.sh
```

Expected: all assertions pass. If the backend is not running locally, start with `docker compose up -d backend postgres` and re-run.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_vault_templates_e2e.sh
git commit -m "test(e2e): vault-template selection coverage"
```

---

## Task 7 — Frontend `api.ts` helpers

**Files:**
- Modify: `frontend/src/lib/api.ts`

**Context:** Add `listVaultTemplates` and extend `createVault` to accept a third optional `template` arg. Backwards-compatible: existing callers pass `(name, description)` only.

- [ ] **Step 1: Read the current shape**

```bash
grep -n "createVault\|listVaults" frontend/src/lib/api.ts
```

Expected: a `createVault(name, description?)` arrow function around line 88-89.

- [ ] **Step 2: Add type + helper, extend createVault**

In `frontend/src/lib/api.ts`, add near the `listVaults` helper (around line 87):

```typescript
export interface VaultTemplateCollection {
  path: string;
  name: string;
}

export interface VaultTemplateSummary {
  name: string;
  display_name: string;
  description: string;
  collection_count: number;
  collections: VaultTemplateCollection[];
}

export const listVaultTemplates = () =>
  api<VaultTemplateSummary[]>("/vaults/templates");
```

Replace the existing `createVault` arrow:

```typescript
export const createVault = (
  name: string,
  description?: string,
  template?: string,
) => {
  const params = new URLSearchParams({ name });
  if (description) params.set("description", description);
  if (template) params.set("template", template);
  return api<any>(`/vaults?${params}`, { method: "POST" });
};
```

(Switching to `URLSearchParams` matches what we did for `deleteCollection` in the prior collection-lifecycle work.)

- [ ] **Step 3: Typecheck**

```bash
cd frontend && pnpm tsc --noEmit
```
Expected: no errors. (Existing callers like `vault-new.tsx` pass `(name, description)` — the 2-arg form is still valid.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(api-client): listVaultTemplates + createVault template arg"
```

---

## Task 8 — Vault-new dropdown + preview + tests

**Files:**
- Modify: `frontend/src/pages/vault-new.tsx`
- Create: `frontend/src/pages/__tests__/vault-new.test.tsx`

**Context:** Inserts a template `<select>` between description and the existing "external git coming soon" tooltip block. Shows a one-line preview (description + collection paths joined by `·`) when a template is selected.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/pages/__tests__/vault-new.test.tsx`:

```typescript
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import VaultNewPage from "../vault-new";
import * as api from "@/lib/api";

vi.mock("@/lib/api", () => ({
  listVaultTemplates: vi.fn(),
  createVault: vi.fn(),
}));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/vault/new"]}>
      <Routes>
        <Route path="/vault/new" element={<VaultNewPage />} />
        <Route path="/vault/:name" element={<div data-testid="vault-landed" />} />
      </Routes>
    </MemoryRouter>,
  );
}

const SAMPLE = [
  {
    name: "engineering", display_name: "Engineering",
    description: "Software dev",
    collection_count: 2,
    collections: [{ path: "specs", name: "Specs" }, { path: "decisions", name: "Decisions" }],
  },
  {
    name: "qa", display_name: "QA",
    description: "Quality assurance",
    collection_count: 1,
    collections: [{ path: "test-plans", name: "Test plans" }],
  },
];

describe("VaultNewPage template selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.createVault as any).mockResolvedValue({ vault_id: "v1", name: "x" });
  });

  it("renders dropdown with 'None' + fetched templates", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    const select = await screen.findByLabelText(/template/i);
    await waitFor(() => expect((select as HTMLSelectElement).options.length).toBe(3));
    const labels = Array.from((select as HTMLSelectElement).options).map((o) => o.text);
    expect(labels[0]).toMatch(/none/i);
    expect(labels).toContain("Engineering");
    expect(labels).toContain("QA");
  });

  it("shows preview when a template is selected", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    const select = (await screen.findByLabelText(/template/i)) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(3));
    fireEvent.change(select, { target: { value: "engineering" } });
    expect(await screen.findByText(/software dev/i)).toBeInTheDocument();
    expect(screen.getByText(/specs/)).toBeInTheDocument();
    expect(screen.getByText(/decisions/)).toBeInTheDocument();
  });

  it("hides preview for 'None'", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    await waitFor(() => expect(api.listVaultTemplates).toHaveBeenCalled());
    expect(screen.queryByText(/software dev/i)).not.toBeInTheDocument();
  });

  it("submits with undefined template when 'None' selected", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "mvault" } });
    fireEvent.click(screen.getByRole("button", { name: /create vault/i }));
    await waitFor(() =>
      expect(api.createVault).toHaveBeenCalledWith("mvault", undefined, undefined),
    );
  });

  it("submits with selected template name", async () => {
    (api.listVaultTemplates as any).mockResolvedValue(SAMPLE);
    renderPage();
    const select = (await screen.findByLabelText(/template/i)) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(3));
    fireEvent.change(select, { target: { value: "qa" } });
    fireEvent.change(screen.getByLabelText(/^name/i), { target: { value: "qvault" } });
    fireEvent.click(screen.getByRole("button", { name: /create vault/i }));
    await waitFor(() =>
      expect(api.createVault).toHaveBeenCalledWith("qvault", undefined, "qa"),
    );
  });

  it("falls back to 'None' only when listVaultTemplates rejects", async () => {
    (api.listVaultTemplates as any).mockRejectedValue(new Error("boom"));
    renderPage();
    const select = (await screen.findByLabelText(/template/i)) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(1));
    expect(select.options[0].text).toMatch(/none/i);
  });
});
```

- [ ] **Step 2: Run, see fail**

```bash
cd frontend && pnpm vitest run pages/__tests__/vault-new
```
Expected: failures (no `<select>` with `template` label exists yet).

- [ ] **Step 3: Implement the UI**

In `frontend/src/pages/vault-new.tsx`:

Add import at the top:

```typescript
import { useEffect, useMemo, useState } from "react";
import { listVaultTemplates, createVault, type VaultTemplateSummary } from "@/lib/api";
```

(Adjust the existing imports — `useEffect` and `useMemo` are new; `createVault` already imported.)

Inside `VaultNewPage`, after the existing `useState` hooks, add:

```typescript
const [templates, setTemplates] = useState<VaultTemplateSummary[]>([]);
const [selectedTemplate, setSelectedTemplate] = useState<string>("");

useEffect(() => {
  listVaultTemplates()
    .then(setTemplates)
    .catch((e) => {
      console.warn("Failed to load vault templates; falling back to none-only.", e);
      setTemplates([]);
    });
}, []);

const selectedSummary = useMemo(
  () => templates.find((t) => t.name === selectedTemplate) || null,
  [templates, selectedTemplate],
);
```

Update `handleSubmit` to pass the template:

```typescript
await createVault(
  trimmed,
  description.trim() || undefined,
  selectedTemplate || undefined,
);
```

Insert the template UI block between the description input (ending at the `</div>` after `<Input id="vault-description" .../>`) and the `<TooltipProvider>` block:

```tsx
<div className="space-y-1.5">
  <Label htmlFor="vault-template">
    Template <span className="normal-case tracking-normal text-foreground-muted">(optional)</span>
  </Label>
  <select
    id="vault-template"
    value={selectedTemplate}
    onChange={(e) => setSelectedTemplate(e.target.value)}
    className="w-full bg-surface border border-border px-3 py-2 text-sm font-mono focus:outline-none focus:border-accent transition-colors"
  >
    <option value="">None — empty vault</option>
    {templates.map((t) => (
      <option key={t.name} value={t.name}>{t.display_name}</option>
    ))}
  </select>
  {selectedSummary && (
    <div className="coord">
      {selectedSummary.description}
      <br />
      Will create {selectedSummary.collection_count} collections:{" "}
      {selectedSummary.collections.map((c) => c.path).join(" · ")}
    </div>
  )}
</div>
```

- [ ] **Step 4: Run tests until green**

```bash
cd frontend && pnpm vitest run pages/__tests__/vault-new
```
Expected: 6 passes.

- [ ] **Step 5: Typecheck + full suite**

```bash
cd frontend && pnpm tsc --noEmit && pnpm vitest run
```
Expected: all green.

- [ ] **Step 6: Manual smoke (optional, if dev server running)**

If Vite is up on :5173, navigate to `/vault/new` and confirm:
- Template dropdown appears between description and the disabled external-git block.
- Selecting "Engineering" shows a preview "Software dev / Will create 6 collections: specs · decisions · ..." (or whatever the live yaml says).
- Creating with no template still works (creates empty vault). Creating with template seeds the collections (visible in the explorer after navigation).

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/vault-new.tsx frontend/src/pages/__tests__/vault-new.test.tsx
git commit -m "feat(ui): vault-new template picker + preview"
```

---

## Task 9 — Final integration verification

**Files:** (no source changes)

- [ ] **Step 1: Backend full e2e sweep**

```bash
AKB_URL=http://localhost:8000 bash backend/tests/test_vault_templates_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_mcp_e2e.sh
```
Expected: both green.

- [ ] **Step 2: Frontend tests + tsc**

```bash
cd frontend && pnpm vitest run && pnpm tsc --noEmit
```
Expected: all green.

- [ ] **Step 3: Manual path**

1. `pnpm dev` (or production URL) → log in.
2. Create vault with template "Engineering" → land on vault page → explorer shows the seeded collections.
3. Create vault with no template → empty vault → explorer shows empty state.
4. Try `?template=does-not-exist` via curl → 400.

- [ ] **Step 4: Deploy** (if desired in same session)

```bash
bash deploy/k8s/internal/deploy-internal.sh
curl -sk https://akb.agent.seahorse.dnotitia.com/livez
```
Expected: `{"status":"alive"}`.

---

## Notes for the executing engineer

- The MCP enum is captured at server-startup. After deploying a new template YAML, the operator must restart the backend pod (`kubectl rollout restart deployment/backend -n akb`) for the enum and registry to pick it up. There is no hot-reload.
- The route ordering (`/vaults/templates` vs any future `/vaults/{name}`) is fine today because no `/vaults/{name}` catch-all exists in `documents.py`. If anyone later adds one, register `/vaults/templates` first explicitly to avoid silent shadowing.
- Backwards-compat: `createVault(name)` and `createVault(name, description)` continue to work; only the new `template` third arg is opt-in. No call-site sweep needed.
- Template YAML schema for "name" at the collection level: the existing `engineering.yaml` already uses `name` (e.g. `name: "API & System Specs"`). The registry's `CollectionSummary.name` falls back to `path` when absent; all current YAML have `name` set so the fallback is a defensive only.
