# Vault template selection — design

**Date**: 2026-05-12
**Status**: Approved (brainstorming)
**Owner**: kwoo24
**Related**: existing vault-template YAML files (`backend/templates/vault-templates/*.yaml`), `DocumentService.create_vault` + `_apply_template` (`backend/app/services/document_service.py:726`), MCP `akb_create_vault` tool (`backend/mcp_server/tools.py:19`)

## Problem

The vault-creation form on `frontend/src/pages/vault-new.tsx` collects only `name` and `description`, even though the backend has been able to provision a vault from a curated YAML template (engineering / qa / hr / finance / management / issue-tracking / product) for some time via the MCP path. The capability exists end-to-end on the agent side and is dead on the UI side.

Two consequences:

1. **Template feature invisible to the human workflow.** A user creating a vault through the web app gets an empty vault even though seven well-defined templates are sitting on disk. The only way to apply a template is to spawn an MCP client. The 의도 of templates ("hand new vault a sensible starter shape") is not delivered to the audience that needs it most — humans clicking "New vault" in the browser.

2. **Template list is maintained in three places that can drift.** The actual templates live as YAML files under `backend/templates/vault-templates/`. The MCP tool's `template` parameter has a hardcoded `enum` of seven names (`tools.py:30-34`). The frontend dropdown — once added — would need its own copy. Adding `qa-v2.yaml` to the directory requires three edits in three files in lockstep or the lists silently disagree.

The UI gap is the user-visible pain; the drift risk is the second-order pain we want to head off while we're in this code.

## Non-goals

- **Editing or creating templates through the UI.** Templates remain operator-curated YAML in the repo. Adding a template is a code change (PR), not an in-app action.
- **Per-vault template post-creation editing.** Templates are applied once at vault creation. Re-applying or swapping a template on an existing vault is out of scope.
- **Backwards-incompatible REST migration.** Existing `POST /api/v1/vaults?name=X&description=Y` query-string shape is preserved; `template` is added as an additional query param. JSON-body migration is a separate cleanup.
- **MCP tool schema versioning.** The `template` enum will become dynamic; clients that cache the schema will catch up via the existing `tools/list_changed` mechanism. No new versioning scheme.
- **Hot-reload of templates.** The directory is scanned once at server startup. Adding a YAML at runtime requires `kubectl rollout restart`. The 7 templates have been stable for a long time; live reload is YAGNI.
- **External-git vault template support.** External-git vaults (`akb_create_vault(external_git={...})`) ignore the `template` parameter today and continue to do so.

## Architecture

```
backend/templates/vault-templates/*.yaml         (source of truth)
                │
                │ read once at process start
                ▼
backend/app/services/template_registry.py        (NEW — module-level cache)
   │
   ├── list_summaries() -> [TemplateSummary]     (name, display_name, description,
   │                                              collection_count, collections[].path)
   │
   ├── get(name) -> dict | None                  (full YAML payload for _apply_template)
   │
   └── list_names() -> [str]                     (MCP enum source)
        │
        ├──── used by ────────► backend/mcp_server/tools.py
        │                        (TOOLS list now imports list_names() at module-load)
        │
        ├──── used by ────────► backend/app/services/document_service.py
        │                        (_apply_template calls registry.get(name) instead of
        │                        reading the YAML file directly)
        │
        └──── exposed by ─────► backend/app/api/routes/vaults.py
                                 GET /api/v1/vaults/templates
                                 │  └─► returns [TemplateSummary]
                                 │
                                 POST /api/v1/vaults?name&description&template
                                    └─► template validated against registry.list_names()
                                        before vault-create call
```

Single source of truth (the YAML directory), single registry that adapts that source for three call sites (MCP enum, REST GET, REST POST validation, and the existing in-process apply step).

### Why a registry module instead of inlining each call site

The three call sites have different read patterns: MCP needs just the names at server-startup, the REST GET needs the structured summary with `collection_count` and a per-collection preview list, the apply path needs the full YAML payload including `guide` bodies. Inlining a YAML re-scan in each call site would either duplicate the file-walk logic three times or force the apply path to re-parse a YAML it already has. A small module that scans once and serves three shapes from a cached dict is the right boundary.

## Backend — template registry

### New file: `backend/app/services/template_registry.py`

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


# name → parsed YAML dict
_PAYLOADS: dict[str, dict] = {}
# name → summary (sorted by display_name)
_SUMMARIES: list[TemplateSummary] = []


def _scan() -> None:
    """Read every *.yaml in the templates dir; populate caches."""
    payloads: dict[str, dict] = {}
    summaries: list[TemplateSummary] = []
    if not _TEMPLATES_DIR.exists():
        logger.warning("Vault templates dir missing: %s", _TEMPLATES_DIR)
        return
    for path in sorted(_TEMPLATES_DIR.glob("*.yaml")):
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            logger.warning("Skipping malformed template %s: %s", path.name, exc)
            continue
        name = data.get("name") or path.stem
        if not data.get("collections"):
            logger.warning("Template %s has no collections; skipping", name)
            continue
        payloads[name] = data
        summaries.append(
            TemplateSummary(
                name=name,
                display_name=data.get("display_name", name),
                description=data.get("description", ""),
                collection_count=len(data["collections"]),
                collections=[
                    CollectionSummary(path=c["path"], name=c.get("name", c["path"]))
                    for c in data["collections"]
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


# Scan at module import — happens once per process.
_scan()
```

Validation rules: a YAML is included iff it parses, has a non-empty `collections` list, and the `path` field is present on every entry. Anything else is logged as a warning and skipped. The registry never raises on bad YAML; the operator notices via logs.

### Changes: `backend/mcp_server/tools.py`

The hardcoded list `["engineering", "qa", "hr", "finance", "management", "issue-tracking", "product"]` in the `akb_create_vault` schema becomes:

```python
from app.services import template_registry

# inside the akb_create_vault Tool(..., inputSchema=...):
"template": {
    "type": "string",
    "enum": template_registry.list_names(),
    "description": (
        "Vault template to apply (pre-creates collections with guides). "
        "Ignored when external_git is set."
    ),
},
```

`list_names()` returns the sorted list at module-load time. The `tools.py` module is imported once when the MCP server starts; client schema updates propagate on the next `tools/list` call. If the YAML directory is empty, the enum becomes `[]` and the template parameter effectively becomes unselectable from any MCP client that validates `enum` constraints — acceptable, equivalent to having no templates.

### Changes: `backend/app/services/document_service.py`

`_apply_template` currently reads the YAML file from disk in-line (`_apply_template` at lines 825-863). Replace the YAML read with a registry call:

```python
async def _apply_template(self, vault_name, vault_id, template, coll_repo) -> None:
    from app.services import template_registry

    tmpl = template_registry.get(template)
    if tmpl is None:
        logger.warning("Template not found: %s", template)
        return
    # … rest of the existing loop, unchanged …
```

No other change to the apply logic. The 의도 of `_apply_template` is unchanged; only the source of the template dict moves from "open file" to "ask registry".

## Backend — REST surface

### New route: `GET /api/v1/vaults/templates`

```python
# backend/app/api/routes/vaults.py
from app.services import template_registry

class VaultTemplate(BaseModel):
    name: str
    display_name: str
    description: str
    collection_count: int
    collections: list[dict]   # [{"path": str, "name": str}, ...]


@router.get("/vaults/templates", response_model=list[VaultTemplate])
async def list_vault_templates(
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List available vault templates. Auth required (any logged-in user)."""
    return [
        VaultTemplate(
            name=s.name,
            display_name=s.display_name,
            description=s.description,
            collection_count=s.collection_count,
            collections=[{"path": c.path, "name": c.name} for c in s.collections],
        )
        for s in template_registry.list_summaries()
    ]
```

Auth gate matches the rest of the vault routes: any logged-in user can list templates. No admin restriction — the templates themselves are public knowledge in the repo, and a user who can't create a vault can still pre-shop templates harmlessly.

### Changed: `POST /api/v1/vaults`

The existing endpoint accepts `name` and `description` as query params. Add `template` as an optional third query param:

```python
@router.post("/vaults")
async def create_vault(
    name: str,
    description: str = "",
    template: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if template is not None and template not in template_registry.list_names():
        raise HTTPException(400, f"Unknown template: {template}")
    # … existing flow, passing template through to DocumentService.create_vault …
```

Body-vs-query is preserved as query — the existing client paths and curl invocations keep working. The validation step happens before the heavy DB transaction so an invalid template doesn't leave a half-created vault.

## Frontend — `vault-new.tsx` + api client

### `frontend/src/lib/api.ts` additions

```typescript
export interface VaultTemplateSummary {
  name: string;
  display_name: string;
  description: string;
  collection_count: number;
  collections: { path: string; name: string }[];
}

export const listVaultTemplates = () =>
  api<VaultTemplateSummary[]>("/vaults/templates");

// existing createVault becomes:
export const createVault = (
  name: string,
  description?: string,
  template?: string,
) => {
  const params = new URLSearchParams({ name, description: description || "" });
  if (template) params.set("template", template);
  return api<any>(`/vaults?${params}`, { method: "POST" });
};
```

The signature change is backwards-compatible at the call-site level: positional `template` is the new optional third arg. Existing callers that pass only `(name, description)` keep working.

### `frontend/src/pages/vault-new.tsx` changes

State:

```typescript
const [templates, setTemplates] = useState<VaultTemplateSummary[]>([]);
const [selectedTemplate, setSelectedTemplate] = useState<string>("");

useEffect(() => {
  listVaultTemplates().then(setTemplates).catch(() => setTemplates([]));
}, []);

const selectedSummary = useMemo(
  () => templates.find((t) => t.name === selectedTemplate) || null,
  [templates, selectedTemplate],
);
```

UI block (inserted between description and submit button, matching the existing form's visual rhythm):

```tsx
<div className="space-y-2">
  <label htmlFor="template-select" className="coord">TEMPLATE (OPTIONAL)</label>
  <select
    id="template-select"
    value={selectedTemplate}
    onChange={(e) => setSelectedTemplate(e.target.value)}
    className="w-full bg-surface border border-border px-3 py-2 text-sm font-mono"
  >
    <option value="">None — empty vault</option>
    {templates.map((t) => (
      <option key={t.name} value={t.name}>{t.display_name}</option>
    ))}
  </select>
  {selectedSummary && (
    <p className="coord">
      {selectedSummary.description}
      <br />
      Will create {selectedSummary.collection_count} collections:{" "}
      {selectedSummary.collections.map((c) => c.path).join(" · ")}
    </p>
  )}
</div>
```

Submit handler now passes `selectedTemplate || undefined`:

```typescript
await createVault(name, description, selectedTemplate || undefined);
```

Default selection is the empty option — creating a vault with no template stays the no-surprise path. A user has to actively opt in to a template.

### Visual treatment

The preview is a single `<p>` styled with the existing `coord` class (mono-uppercase muted), one or two lines max. No icons, no expanding cards — that's option C from the brainstorming and was deferred for YAGNI reasons. If the option count grows past ~12, revisit.

## Error handling

| Scenario | Behavior |
|---|---|
| Templates dir missing on backend | `list_summaries() == []`, REST GET returns `[]`, dropdown shows only "None", MCP enum is `[]` (template unselectable through schema-validating clients). Vault create with no template still works. |
| Malformed YAML in directory | Logged warning at startup, that file skipped, others continue. |
| YAML missing `collections` field | Same as malformed — warning + skip. |
| Client sends `template=foo` where foo is not in registry | REST: 400 "Unknown template: foo". MCP: schema validation rejects before the handler. |
| `GET /vaults/templates` called by unauthenticated client | 401 from existing `get_current_user` dependency. |
| Frontend `listVaultTemplates` fetch fails | Catch sets templates to `[]`; dropdown silently degrades to "None only". Form still submits. Log to console. |
| User selects template then it disappears from server (operator removed YAML + restarted between page load and submit) | REST POST returns 400 "Unknown template". Frontend surfaces this as the existing error toast on the form. |

The malformed-YAML and missing-dir cases are deliberately permissive: the goal is "ship vault create even when templates are broken" because the no-template path is the default and most-trafficked one anyway.

## Testing

### Backend

**New unit test**: `backend/tests/test_template_registry.py`

- Bootstrap with a temp directory of crafted YAML files using `monkeypatch.setattr(template_registry, "_TEMPLATES_DIR", tmp_path)` followed by `template_registry._scan()`. Cases:
  - `test_list_summaries_returns_sorted_by_display_name`
  - `test_get_returns_full_payload`
  - `test_malformed_yaml_skipped_not_raised`
  - `test_missing_collections_field_skipped`
  - `test_empty_dir_yields_empty_lists`
  - `test_list_names_matches_summaries_order`

The registry has no async, no DB, no network — these are fast pure-python tests with no need for Postgres.

**Extended e2e**: `backend/tests/test_vault_templates_e2e.sh` (new file)

- `GET /api/v1/vaults/templates` (authenticated) returns an array containing at least `"engineering"` with the expected `collection_count`.
- `GET /api/v1/vaults/templates` (no auth) returns 401.
- `POST /api/v1/vaults?name=X&template=engineering` succeeds, then `akb_browse(X)` shows the engineering collections (specs / decisions / runbooks / retrospectives / guides / plans).
- `POST /api/v1/vaults?name=Y&template=does-not-exist` returns 400.
- `POST /api/v1/vaults?name=Z` (no template) creates an empty vault — regression for the no-template default path.

**Touched-but-not-broken** check: existing `backend/tests/test_mcp_e2e.sh` template assertions (currently expecting `engineering`-template behavior) should keep passing without modification — the `_apply_template` logic is byte-for-byte equivalent, only the source of the dict moves.

### Frontend

**New component test**: `frontend/src/pages/__tests__/vault-new.test.tsx`

Mocks `@/lib/api` (`listVaultTemplates`, `createVault`).

- `test_dropdown_renders_none_plus_fetched_templates`
- `test_preview_shows_when_template_selected`
- `test_preview_hidden_for_none_option`
- `test_submit_passes_undefined_template_when_none_selected`
- `test_submit_passes_template_name_when_selected`
- `test_template_fetch_failure_yields_none_only_dropdown` (mock rejects → form still usable)

Follows the existing test patterns from the create-collection-dialog and document-view-toggle tests landed in the previous design.

## Files touched (summary)

**New (backend)**:
- `backend/app/services/template_registry.py`
- `backend/tests/test_template_registry.py`
- `backend/tests/test_vault_templates_e2e.sh`

**Changed (backend)**:
- `backend/mcp_server/tools.py` — enum becomes `template_registry.list_names()`
- `backend/app/services/document_service.py` — `_apply_template` reads from registry
- `backend/app/api/routes/vaults.py` — new `GET /vaults/templates`, `template` param on `POST /vaults`

**New (frontend)**:
- `frontend/src/pages/__tests__/vault-new.test.tsx`

**Changed (frontend)**:
- `frontend/src/lib/api.ts` — `listVaultTemplates`, `createVault` signature
- `frontend/src/pages/vault-new.tsx` — dropdown + preview + submit wiring
