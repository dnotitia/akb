# AKB Help Skill Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an LLM agent connecting to AKB via MCP discover vault-specific writing conventions in one tool call, with the convention stored as a normal AKB document the vault owner authors.

**Architecture:** 3 layers — (L1) MCP `initialize.instructions` injected into the host's system prompt every turn, (L2) `akb_help(topic="vault-skill", vault?)` as router, (L3) `overview/vault-skill.md` as the contract (a typed AKB doc with new `doc_type="skill"`). No new tool, no new endpoint, no filesystem write, no DB migration.

**Tech Stack:** Python 3.11 + FastAPI + Anthropic MCP SDK (`Server("akb")` over Streamable HTTP), PostgreSQL (`doc_type TEXT`, `metadata JSONB`), GitPython for vault storage. Tests are bash E2E suites under `backend/tests/`.

**Spec:** `docs/superpowers/specs/2026-05-14-akb-help-skill-bootstrap-design.md`

---

## File map (locked from spec)

| File | Status | Change |
|---|---|---|
| `backend/mcp_server/tools.py:73,214` | modify | Append `"skill"` to `akb_put` + `akb_search` type enums |
| `backend/mcp_server/tools.py` (akb_help section) | modify | Add optional `vault: string` parameter to `akb_help` tool schema |
| `backend/app/models/document.py:17` | modify | Append `"skill"` to doc-type Literal |
| `backend/app/services/metadata_worker.py:38` | modify | Append `"skill"` to `_DOC_TYPES` set |
| `backend/mcp_server/help.py` | modify | Add `vault-skill` topic body + `render_vault_skill_response(vault, fetch_fn)` helper |
| `backend/mcp_server/server.py:119-122` | modify | `_handle_help` reads `vault?` arg, dispatches to new helper |
| `backend/mcp_server/server.py:71` (and where `Server` is instantiated / handshake) | modify | Set `instructions` on `Server("akb")` initialization |
| `backend/mcp_server/http_app.py` | possibly modify | Confirm `create_initialization_options()` propagates instructions |
| `backend/app/services/document_service.py:create_vault` | modify | After `_apply_template`, seed `overview/vault-skill.md` (inside existing try block) |
| `backend/tests/test_skill_e2e.sh` | create | E2E suite covering B1–B4 |

---

## Shared constants (referenced from multiple tasks)

**Vault-skill template body** (used at vault create time and as a string constant in `help.py`):

```markdown
# {Vault} Vault Skill

> Edit this document to describe how agents should write into this vault.
> Until you do, it acts as the AKB-default template — agents fall back to
> general AKB conventions (browse before write, no inline secrets, etc.).

## Purpose

(Describe what this vault is for and what it is not for. One paragraph.)

## Document types

Use these types in akb_put. Skip the rest unless the body explicitly calls for them.

- note — lightweight record
- report — synthesized analysis
- decision — durable decision with rationale
- spec — technical or product specification
- plan — future work
- session — agent session record
- task — assignment
- reference — stable reference material

## Tag conventions

- topic:<slug> — concept grouping
- source:<system> — imported source family
- area:<slug> — organizational area

## Collections

(Optional — list collections and their write policy here. Vault owner can
free-form this section. Agents read it as context, not a hard schema.)

## Relation rules

- depends_on — one resource cannot be understood without another
- references — background citation
- derived_from — generated/curated work depends on source material

## Do not

- Inline secrets in bodies; use ${{secrets.X}} placeholders
- Edit auto-generated docs without checking provenance
```

**Initialize instructions body** (≤ ~120 tokens, English):

```text
AKB stores documents/tables/files in vaults. Before writing into a vault:
1. Call akb_help(topic="vault-skill", vault="<vault>") to read the owner's conventions for that vault.
2. If the vault has no vault-skill, follow the fallback guidance in that response.
3. Use akb_browse before akb_put on an unfamiliar collection.
4. Never inline secrets in document bodies — use ${{secrets.X}} placeholders.
5. Destructive tools (akb_delete_vault, akb_delete_collection) require explicit user confirmation.
```

---

## Task 1 — Add `"skill"` to doc-type enum (data model)

**Files:**
- Modify: `backend/mcp_server/tools.py` (lines 73 and 214)
- Modify: `backend/app/models/document.py` (line 17)
- Modify: `backend/app/services/metadata_worker.py` (line 38)

- [ ] **Step 1.1: Inspect current enum sites**

```bash
grep -n 'reference' backend/mcp_server/tools.py backend/app/models/document.py backend/app/services/metadata_worker.py
```
Expected: matches in all three files — two enums in `tools.py`, the `_DOC_TYPES` set in `metadata_worker.py`, and a comment string in `document.py:17` (the field itself is plain `str`, no `Literal` alias).

- [ ] **Step 1.2: Edit `backend/mcp_server/tools.py:73` — `akb_put` type enum**

Find:
```python
"enum": ["note", "report", "decision", "spec", "plan", "session", "task", "reference"],
```
Replace with:
```python
"enum": ["note", "report", "decision", "spec", "plan", "session", "task", "reference", "skill"],
```

- [ ] **Step 1.3: Edit `backend/mcp_server/tools.py:214` — `akb_search` type filter enum**

Same edit (append `"skill"`).

- [ ] **Step 1.4: Update the `document.py:17` comment**

The field is plain `str` (no `Literal` alias, no enforcement at this layer — validation happens at the MCP schema). Just keep the comment in sync as documentation. Find:
```python
    type: str = "note"  # note, report, decision, spec, plan, session, task, reference
```
Append `, skill`:
```python
    type: str = "note"  # note, report, decision, spec, plan, session, task, reference, skill
```

- [ ] **Step 1.5: Edit `backend/app/services/metadata_worker.py:38`**

Find:
```python
_DOC_TYPES = {"note", "report", "decision", "spec", "plan", "session", "task", "reference"}
```
Append `"skill"`:
```python
_DOC_TYPES = {"note", "report", "decision", "spec", "plan", "session", "task", "reference", "skill"}
```

- [ ] **Step 1.6: Smoke check — Python import + module load**

```bash
cd backend && python -c "from app.services.metadata_worker import _DOC_TYPES; print(sorted(_DOC_TYPES))"
```
Expected output includes `'skill'`.

```bash
cd backend && python -c "from mcp_server.tools import TOOLS; akb_put = next(t for t in TOOLS if t.name == 'akb_put'); print(akb_put.inputSchema['properties']['type']['enum'])"
```
Expected output ends with `'skill']`.

- [ ] **Step 1.7: Commit**

```bash
git add backend/mcp_server/tools.py backend/app/models/document.py backend/app/services/metadata_worker.py
git -c user.name='kwoo24' -c user.email='279600312+kwoo24-oss@users.noreply.github.com' commit -m "feat(akb): add \"skill\" doc-type enum value"
```

---

## Task 2 — `akb_help` router: `vault-skill` topic + `vault?` arg + missing notice

**Files:**
- Modify: `backend/mcp_server/help.py` (add `vault-skill` topic + helper function)
- Modify: `backend/mcp_server/tools.py` (akb_help tool schema — add `vault?: string`)
- Modify: `backend/mcp_server/server.py:119-122` (`_handle_help` to read `vault` arg)
- Test (unit): `backend/tests/test_help_skill_unit.py` (new)

### Step 2.1: Find the existing `akb_help` tool schema

```bash
grep -nA8 '"name": "akb_help"' backend/mcp_server/tools.py
```
Note the location (the `inputSchema.properties` block — should have only `topic: string` today).

- [ ] **Step 2.2: Extend the `akb_help` tool schema with `vault?: string`**

In `backend/mcp_server/tools.py`, find the `akb_help` Tool definition. The `inputSchema.properties` looks like:
```python
"properties": {
    "topic": {"type": "string", "description": "..."},
},
"required": [],
```

Add `vault`:
```python
"properties": {
    "topic": {"type": "string", "description": "..."},
    "vault": {
        "type": "string",
        "description": "Vault name. Required for topic='vault-skill' — returns that vault's skill doc body if it exists.",
    },
},
"required": [],
```

(Keep `required: []`; vault is optional.)

- [ ] **Step 2.3: Write failing unit test for help.py**

Create `backend/tests/test_help_skill_unit.py`:

```python
"""Unit tests for akb_help(topic='vault-skill', vault?) routing."""
import pytest
from unittest.mock import AsyncMock

from mcp_server.help import (
    VAULT_SKILL_TOPIC_BODY,
    VAULT_SKILL_PATH,
    render_vault_skill_response,
)


def test_topic_body_constant_exists():
    """The static topic body explains the convention without needing a vault."""
    assert "vault-skill" in VAULT_SKILL_TOPIC_BODY.lower()
    assert "akb_put" in VAULT_SKILL_TOPIC_BODY  # tells owner how to create one


def test_vault_skill_path_constant():
    """The doc path is fixed at overview/vault-skill.md."""
    assert VAULT_SKILL_PATH == "overview/vault-skill.md"


@pytest.mark.asyncio
async def test_render_with_vault_present():
    """When the doc exists, return body verbatim with source attribution."""
    async def fake_fetch(vault, doc_id):
        return {
            "content": "# My vault skill\n\nCustom rules here.",
            "commit": "abc1234",
            "updated_at": "2026-05-14T10:00:00Z",
        }

    out = await render_vault_skill_response(vault="my-vault", fetch_fn=fake_fetch)
    assert "# Vault skill for my-vault" in out
    assert "<!-- akb-skill-source -->" in out
    assert "Source: vault owner" in out
    assert "Custom rules here." in out


@pytest.mark.asyncio
async def test_render_with_vault_missing():
    """When the doc is missing, return notice + akb_put template + fallback rules."""
    async def fake_fetch(vault, doc_id):
        return None  # sentinel: doc not found

    out = await render_vault_skill_response(vault="empty-vault", fetch_fn=fake_fetch)
    assert "# Vault skill for empty-vault" in out
    assert "No `overview/vault-skill.md` found" in out
    assert "akb_put(" in out
    assert "akb_browse before writing" in out  # fallback bullet
    assert "${{secrets.X}}" in out  # secrets fallback


@pytest.mark.asyncio
async def test_render_without_vault_arg():
    """When no vault arg, returns just the static topic body."""
    out = await render_vault_skill_response(vault=None, fetch_fn=None)
    assert out == VAULT_SKILL_TOPIC_BODY
```

- [ ] **Step 2.4: Run test to verify it fails**

```bash
cd backend && python -m pytest tests/test_help_skill_unit.py -v
```
Expected: FAIL with `ImportError: cannot import name 'VAULT_SKILL_TOPIC_BODY' from 'mcp_server.help'`.

- [ ] **Step 2.5: Implement `help.py` additions**

Append to `backend/mcp_server/help.py`:

```python
# ── Vault skill router ────────────────────────────────────

VAULT_SKILL_PATH = "overview/vault-skill.md"

VAULT_SKILL_TOPIC_BODY = """# Vault skill

Each AKB vault can declare its writing conventions in a `vault-skill` document
at `overview/vault-skill.md`. Agents read it via:

    akb_help(topic="vault-skill", vault="<vault>")

The doc is a normal AKB document with `type="skill"`. The vault owner edits it
with regular AKB tools (akb_edit / akb_update). New vaults receive a starter
template at creation time.

If a vault has no vault-skill yet, the owner can create one with:

    akb_put(
      vault="<vault>",
      collection="overview",
      title="Vault Skill",
      type="skill",
      content="<see template in this topic body>",
    )

When no vault has a custom skill, agents follow these fallback rules:
- akb_browse before writing to learn the existing collection layout
- akb_search / akb_grep before writing to avoid duplicates
- Never inline secrets; use `${{secrets.X}}` placeholders
"""


_MISSING_FALLBACK = """No `overview/vault-skill.md` found in this vault.

The vault owner can create one with:

    akb_put(
      vault="{vault}",
      collection="overview",
      title="Vault Skill",
      type="skill",
      content="<see akb_help(topic='vault-skill') for the template>",
    )

Until then, follow general AKB conventions:
- akb_browse before writing to learn the existing collection layout
- akb_search / akb_grep before writing to avoid duplicates
- Never inline secrets; use `${{secrets.X}}` placeholders
"""


async def render_vault_skill_response(vault, fetch_fn):
    """Render the akb_help(topic='vault-skill', vault?) response.

    Args:
        vault: Vault name (str) or None.
        fetch_fn: async callable (vault, doc_id) → dict|None. The dict carries
            at least 'content'; optional 'commit', 'updated_at' used for the
            source-attribution header. None means doc not found.

    Returns:
        Markdown string.
    """
    if not vault:
        return VAULT_SKILL_TOPIC_BODY

    doc = await fetch_fn(vault, VAULT_SKILL_PATH)
    if doc is None:
        body = _MISSING_FALLBACK.format(vault=vault)
        return f"# Vault skill for {vault}\n\n{body}"

    version = doc.get("commit") or doc.get("updated_at") or "unknown"
    return (
        f"# Vault skill for {vault}\n"
        f"<!-- akb-skill-source -->\n\n"
        f"Source: vault owner ({VAULT_SKILL_PATH}, version {version})\n\n"
        f"{doc['content']}"
    )
```

Also add `"vault-skill"` to whichever lookup table `_resolve_help` consults (likely a dict that maps `topic → body`). If the topic isn't in the static dict it should fall through to `VAULT_SKILL_TOPIC_BODY`. Search for the existing dispatcher:

```bash
grep -nE "(HELP|_resolve_help)" backend/mcp_server/help.py
```
and add an entry mapping `"vault-skill": VAULT_SKILL_TOPIC_BODY` to the static dict (so `akb_help(topic="vault-skill")` without `vault` still works, returning the topic explanation).

- [ ] **Step 2.6: Run unit test to verify it passes**

```bash
cd backend && python -m pytest tests/test_help_skill_unit.py -v
```
Expected: PASS (4 tests).

- [ ] **Step 2.7: Wire `_handle_help` to accept `vault?` and dispatch**

In `backend/mcp_server/server.py` find `_handle_help` (around line 119):

```python
@_h("akb_help")
async def _handle_help(args: dict, uid: str, user: _MCPUser) -> dict:
    topic = args.get("topic")
    return {"help": _resolve_help(topic)}
```

Replace with:

```python
@_h("akb_help")
async def _handle_help(args: dict, uid: str, user: _MCPUser) -> dict:
    topic = args.get("topic")
    vault = args.get("vault")
    if topic == "vault-skill" and vault:
        from mcp_server.help import render_vault_skill_response, VAULT_SKILL_PATH
        async def _fetch(v, doc_id):
            row = await _find_doc(v, doc_id)
            if row is None:
                return None
            # NB: DB column is `current_commit`, not `commit_hash`.
            return {
                "content": row.get("content", ""),
                "commit": row.get("current_commit"),
                "updated_at": str(row.get("updated_at", "")),
            }
        return {"help": await render_vault_skill_response(vault, _fetch)}
    return {"help": _resolve_help(topic)}
```

(`_find_doc(vault_name, doc_ref)` is the existing helper at `server.py:55-67`; it returns the document row dict — note column name `current_commit`.)

- [ ] **Step 2.8: Run unit tests again — still pass**

```bash
cd backend && python -m pytest tests/test_help_skill_unit.py -v
```
Expected: PASS, 4/4.

- [ ] **Step 2.9: Quick smoke — start server and call help**

(Manual / optional — skip if the dev backend isn't trivially restartable.)

```bash
# In a separate shell, with backend running:
curl -s "$BACKEND_URL/api/v1/mcp/" -X POST \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"akb_help","arguments":{"topic":"vault-skill"}}}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["content"][0]["text"][:200])'
```
Expected: contains "# Vault skill" topic body.

- [ ] **Step 2.10: Commit**

```bash
git add backend/mcp_server/help.py backend/mcp_server/server.py backend/mcp_server/tools.py backend/tests/test_help_skill_unit.py
git -c user.name='kwoo24' -c user.email='279600312+kwoo24-oss@users.noreply.github.com' commit -m "feat(akb): akb_help vault-skill router + missing notice"
```

---

## Task 3 — MCP `initialize.instructions` (Layer 1)

**Files:**
- Modify: `backend/mcp_server/server.py` (Server constructor or initialization options)
- Modify: `backend/mcp_server/http_app.py` (verify create_initialization_options propagates)
- Test (unit): `backend/tests/test_mcp_init_unit.py` (new)

### Step 3.1: Confirm SDK supports `instructions` on Server

```bash
cd backend && python -c "
from mcp.server import Server
s = Server('test', instructions='hello')
opts = s.create_initialization_options()
print('instructions' in dir(opts) or 'instructions' in opts.__dict__ or opts.instructions)
"
```
Expected: prints `True` or the string `'hello'` — confirms SDK accepts `instructions`.

If the constructor doesn't accept `instructions`, fall back to setting `s.instructions = "..."` after construction. If that doesn't work either, escalate (this means the SDK version is too old; bumping is out of scope here).

- [ ] **Step 3.2: Write failing unit test**

Create `backend/tests/test_mcp_init_unit.py`:

```python
"""Unit test: Server('akb') initialization includes the bootstrap instructions."""
from mcp_server.server import server, INSTRUCTIONS


def test_instructions_constant_exists():
    assert isinstance(INSTRUCTIONS, str)
    assert len(INSTRUCTIONS) > 100  # not empty
    assert len(INSTRUCTIONS) < 2000  # not absurd
    assert "akb_help" in INSTRUCTIONS
    assert "vault-skill" in INSTRUCTIONS
    assert "secrets" in INSTRUCTIONS
    assert "akb_delete" in INSTRUCTIONS


def test_server_carries_instructions():
    """The Server('akb') instance is configured with the instructions."""
    # Two possible SDK shapes — accept either.
    direct = getattr(server, "instructions", None)
    if direct is not None:
        assert direct == INSTRUCTIONS
        return
    opts = server.create_initialization_options()
    got = getattr(opts, "instructions", None) or getattr(opts, "_instructions", None)
    assert got == INSTRUCTIONS
```

- [ ] **Step 3.3: Run test to verify it fails**

```bash
cd backend && python -m pytest tests/test_mcp_init_unit.py -v
```
Expected: FAIL — `ImportError: cannot import name 'INSTRUCTIONS'`.

- [ ] **Step 3.4: Add `INSTRUCTIONS` constant + pass to `Server`**

In `backend/mcp_server/server.py`, before `server = Server("akb")` (currently line 71), insert:

```python
INSTRUCTIONS = """AKB stores documents/tables/files in vaults. Before writing into a vault:
1. Call akb_help(topic="vault-skill", vault="<vault>") to read the owner's conventions for that vault.
2. If the vault has no vault-skill, follow the fallback guidance in that response.
3. Use akb_browse before akb_put on an unfamiliar collection.
4. Never inline secrets in document bodies — use ${{secrets.X}} placeholders.
5. Destructive tools (akb_delete_vault, akb_delete_collection) require explicit user confirmation.
"""

server = Server("akb", instructions=INSTRUCTIONS)
```

If the SDK version in use rejects `instructions=` as a kwarg (rare, but check with Step 3.1), use:

```python
server = Server("akb")
server.instructions = INSTRUCTIONS
```

- [ ] **Step 3.5: Run test to verify it passes**

```bash
cd backend && python -m pytest tests/test_mcp_init_unit.py -v
```
Expected: PASS (2 tests).

- [ ] **Step 3.6: Verify HTTP layer propagates `instructions`**

```bash
grep -n "create_initialization_options" backend/mcp_server/http_app.py
```
Expected: at least one call. If it's `server.create_initialization_options()` passed verbatim to the SDK run loop, no change needed (the SDK serializes instructions into the initialize response). If the file builds a custom initialization response, edit it to include `instructions=INSTRUCTIONS`.

- [ ] **Step 3.7: Run the full backend test suite (whatever exists today) — no regression**

```bash
cd backend && python -m pytest tests/ -v 2>&1 | tail -10
```
Expected: no new failures (pre-existing fails OK to ignore if they're already known broken).

- [ ] **Step 3.8: Commit**

```bash
git add backend/mcp_server/server.py backend/tests/test_mcp_init_unit.py
git -c user.name='kwoo24' -c user.email='279600312+kwoo24-oss@users.noreply.github.com' commit -m "feat(akb): mcp initialize instructions (vault-skill bootstrap gate)"
```

---

## Task 4 — Vault creation seeds `overview/vault-skill.md`

**Files:**
- Modify: `backend/app/services/document_service.py:create_vault` (around line 811)
- Test (unit): `backend/tests/test_vault_skill_seed_unit.py` (new)

### Step 4.1: Read the existing `create_vault` flow

```bash
sed -n '780,830p' backend/app/services/document_service.py
```

You'll see the `try` block that calls `self._apply_template(...)`. The seed must run **inside the same try** (so existing rollback covers it) and **after** `_apply_template` so collections exist.

- [ ] **Step 4.2: Write failing unit test**

Create `backend/tests/test_vault_skill_seed_unit.py`:

```python
"""Unit test: create_vault seeds overview/vault-skill.md with type=skill."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.document_service import DocumentService, VAULT_SKILL_SEED_TEMPLATE


def test_seed_template_constant_exists():
    assert "Vault Skill" in VAULT_SKILL_SEED_TEMPLATE
    assert "{vault}" in VAULT_SKILL_SEED_TEMPLATE  # substitutable
    assert "akb_put" not in VAULT_SKILL_SEED_TEMPLATE  # template is for owners to edit, not call-instructions


@pytest.mark.asyncio
async def test_seed_runs_after_template_apply():
    """create_vault writes overview/vault-skill.md after collections are seeded."""
    # NOTE: This is a thin behavioral test. The integration check happens in
    # the E2E suite (test_skill_e2e.sh). Here we just ensure the seed function
    # is called by inspecting the git commit log on a real but ephemeral vault
    # — easier to do this in E2E. Skip-mark this if the harness doesn't run it.
    pytest.skip("Covered by test_skill_e2e.sh; unit harness can't easily mock GitService.")
```

(The actual behavioral check is in the E2E suite — Task 5. This unit test exists to enforce the public constant.)

- [ ] **Step 4.3: Run test to verify it fails**

```bash
cd backend && python -m pytest tests/test_vault_skill_seed_unit.py -v
```
Expected: FAIL with `ImportError: cannot import name 'VAULT_SKILL_SEED_TEMPLATE'`.

- [ ] **Step 4.4: Add `VAULT_SKILL_SEED_TEMPLATE` and seed call**

In `backend/app/services/document_service.py`, near the top (after imports), add the template constant:

```python
VAULT_SKILL_SEED_TEMPLATE = """# {vault} Vault Skill

> Edit this document to describe how agents should write into this vault.
> Until you do, it acts as the AKB-default template — agents fall back to
> general AKB conventions (browse before write, no inline secrets, etc.).

## Purpose

(Describe what this vault is for and what it is not for. One paragraph.)

## Document types

Use these types in akb_put. Skip the rest unless the body explicitly calls for them.

- note — lightweight record
- report — synthesized analysis
- decision — durable decision with rationale
- spec — technical or product specification
- plan — future work
- session — agent session record
- task — assignment
- reference — stable reference material

## Tag conventions

- topic:<slug> — concept grouping
- source:<system> — imported source family
- area:<slug> — organizational area

## Collections

(Optional — list collections and their write policy here. Vault owner can
free-form this section. Agents read it as context, not a hard schema.)

## Relation rules

- depends_on — one resource cannot be understood without another
- references — background citation
- derived_from — generated/curated work depends on source material

## Do not

- Inline secrets in bodies; use ${{secrets.X}} placeholders
- Edit auto-generated docs without checking provenance
"""
```

Then in `create_vault`, after `_apply_template(...)` returns and **still inside the existing `try` block**, add:

```python
# Seed overview/vault-skill.md so every non-mirror vault carries a starter
# skill doc. The vault owner edits this later via akb_edit/akb_update; agents
# read it via akb_help(topic="vault-skill", vault=...).
#
# Unlike `_apply_template` which writes only git (the existing collection-level
# `_guide.md` files are not reachable via akb_get because no DB row is created),
# the vault-skill seed writes BOTH git AND a documents row so akb_get /
# akb_browse / akb_search can find it.
if not external_git:  # mirror vaults are read-only
    skill_body = VAULT_SKILL_SEED_TEMPLATE.format(vault=name)
    skill_path = "overview/vault-skill.md"

    # 1) git commit — capture commit hash for the document row
    commit_hash = await asyncio.to_thread(
        self.git.commit_file,
        vault_name=name,
        file_path=skill_path,
        content=skill_body,
        message="[init] Seed vault-skill.md",
    )

    # 2) Ensure overview collection exists (templates may not have created it)
    overview_coll_id, _, _, _, _ = await coll_repo.create_empty(vault_id, "overview")

    # 3) Insert document row — signature from DocumentRepository.create
    #    (vault_id, collection_id, path, title, doc_type, status, summary,
    #     domain, created_by, now, commit_hash, tags, metadata) → doc_id
    short_id = f"d-{uuid.uuid4().hex[:8]}"
    await doc_repo.create(
        vault_id=vault_id,
        collection_id=overview_coll_id,
        path=skill_path,
        title=f"{name} Vault Skill",
        doc_type="skill",
        status="active",
        summary=None,
        domain=None,
        created_by=str(owner_id),
        now=now,
        commit_hash=commit_hash,
        tags=["akb:skill"],
        metadata={"id": short_id},
    )
```

**Verify before editing:**

- `git.commit_file` return type — read the method around `git_service.py` to confirm it returns the commit hash as a string. If not, capture the hash from `self.git.get_head_commit(vault_name)` after the commit.
- `coll_repo.create_empty` signature — confirmed at `document_repo.py:340` returns `(collection_id, created, name, summary, doc_count)`. The first element is the UUID we need.
- `doc_repo.create` signature — confirmed at `document_repo.py:18-33`; this snippet matches.

**Mandatory prep edit:** The current `create_vault` unpacks `_repos()` as `vault_repo, _, coll_repo` — **discarding the document repo**. Before adding the snippet, change that unpack to `vault_repo, doc_repo, coll_repo` so `doc_repo` is bound. Without this, `doc_repo.create(...)` raises `NameError`.

**Other variables to set up before the seed block:**

- `now = datetime.now(timezone.utc)` (the `_apply_template` function sets its own local `now`; the outer `create_vault` doesn't). `datetime`/`timezone` are already imported at the module level.
- `owner_id` is already a parameter of `create_vault`, in scope.

- [ ] **Step 4.5: Run test to verify constant exists**

```bash
cd backend && python -m pytest tests/test_vault_skill_seed_unit.py -v
```
Expected: PASS for `test_seed_template_constant_exists`, SKIP for the behavioral one.

- [ ] **Step 4.6: Smoke — create a vault locally**

(Manual / skip if dev env is heavy)

With backend running:
```bash
curl -sk -X POST "$BACKEND_URL/api/v1/vaults" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -d '{"name":"skill-test-'$(date +%s)'","description":"test"}'
```
Then:
```bash
# Use the vault name from the response above
curl -sk "$BACKEND_URL/api/v1/documents/skill-test-XXX/overview/vault-skill.md" \
  -H "Authorization: Bearer $PAT" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("type"), d.get("path"), len(d.get("content","")))'
```
Expected: `skill overview/vault-skill.md <length>` where length > 500.

- [ ] **Step 4.7: Commit**

```bash
git add backend/app/services/document_service.py backend/tests/test_vault_skill_seed_unit.py
git -c user.name='kwoo24' -c user.email='279600312+kwoo24-oss@users.noreply.github.com' commit -m "feat(akb): seed overview/vault-skill.md on vault create"
```

---

## Task 5 — E2E suite

**Files:**
- Create: `backend/tests/test_skill_e2e.sh`

This is the integration-level check that proves all 4 prior tasks compose correctly against a running backend.

- [ ] **Step 5.1: Write the E2E script**

Create `backend/tests/test_skill_e2e.sh` (chmod +x):

```bash
#!/bin/bash
#
# AKB vault-skill bootstrap E2E
# Covers: doc_type='skill', akb_help router, missing notice, vault create seed,
# author workflow.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="skill-e2e-$(date +%s)"
EMPTY_VAULT="skill-e2e-empty-$(date +%s)"
E2E_USER="skill-user-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB Vault-Skill Bootstrap E2E          ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup: register user + get PAT ───────────────────────
echo "▸ 0. Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"skill-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

mcp() {
  local tool="$1"; local args="$2"
  curl -sk -X POST "$BASE_URL/api/v1/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}

# ── 1. Create a vault → vault-skill.md should be seeded ─────
echo "▸ 1. Vault create seeds overview/vault-skill.md"

mcp akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"e2e\"}" >/dev/null

GET_RESP=$(mcp akb_get "{\"vault\":\"$VAULT\",\"doc_id\":\"overview/vault-skill.md\"}")

echo "$GET_RESP" | grep -q '"type": *"skill"' \
  && pass "Seeded doc has type=skill" \
  || fail "T1.1" "type is not 'skill'; got: $(echo $GET_RESP | head -c 200)"

echo "$GET_RESP" | grep -q "$VAULT Vault Skill" \
  && pass "Seeded body contains vault name in title" \
  || fail "T1.2" "vault name not substituted in template title"

echo "$GET_RESP" | grep -q "Document types" \
  && pass "Seeded body includes Document types section" \
  || fail "T1.3" "missing Document types section"

# ── 2. akb_help(topic='vault-skill') static topic body ──────
echo "▸ 2. akb_help(topic='vault-skill') without vault arg"

H1=$(mcp akb_help '{"topic":"vault-skill"}')
echo "$H1" | grep -q "Vault skill" \
  && pass "Topic body returned" \
  || fail "T2.1" "topic body missing"

# Should NOT contain a 'Vault skill for <name>' header (that's only for the vault-specific render)
echo "$H1" | grep -q "Vault skill for" \
  && fail "T2.2" "static topic returned vault-specific header" \
  || pass "Static topic has no vault-specific header"

# ── 3. akb_help(topic='vault-skill', vault=<v>) returns body ─
echo "▸ 3. akb_help(topic='vault-skill', vault=<existing>)"

H2=$(mcp akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$VAULT\"}")
echo "$H2" | grep -q "# Vault skill for $VAULT" \
  && pass "Response header names the vault" \
  || fail "T3.1" "header missing"

echo "$H2" | grep -q "akb-skill-source" \
  && pass "Source-attribution marker present" \
  || fail "T3.2" "source marker missing"

echo "$H2" | grep -q "Source: vault owner" \
  && pass "Source line names the owner channel" \
  || fail "T3.3" "owner attribution missing"

echo "$H2" | grep -q "Document types" \
  && pass "Body content is included verbatim" \
  || fail "T3.4" "vault-skill.md body not embedded"

# ── 4. Missing vault-skill → notice + akb_put template ──────
echo "▸ 4. akb_help(topic='vault-skill', vault=<no-skill>)"

# Create a vault then DELETE its vault-skill.md so it's missing.
mcp akb_create_vault "{\"name\":\"$EMPTY_VAULT\",\"description\":\"e2e\"}" >/dev/null
mcp akb_delete "{\"vault\":\"$EMPTY_VAULT\",\"doc_id\":\"overview/vault-skill.md\"}" >/dev/null

H3=$(mcp akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$EMPTY_VAULT\"}")
echo "$H3" | grep -q "No \`overview/vault-skill.md\` found" \
  && pass "Missing notice rendered" \
  || fail "T4.1" "missing notice not shown"

echo "$H3" | grep -q 'akb_put(' \
  && pass "akb_put template included in missing notice" \
  || fail "T4.2" "akb_put template missing"

echo "$H3" | grep -q '\${{secrets.X}}' \
  && pass "Fallback rules included" \
  || fail "T4.3" "fallback rules missing"

# ── 5. Author workflow: edit vault-skill, re-fetch ──────────
echo "▸ 5. Owner can edit vault-skill, akb_help returns updated body"

NEW_BODY="# Custom Vault Skill\n\nMy custom rules: report only."
mcp akb_update "{\"vault\":\"$VAULT\",\"doc_id\":\"overview/vault-skill.md\",\"content\":\"$NEW_BODY\"}" >/dev/null

H4=$(mcp akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$VAULT\"}")
echo "$H4" | grep -q "My custom rules" \
  && pass "Edited body is returned" \
  || fail "T5.1" "edit did not propagate to akb_help"

GET2=$(mcp akb_get "{\"vault\":\"$VAULT\",\"doc_id\":\"overview/vault-skill.md\"}")
echo "$GET2" | grep -q '"type": *"skill"' \
  && pass "type=skill preserved across edit" \
  || fail "T5.2" "type changed after update"

# ── 6. doc_type='skill' is queryable ────────────────────────
echo "▸ 6. akb_search supports type='skill'"

S1=$(mcp akb_search "{\"vault\":\"$VAULT\",\"query\":\"vault\",\"type\":\"skill\"}")
echo "$S1" | grep -q "overview/vault-skill.md" \
  && pass "type=skill filter accepts and matches" \
  || fail "T6.1" "search with type=skill did not return the skill doc"

# ── Cleanup ──────────────────────────────────────────────────
mcp akb_delete_vault "{\"name\":\"$VAULT\"}" >/dev/null 2>&1
mcp akb_delete_vault "{\"name\":\"$EMPTY_VAULT\"}" >/dev/null 2>&1

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo "  Passed: $PASS    Failed: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo "  Errors:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
  exit 1
fi
```

- [ ] **Step 5.2: Make executable**

```bash
chmod +x backend/tests/test_skill_e2e.sh
```

- [ ] **Step 5.3: Verify it fails against current backend (sanity)**

```bash
AKB_URL=http://localhost:8000 bash backend/tests/test_skill_e2e.sh 2>&1 | tail -20
```
Expected: many FAILs (vault-skill seed not yet deployed, akb_help vault-arg not yet wired) — depending on which tasks have been deployed. After the implementation is deployed, all PASS.

- [ ] **Step 5.4: After deploy, run again — all PASS**

(Run after Tasks 1–4 are merged and deployed to the dev backend.)

```bash
AKB_URL=http://localhost:8000 bash backend/tests/test_skill_e2e.sh
```
Expected output ends with `Passed: ≥14   Failed: 0`.

- [ ] **Step 5.5: Commit**

```bash
git add backend/tests/test_skill_e2e.sh
git -c user.name='kwoo24' -c user.email='279600312+kwoo24-oss@users.noreply.github.com' commit -m "test(skill): e2e suite for vault-skill bootstrap"
```

---

## Final integration sweep

After Tasks 1–5 are merged, run the broader e2e suites against a fresh backend to catch regressions. The canonical suites for this project (per `CLAUDE.md`) are:

```bash
AKB_URL=http://localhost:8000 bash backend/tests/test_mcp_e2e.sh        # main 75 tests
AKB_URL=http://localhost:8000 bash backend/tests/test_defensive_e2e.sh  # 33 tests
AKB_URL=http://localhost:8000 bash backend/tests/test_skill_e2e.sh      # new
```

Expected: each suite ends `Passed: N    Failed: 0`. (Other suites — `test_edit_e2e.sh`, `test_collection_lifecycle_e2e.sh`, etc. — also worth running but the three above are the highest-value regression checks.)

If `test_mcp_e2e.sh` fails because a vault-create test now produces extra files (the seeded `vault-skill.md`), update that suite to tolerate the new file. Search for any hard-coded "expect N docs in new vault" assertions:

```bash
grep -nE "(docs.*count|doc_count.*==)" backend/tests/test_mcp_e2e.sh
```

If found, increment the expected count by 1 in the same commit.

---

## Out of scope (do not implement)

These were in the spec's "Out of scope" section. Resist the urge to slip them in:

- `{collection}/_skill.md` cascade
- Frontmatter `rules` parser + section-key merge
- `akb_link` between vault-skill and collection-skill
- Exemplar tagging (`tags=["akb:exemplar"]` with statistics)
- Dedicated `akb_skill_resolve` MCP tool
- Backfilling existing vaults with `vault-skill.md`

If any reviewer asks for one of these, point them at the spec's "Out of scope" section.

## Skills referenced

- @superpowers:test-driven-development — every step follows red → green → commit.
- @superpowers:subagent-driven-development — the recommended way to execute this plan.
