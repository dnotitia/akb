# Claude Guide For AKB

Read `AGENTS.md` first. This file adds Claude-specific behavior for the AKB
repo and the project-management workflow around it.

## Mission

AKB is moving toward production service level. Your job is not only to modify
code, but to keep product intent, design decisions, review output, and AKB vault
memory aligned.

## Default Work Loop

1. Inspect the relevant code and docs before editing.
2. Decide whether the task needs a PRD/design item:
   - Product behavior, roadmap, acceptance criteria, operations, launch, or
     user-facing workflow: use `docs/prd/`.
   - Architecture, API contracts, storage, indexing, deployment, auth, or
     cross-component changes: use `docs/design/`.
3. Keep docs as item folders with `README.md`, `rounds/`, and `feedback/`.
4. Implement the smallest coherent change that respects existing boundaries.
5. Run the relevant tests or explain why they were not run.
6. Mirror durable decisions and review summaries into the `akb` vault when the
   work affects project direction. Do not use `akb-test`.

## PM Collaboration

The user is acting as project manager. Be explicit about:

- What decision is being made.
- Which acceptance criteria are still missing.
- Which release risks remain.
- Whether a doc should stay WIP/proposal or be promoted.
- Which AKB vault document should become the durable source of truth.

Do not bury PM questions inside long implementation detail. If one decision
blocks safe progress, ask it directly.

## Docs State Machine

PRD item paths:

- `docs/prd/backlog/<yyyy-mm-dd-slug>/`
- `docs/prd/wip/<yyyy-mm-dd-slug>/`
- `docs/prd/applied/<yyyy-mm-dd-slug>/`
- `docs/prd/denied/<yyyy-mm-dd-slug>/`

Design item paths:

- `docs/design/proposal/<yyyy-mm-dd-slug>/`
- `docs/design/accepted/<yyyy-mm-dd-slug>/`
- `docs/design/denied/<yyyy-mm-dd-slug>/`

Each item directory contains:

- `README.md`: canonical doc with frontmatter.
- `rounds/`: critique rounds, alternatives, revision notes.
- `feedback/`: PM feedback, Claude/Codex reviews, external review notes.

When moving an item, update frontmatter `status`, `stage`, and `updated`.

## Architecture Reminders

- Backend business logic belongs in the FastAPI/MCP backend.
- Local filesystem access belongs in the Node stdio proxy.
- Do not add backend behavior for `akb_put_file`, `akb_get_file`,
  `akb_delete_file`, or the `file` parameter on `akb_put` / `akb_update`.
- PostgreSQL + Git are source of truth. Vector stores are derived indexes.
- `document_repo.find_by_ref()` is the central path for doc ID resolution.
- `GitService` writes go through the persistent worktree and per-vault lock.

## Hook Contract

`.claude/settings.json` enables project hooks implemented by
`.claude/hooks/akb_pm_hook.py`.

The hooks are intentionally conversational:

- Session and prompt hooks inject the PM/doc/AKB context.
- Pre-tool hooks block clearly dangerous commands and secret edits.
- Pre-tool hooks ask for confirmation before deployment, npm publish, or git
  push operations.
- Stop hooks remind you to report tests, docs state, review output, and AKB
  sync status.

If a hook warns about a docs lifecycle issue, update the item structure instead
of bypassing the hook.

## Review Expectations

When asked to review, lead with findings ordered by severity and cite exact
files/lines. Include open questions and test gaps after findings. If no issue is
found, say that directly and still note residual risk.

For production-readiness reviews, check at least:

- Auth/access control and publication exposure.
- Crash recovery and async worker retry behavior.
- Vector-store lag and degraded search behavior.
- Git write serialization and worktree consistency.
- MCP proxy/backend boundary violations.
- Test coverage for the changed contract.

## Useful Commands

- `bash scripts/check.sh`
- `cd backend && pytest tests/ -k 'not _e2e' -v --tb=short`
- `bash backend/tests/test_mcp_e2e.sh`
- `bash backend/tests/test_security_edge_e2e.sh`
- `python3 .claude/hooks/akb_pm_hook.py --self-test`
- `bash scripts/claude-review.sh`

Always check whether command prerequisites are installed before turning a tool
failure into a product conclusion.
