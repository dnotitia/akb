---
name: session-ingest
description: Ingest a coding session JSONL into AKB as structured notes — session report + parallel-drafted TIL / task / idea / decision sub-notes. Auto-discovers the current session's JSONL when no positional path is given (Claude / Codex). `--delegate {target}` fires the work to another target's CLI and exits.
disable-model-invocation: true
argument-hint: '[<jsonl_path>] --vault {name} [--lang {lang}] [--delegate {target}]'
allowed-tools: Bash(git *), Bash(claude *), Bash(codex *), Bash(find *), Bash(tr *), Bash(printf *), Bash(nohup *), Read, Edit, Glob, Grep, Agent, mcp__akb__akb_put, mcp__akb__akb_update, mcp__akb__akb_get, mcp__akb__akb_search, mcp__akb__akb_todo, mcp__akb__akb_todos, mcp__akb__akb_todo_update
---

# AKB Session Ingest

Multi-agent workflow that analyzes a session JSONL file and writes structured notes to an AKB vault.

**Workflow classification.** LLM-wiki **Ingest** workflow. **Cognitive-debt** layer — it captures the in-flight reasoning of a coding session (how the team arrived at an understanding, the decisions and dead ends), turning ephemeral working context into persistent compiled-truth notes (TIL / decision / idea / task) before it evaporates. The four drafter subagents share this classification.

## Prerequisites

- AKB MCP server is accessible.
- `--vault {name}` provided.

## Execution Flow

```text
Phase 0 — Resolve       parse args, resolve JSONL path (auto or explicit), maybe delegate & exit
Phase 1 — Prepare       detect duplicates, fetch open tasks
Phase 2 — Draft         read JSONL, identify commits/repo, draft session note
Phase 3 — Sub-Draft     launch til / task / idea / decision drafters; triage ideas
Phase 4 — Write         dedup ↔ finalize ↔ write session, sub-notes, resolve tasks
```

## Phase 0 — Resolve & Maybe Delegate

Resolve invocation arguments and the JSONL path. Optionally fire-and-forget to a different target's CLI and exit early. All Phase 1+ work assumes `$VAULT` and `$JSONL_PATH` are set and the JSONL file exists.

### 0-1. Parse Arguments

Parse positional `[jsonl_path]` (optional) and flags:

- `--vault {name}` (required) — hard-fail with `--vault {name} required.` if absent
- `--lang {language}` (optional) — preferred language for synthesized note body
- `--delegate {claude|codex}` (optional) — fire-and-forget to a different target's CLI and exit early

Store as `$JSONL_PATH`, `$VAULT`, `$LANG_FLAG`, `$DELEGATE`.

### 0-2. Reject Self-Delegate

The current target was baked in at build time. If `--delegate` matches it, abort to prevent infinite recursion.

```bash
SELF="claude"
if [ -n "$DELEGATE" ] && [ "$DELEGATE" = "$SELF" ]; then
  echo "Refusing $SELF -> $SELF delegate (would recurse)." >&2
  exit 2
fi
```

### 0-3. Resolve JSONL Path

If `$JSONL_PATH` was supplied positionally, use it. Otherwise discover the current session's JSONL automatically — discovery is target-specific.

**Claude target — `${CLAUDE_SESSION_ID}` template substitution + `$PWD` encoding.**

The Claude Code skill runtime substitutes `${CLAUDE_SESSION_ID}` with the current session's UUID at render time (see the substitution table at `code.claude.com/docs/en/skills.md`) — it is NOT a shell environment variable. Do not gate the path on an env-var check; the substitution has already happened by the time bash runs.

```bash
if [ -z "$JSONL_PATH" ]; then
  ENCODED=$(printf '%s' "$PWD" | tr '/.' '--')   # both / and . map to -
  JSONL_PATH="$HOME/.claude/projects/${ENCODED}/${CLAUDE_SESSION_ID}.jsonl"
fi
```

If the substitution did not happen (e.g., this skill was somehow invoked outside a Claude session), the resulting `$JSONL_PATH` will contain the literal text `${CLAUDE_SESSION_ID}` and the file-exists check below will catch it.

Verify the resolved path exists before proceeding:

```bash
if [ ! -f "$JSONL_PATH" ]; then
  echo "JSONL path not readable: $JSONL_PATH" >&2
  exit 2
fi
```

### 0-4. Delegate Fire-and-Forget (Optional)

If `$DELEGATE` is set, hand the ingest work to a different target and exit. The current session does no ingest work — the receiver runs Phase 1+ on the same JSONL. `claude` / `codex` spawn the target's CLI in the background.

Each target runs the receiver unattended while limiting blast radius if the JSONL contains injected content:

- `claude`: `--permission-mode acceptEdits` auto-approves file edits; Bash/MCP rely on the skill's `allowed-tools`.
- `codex`: `-s workspace-write -c approval_policy="never"` — confines file writes to the workspace AND skips approval pauses. `--skip-git-repo-check` lets the receiver start in a non-git pwd (delegate does not require the JSONL's project to be checked out). Avoid `--dangerously-bypass-approvals-and-sandbox`: that drops the sandbox entirely, letting prompt-injection in the JSONL escalate to arbitrary shell.

```bash
LOG="${TMPDIR:-/tmp}/session-ingest-delegate-$(date +%s)-$$.log"
ARGS="\"$JSONL_PATH\" --vault \"$VAULT\""
[ -n "$LANG_FLAG" ] && ARGS="$ARGS --lang \"$LANG_FLAG\""

case "$DELEGATE" in
  claude)
    nohup claude -p "/session-ingest $ARGS" --permission-mode acceptEdits \
      >"$LOG" 2>&1 &
    echo "Delegated to claude (pid $!, log $LOG)."
    ;;
  codex)
    # $session-ingest must NOT shell-expand. Single-quote the literal,
    # then concatenate the double-quoted $ARGS so the file path stays quoted.
    # --skip-git-repo-check: the receiver may run in a non-git pwd (delegate
    # does not require the JSONL's project to be checked out); codex exec
    # otherwise aborts before starting.
    PROMPT='$session-ingest '"$ARGS"
    nohup codex exec --skip-git-repo-check -s workspace-write -c 'approval_policy="never"' "$PROMPT" \
      >"$LOG" 2>&1 &
    echo "Delegated to codex (pid $!, log $LOG)."
    ;;
  "")
    : ;;
  *)
    echo "Unknown --delegate target: $DELEGATE (expected: claude, codex)" >&2
    exit 2
    ;;
esac

if [ -n "$DELEGATE" ]; then
  exit 0
fi
```

## Phase 1 — Prepare

### 1-1. Detect Duplicates

Before reading the full JSONL, check whether this session was already ingested.

#### Detect JSONL Schema

Read the first ~10 lines of the JSONL (`Read` with `limit: 10`). The opening line may be a noise event without identifying fields (CC sessions sometimes start with `permission-mode` or `file-history-snapshot`, neither of which carries `sessionId`); scan all 10 to find the first line that decisively identifies the schema:

- If any of those lines has a top-level `sessionId` field (e.g., `{"type": "worktree-state", "sessionId": "..."}` or `{"type": "user", "sessionId": "..."}`), the JSONL is **CC schema** (flat per-line events). Set `jsonl_schema = "cc"`.
- If any line has a top-level `payload` object containing `id` (e.g., `{"timestamp": "...", "type": "session_meta", "payload": {"id": "...", "cwd": "..."}}`), the JSONL is **Codex schema** (event-wrapped). Set `jsonl_schema = "codex"`.
- If none of the first 10 lines yields either signal, hard-fail with `"Unrecognized JSONL schema: $JSONL_PATH"` — do not guess.

Schema detection is independent of `runtime.target`: a Codex JSONL ingested from a Claude harness still uses Codex parsing, and vice versa.

#### Extract Session ID, Timestamps, and Source CWD

Reuse the first-10-lines window from schema detection and read the last few lines of the JSONL. Capture everything the head window carries in one pass — later phases reuse these values rather than re-scanning. Field lookup is schema-aware:

For `jsonl_schema == "cc"`:
- Store the first encountered `sessionId` field as `session_id`. (Lines without `sessionId` such as `file-history-snapshot` are skipped — they precede the first content event in some sessions.)
- If a `worktree-state` entry is present, store `worktreeSession.originalCwd` as `session_cwd`; otherwise leave `session_cwd` unset.

For `jsonl_schema == "codex"`:
- Find the entry with `type == "session_meta"` (typically the first line). Store `payload.id` as `session_id` and `payload.cwd` as `session_cwd`.

For both schemas:
- Last entry's top-level `timestamp` → `jsonl_last_timestamp`.

Store:
- `session_id` — the resolved session/thread id
- `jsonl_last_timestamp` — the ISO 8601 timestamp of the final entry
- `session_cwd` — the source session's working directory (or unset)

#### Search Vault for Existing Session

```text
mcp__akb__akb_search(
  query="{session_id}",
  vault="{vault_name}",
  collection="sessions",
  tags=["session:{session_id}"],
  type="session",
  limit=1
)
```

**No match** → `sync_mode = "new"`, proceed to Phase 2.

**Match found** → read the existing session note:

```text
mcp__akb__akb_get(vault="{vault_name}", doc_id="{matched_doc_id}")
→ existing_session_doc_id, existing_tags, existing_session_content
```

Extract the `last-synced:{timestamp}` tag value from the existing note's tags. Store as `last_synced_timestamp`. Preserve `existing_session_content` (the full markdown body) for Phase 2-3 and Phase 4-1.

#### Compare Timestamps

- `jsonl_last_timestamp <= last_synced_timestamp` → no new content since last sync → **stop**:
  `"Session {session_id} already synced up to {last_synced_timestamp}. No new content. Skipping."`
- `jsonl_last_timestamp > last_synced_timestamp` → proceed to the new-content assessment below.

#### Assess New Content

Read the JSONL entries with timestamps after `last_synced_timestamp`. Filter out noise. The filter list is schema-aware:

For `jsonl_schema == "cc"`:
- `type: "system"` messages
- `type: "permission-mode"` entries
- `type: "file-history-snapshot"` entries
- User messages containing only `/exit`, `/quit`, or session-end signals
- `isMeta: true` entries

For `jsonl_schema == "codex"`:
- `type: "turn_context"` entries (per-turn configuration)
- `type: "event_msg"` with `payload.type` in `{task_started, token_count}` (system markers and telemetry)
- `type: "response_item"` with `payload.type == "reasoning"` (model-internal thinking, analogous to CC thinking blocks)
- User messages (`type: "event_msg"` with `payload.type == "user_message"`) containing only `/exit`, `/quit`, or session-end signals
- `type: "session_meta"` (metadata-only, already consumed for session_id extraction)

After filtering, judge whether the remaining entries represent **meaningful work** — substantive user messages, assistant responses with real content, tool calls that changed files, or decisions made.

**Not meaningful** (e.g., resume then immediately exit, only system noise) → **stop**:
`"Session {session_id} has entries after {last_synced_timestamp} but no meaningful new work. Skipping."`

**Meaningful** → set `sync_mode = "update"`, store `existing_session_doc_id` and `last_synced_timestamp`, proceed to Phase 1-2.

### 1-2. Retrieve Open Tasks

Before reading the full JSONL, retrieve project-scoped open tasks from the vault so the session drafter can identify which tasks this session resolved.

**Parallel execution with Phase 1-1**: the project-name parse step below runs immediately (no MCP), and the `akb_search` for open tasks is independent of Phase 1-1's duplicate-detection search. Issue Phase 1-1's existing-session search and Phase 1-2's open-task search in a single parallel tool-use block. Act on Phase 1-2 results only after Phase 1-1 greenlights continuation (otherwise discard).

#### Derive Project Name

Project name is the basename of the source session's working directory. Use `session_cwd` (captured in Phase 1-1) when set — it is authoritative metadata from the JSONL, and cross-target delegate (e.g. Claude → Codex) puts the receiver in a different `pwd` than where the session originated, so `pwd` is not a reliable fallback.

- `session_cwd` set → `project_name` is its basename. (For CC worktree sessions this is `originalCwd`, the unworktreed project root, not the worktree name.)
- `session_cwd` unset (CC session with no `worktree-state` entry) → use the current working directory's basename. The CC JSONL parent directory uses a lossy encoding (`/` and `.` both map to `-`), so decoding it is unreliable and not used.

If derivation fails entirely, set `project_name` to the empty string. The downstream open-tasks query becomes a no-op (no `project:` tag match) — degraded but safe.

#### Query Open Tasks

```text
mcp__akb__akb_search(
  query="open task",
  vault="{vault_name}",
  collection="sessions/tasks",
  type="task",
  tags=["project:{project_name}"],
  limit=20
)
```

From the results, exclude any document whose tags contain `resolved`. Store the remaining results as `open_tasks` — a list of `{doc_id, title, summary}` for each match.

If no results or the query fails, set `open_tasks` to an empty list and continue.

## Phase 2 — Draft Session Note

The main agent acts as the session drafter. Read the JSONL file, extract session information, and produce both a session draft (navigator skeleton) and a session context for sub-agents. The session draft is finalized later in Phase 4-0 after sub-drafter results are reconciled.

### 2-1. Read Session JSONL

Read the JSONL file. It contains the complete conversation history: user messages, assistant responses, tool calls, and results. The field-name layout differs by `jsonl_schema` (set in Phase 1-1) — apply the analogous mapping when extracting content:

| Concept | CC schema | Codex schema |
|---|---|---|
| User message | `type: "user"` entry, `message.content[]` with `type: "text"` | `type: "event_msg"`, `payload.type: "user_message"` |
| Assistant text response | `type: "assistant"` entry, `message.content[]` with `type: "text"` | `type: "event_msg"`, `payload.type: "agent_message"` (and/or `response_item` with `payload.type: "message"`) |
| Tool call | `type: "assistant"` entry, `message.content[]` with `type: "tool_use"` (nested inside the assistant turn — NOT a top-level event type) | `type: "response_item"`, `payload.type: "function_call"` |
| Tool result | `type: "user"` entry, `message.content[]` with `type: "tool_result"` (nested inside a user-role turn that wraps the tool reply) | `type: "response_item"`, `payload.type: "function_call_output"` |

**Update mode (`sync_mode = "update"`):** Focus **JSONL analysis** on entries with timestamps **after** `last_synced_timestamp` — this is where the new information lives. The **session draft you produce must be a combined compiled truth**: read `existing_session_content` (retrieved in Phase 1-1) and integrate its Summary / Narrative / Artifacts / Knowledge Produced / Open Questions with the new work from the resumed portion. The written draft is the new canonical state of the session, not a delta.

**New mode (`sync_mode = "new"`):** Read the full JSONL as normal.

Focus on extracting:
- Work performed (main topics and tasks), as a chronological narrative
- Files and commits touched
- External URLs (articles, repos, docs) referenced in the session
- Unresolved questions (to be promoted in Phase 4-0 where possible)

Skip tool call/result noise — focus on user messages and assistant text responses. Reusable lessons and long-lived decisions will be extracted by the Phase 3 drafters; the session draft itself does not need to generalize.

### 2-2. Identify Session Commits and Repository

Scan the JSONL for git commit activity. Extract commit hashes to determine the range of changes made during the session.

If commits were found, run:

```bash
git log --oneline {earliest_commit_hash}^..{latest_commit_hash}
git diff --stat {earliest_commit_hash}^..{latest_commit_hash}
```

If no commits were found in the JSONL, skip these commands.

Also collect the repository URL:

```bash
git remote get-url origin
```

Store the result as `repo_url`. If the command fails (no remote configured), set `repo_url` to `null`.

### 2-3. Draft Session Note

Compose the session draft per `references/session-template.md` — that file carries the full draft template, section-omission rules, update-mode handling, and authoring guidelines. The session note is a navigator + narrative + timeline; deep knowledge (reusable lessons, long-lived decisions, follow-up tasks, creative ideas) belongs to the Phase 3 sub-notes and must not be restated here.

Determine the session's primary theme (debugging vs. feature build vs. refactor) and let it guide the emphasis in `## Narrative`. Number `## Open Questions` bullets `Q1`, `Q2`, … so Phase 3 sub-drafters can target them via `resolves_question: Q{n}`.

### 2-4. Identify Resolved Tasks

Compare the work performed in the session (from Phase 2-1) against the `open_tasks` list (from Phase 1-2).

A task is "resolved" if the session performed substantial work directly addressing the task's topic. Partial completion counts — any remaining work will be captured as a new task by the task-drafter.

For each resolved task, record:
- `doc_id` — from the open_tasks entry
- `title` — the task's title
- `path` — the task's path (for Knowledge Produced link)
- `reason` — brief explanation of how it was addressed (e.g., "Implemented the retry logic described in the task")

Store as `resolved_tasks` list. If no tasks were resolved, set to an empty list.

### 2-5. Construct Session Context for Sub-Agents

Build a lightweight session context as a roadmap for the Phase 3 drafter agents. This supplements the session draft and JSONL — it helps agents orient quickly.

```text
Session Context:
- Project: {project name}
- Repository: {repo_url from Phase 2-2, or "N/A"}
- Date: {YYYY-MM-DD from JSONL or today}
- Session JSONL: {$JSONL_PATH resolved in Phase 0}
- Work performed: {brief list of main topics/tasks}
- Committed changes: {from Phase 2-2 — commit messages and files changed, or "None"}
- Problems encountered (for possible troubleshooting TILs): {brief list}
- Long-lived decisions considered (for possible Decision notes): {brief list}
- Rejected alternatives (anti-pattern memory for Decisions): {brief list}
- Tools/technologies used: {notable APIs, libraries, frameworks}
- Vault name: {resolved vault_name from --vault}
- Collections: {plugin's default_collections mapping — includes decisions}
- Ingest mode: {new|update}
- Last ingested timestamp: {ISO timestamp from existing session note, or "N/A" if new}
- JSONL analysis start: {last_synced_timestamp if update, or "beginning" if new}
- Open tasks: [{doc_id}] {title} (for each task in open_tasks, or "None")
- Resolved tasks: [{doc_id}] {title} — {reason} (for each task in resolved_tasks, or "None")
- Open Questions (session draft): Q1: {text}, Q2: {text}, ... (each question available for sub-drafters to resolve via `resolves_question: Q{n}`)
```

Keep this concise — it is a roadmap, not a comprehensive record.

## Phase 3 — Parallel Sub-Drafting & Validation

### 3-1. Launch Drafter Agents

Launch 4 agents simultaneously in a single message. Each agent receives the session context, a **session headline** (frontmatter + `> **Summary:**` line + numbered `## Open Questions` list only — not the full Narrative), and the JSONL path. The JSONL is the primary source; drafters do not need Narrative prose.

```text
Agent(
    subagent_type="akb-sessions:til-drafter",
    description="TIL learning notes",
    prompt="[Session Context]\n\n## Session Headline\n{frontmatter + Summary + Open Questions}\n\nExtract learnings from the JSONL, validate against vault, and compose TIL notes."
)

Agent(
    subagent_type="akb-sessions:task-drafter",
    description="Follow-up task notes",
    prompt="[Session Context]\n\n## Session Headline\n{frontmatter + Summary + Open Questions}\n\nIdentify follow-up tasks from the JSONL and validate against vault."
)

Agent(
    subagent_type="akb-sessions:idea-drafter",
    description="Creative idea notes",
    prompt="[Session Context]\n\n## Session Headline\n{frontmatter + Summary + Open Questions}\n\nGenerate creative ideas inspired by the JSONL and validate against vault."
)

Agent(
    subagent_type="akb-sessions:decision-drafter",
    description="Long-lived decision notes",
    prompt="[Session Context]\n\n## Session Headline\n{frontmatter + Summary + Open Questions}\n\nExtract long-lived architectural/product decisions (ADR) from the JSONL, validate against vault, and compose Decision notes."
)
```


### Agent Summary

| Agent | Model | Purpose | Output |
|-------|-------|---------|--------|
| til-drafter | sonnet | Learnings + vault validation | 0–N learning notes with dispositions + `til_kind` tag |
| task-drafter | sonnet | Follow-up tasks + vault validation | 0–N task notes with dispositions |
| idea-drafter | opus | Creative ideas + vault validation | 0–N idea notes with dispositions |
| decision-drafter | sonnet | Long-lived decisions (ADR) + vault validation | 0–N decision notes with dispositions |

Each agent receives:
1. **Session Context** — lightweight roadmap (from Phase 2-5)
2. **Session Headline** — frontmatter + `> **Summary:**` line + numbered `## Open Questions`. The full Narrative is not sent — drafters read the JSONL directly for domain-specific detail.
3. **JSONL path** — primary source for each drafter's domain analysis
4. **Vault config** — `vault_name` and `collections` for duplicate checking

Each agent drafts notes, then validates them against the vault using `akb_search`. Drafts are returned with a `disposition` (create / append; `supersede` additionally for decisions), optional `resolves_question: Q{n}` when a draft addresses a specific session Open Question, and `related_to` doc IDs. Duplicates are reported as skipped and excluded from the output.

**idea-drafter specific.** Idea drafts additionally carry `impact` / `effort` / `confidence` enum assessments. The drafter silent-drops any idea it rates `impact: low` before returning (no report, no skipped block). The main agent re-triages the surviving drafts in Phase 3-2.

Agents that find nothing to write return "No {type} notes for this session."

### 3-2. Triage Idea Drafts

After the idea-drafter agent returns, the main agent filters the surviving idea drafts using their `impact` / `effort` / `confidence` frontmatter fields. The drafter already silent-dropped any idea it rated `impact: low` before returning; this phase is a second gate that catches cases the drafter missed and removes high-cost, low-conviction ideas.

**Scope.** Applies only to idea drafts. TIL / task / decision drafts pass through unchanged.

**Inputs.** Each idea draft from Phase 3-1 with `disposition ∈ {create, append}`.

**Automatic drop rules** — apply in order, drop on first match:

1. `impact == low` — drop. Safety net for the drafter's self-filter.
2. `effort == large` AND `confidence == low` — drop. High-cost, low-conviction ideas clutter the vault.

All remaining idea drafts pass through to Phase 4.

**No user prompt. No skipped block written. No vault write.** Dropped drafts are discarded in-memory; they do not feed Phase 4-0 or Phase 4-2. If a dropped idea set `resolves_question: Q{n}`, discard that mapping as well — the Q ID stays in the session's `## Open Questions`.

## Phase 4 — Write to AKB

Save all drafts with `create`, `append`, or (decisions only) `supersede` disposition directly — no user approval needed. Each drafter sets `status: active` in its `---DRAFT---` block; Phase 4-1 / 4-2 pass it through verbatim.

### 4-0. Dedup Drafts & Finalize Session Body

In-memory reconciliation between the session draft and the sub-note drafts before any AKB write. Single in-context pass — no extra LLM call, no MCP call, no per-pair loop. Inputs: session draft body, all sub-note drafts with `disposition ∈ {create, append, supersede}`, `resolved_tasks` list.

**Step A — Promote Open Questions.** For each sub-note draft with `resolves_question`: normalize the value (strip / upper / `^Q\d+$`; non-conforming → log warning + no-match), find the matching `Q{n}` bullet in the session's `## Open Questions`, and **remove that bullet entirely** — the answer surfaces in `## Knowledge Produced` (Step C). Multiple sub-notes can target the same `Q{n}`; the bullet is removed once. If no session question matches, log a warning and leave the sub-note unchanged.

**Step B — Cross-Type Content Overlap Detection.** For each sub-note, extract its headline sentence — TIL: `> **Key Insight:** …`; Task: `> **Next Action:** …`; Idea: `> **Inspiration:** …` plus Proposal first sentence; Decision: `> **Context:** …` plus Decision first sentence. Scan the session Narrative and Artifacts prose for sentences that match a headline. A sentence matches when at least **two** of these hold: (1) core noun phrases overlap, (2) assertion direction is the same (not negated / qualified differently), (3) reasoning or evidence cited is the same. One criterion alone is not enough — preserve the Narrative sentence if in doubt. On a match, remove the redundant sentence or replace it with a brief pointer ("…the lesson captured in Knowledge Produced"); do not rewrite surrounding prose beyond what grammatical flow needs. Overlap resolution **always favors sub-notes** — never edit a sub-note body here.

**Step C — Finalize Knowledge Produced.** Build the block from deduplicated sub-notes plus `resolved_tasks`, in the order TIL → Idea → Task → Decision → Resolved:

```markdown
## Knowledge Produced

- 🧠 TIL: [{title}](sessions%2Flearnings%2F{doc_path}.md) — {one-line hook from Key Insight}
- 💡 Idea: [{title}](sessions%2Fideas%2F{doc_path}.md) — {one-line hook from Inspiration}
- ✅ Task: [{title}](sessions%2Ftasks%2F{doc_path}.md) — {one-line hook from Next Action}
- 📐 Decision: [{title}](sessions%2Fdecisions%2F{doc_path}.md) — {one-line hook from Decision first sentence}
- ♻️ Resolved: [{title}](sessions%2Ftasks%2F{path}.md) — {reason from resolved_tasks}
```

For newly-drafted sub-notes the link's `doc_path` is substituted at render time from each Phase 4-2 put response. For resolved tasks the link uses the existing task's `path` captured in Phase 2-4. See `references/note-templates.md` → "Cross-Note Links" for why this hybrid (body link + `depends_on` frontmatter) is used.

**Step D — Final omissions.** Omit `## Knowledge Produced` entirely when no sub-notes were produced and no tasks resolved. Omit `## Open Questions` if Step A left zero unresolved bullets. The resulting body is what Phase 4-1 writes — do not mutate further.

### 4-1. Write Session Note

The session note is written first because sub-notes reference it via `depends_on`. Sub-notes always use the full URI `akb://{vault_name}/doc/{session_doc_path}` (always-URI cross-ref convention; future-proof against vault tier splits).

**New mode (`sync_mode = "new"`).** Extract metadata from the draft's `---DRAFT---` block and `akb_put(vault="{vault_name}", ...)` with `type="session"`, `collection={draft.collection}`, `status={draft.status}`, `tags=["session", "claude-code", "session", "session:{session_id}", "project:{project_name}", "last-synced:{jsonl_last_timestamp}", ...topic_tags]`, and the finalized body from Phase 4-0. Capture `session_doc_id` and `session_doc_path`.

**Update mode (`sync_mode = "update"`).** The session note already exists. Phase 2-3 + 4-0 produced a full compiled-truth replacement plus exactly one new timeline entry. Apply the **timeline merge procedure — Mode A (full-body merge)** (see appendix) against `existing_session_content`. Build the updated tag list (replace the old `last-synced:{old_timestamp}` with `last-synced:{jsonl_last_timestamp}`; preserve all other tags; add any new topic tags). Then `akb_update(vault="{vault_name}", doc_id=existing_session_doc_id, content={merged}, tags={updated}, summary={draft.summary}, message="Update from resumed session: {brief description}")`. Assemble `session_uri = akb://{vault_name}/doc/{existing_session_doc_path}` for sub-notes' `depends_on`.

### 4-2. Write Sub-Notes (Learning, Task, Idea, Decision)

**Issue all sub-note `akb_put` calls in a single parallel tool-use block.** Sub-notes are independent of each other; only the session note's order matters (via `depends_on`).

For each sub-note, extract metadata from the draft and call `akb_put(vault="{vault_name}", ...)` with `collection={draft.collection}`, `type={draft.type}`, `status={draft.status}`, `tags=["session", "claude-code", ...draft_tags]`, `depends_on=["akb://{vault_name}/doc/{session_doc_path}"]`, `related_to=[...draft.related_to]` (full URIs as emitted by drafters). Collection per type: TIL → `sessions/learnings`, Task → `sessions/tasks`, Idea → `sessions/ideas`, Decision → `sessions/decisions`.

**Idea notes** — append four namespaced tags to `draft_tags` before put: `category:{draft.category}`, `impact:{draft.impact}`, `effort:{draft.effort}`, `confidence:{draft.confidence}`. Drop duplicates (the drafter may have already emitted some as tags). These four tags are the durable carriers for the external maintenance layer's consolidation synthesis and staleness checks.

**Task notes** — after the task document is created, also create a lightweight todo via `akb_todo(vault="{vault_name}", title=draft.title, priority={P0→urgent, P1→high, P2→normal, P3→low}, ref_doc=task_doc_id, note=draft.summary)`.

**`append` disposition** — the drafter has already merged compiled truth + one new timeline entry. Read the existing document with `akb_get(vault="{vault_name}", doc_id=draft.append_target)`, apply timeline merge **Mode A** against `draft.body`, and `akb_update(vault="{vault_name}", doc_id=draft.append_target, content={merged}, tags=[...existing, ...new from draft], summary=draft.summary, message="Appended from session ingest: {brief description}")`.

**`supersede` disposition (decisions only)** — write the new decision via `akb_put(vault="{vault_name}", ..., tags=[..., "supersedes:{old_doc_id}"])` (tags use bare doc_id; the `:` already binds `key:value`). Capture `new_doc_id` and `new_doc_path`; compose `new_doc_ref = akb://{vault_name}/doc/{new_doc_path}`. Then read the old decision via `akb_get(vault="{vault_name}", doc_id=draft.supersedes)`, apply timeline merge **Mode B** with `new_entry = "- **{today}** | Superseded by {new_title} ({new_doc_ref}). {reason}. [Source: session:{session_id}, {today}]"`, and `akb_update(vault="{vault_name}", doc_id=draft.supersedes, content={merged}, tags=[...old, "superseded-by:{new_doc_id}"], status="superseded", message="Superseded by {new_doc_ref} in session ingest")`. Setting `status="superseded"` removes the old decision from the external maintenance layer's active-document pools.

### 4-3. Resolve Completed Tasks

If `resolved_tasks` is non-empty, fetch open todos once: `akb_todos(vault="{vault_name}", status="open") → open_todos`. For each resolved task:

1. Scan `open_todos` in memory for a todo whose `ref_doc` matches the task's `doc_id`. If found, `akb_todo_update(vault="{vault_name}", todo_id={matched}, status="done", note="Resolved in session {session_doc_id}")`.
2. Read the task document with `akb_get(vault="{vault_name}", doc_id={task_doc_id})`, then compose the resolution timeline entry `- **{today YYYY-MM-DD}** | Task resolved in session:{session_id}. {reason}. [Source: session:{session_id}, {today YYYY-MM-DD}]` and apply timeline merge **Mode B** against `task_content` (compiled-truth zone preserved as-is).
3. `akb_update(vault="{vault_name}", doc_id={task_doc_id}, content={merged}, tags=[...existing, "resolved"], status="archived", message="Resolved in session {session_doc_id}")`.

If no matching todo is found, still archive the task document. If the up-front `akb_todos` fails, skip all todo updates this ingest but archive the task documents anyway.

## Error Handling

If a drafter agent (Phase 3) fails or returns an error:
- Continue with results from the remaining agents
- Log the failure in the session summary
- Do not block the pipeline for a single agent failure

If an `akb_put` fails partway through Phase 4:
- Continue writing the remaining notes from this invocation
- The partial result is queryable under the `"session:{session_id}"` tag

If Phase 4-0 Step A finds a `resolves_question` that does not match any session Open Question ID:
- Log a warning naming the sub-note and the invalid Q ID
- Leave the sub-note draft as-is, write without session mutation for that question

## References

- `references/session-template.md` — session draft template, section-omission rules, update-mode handling, authoring guidelines.
- `references/note-templates.md` — sub-note templates (TIL / task / idea / decision).
- Agent prompts in `agents/` — full drafter-agent instructions.

## Appendix: Timeline Merge Procedure

Shared procedure for every skill that writes back to an existing note. Two modes — every caller picks **one** explicitly.

**Shared helper.** `split_body(content)` — locate the first standalone `---` line that is followed (eventually) by a `## Timeline` heading. Return `(body_above, timeline_entries)`. If no `## Timeline` heading exists, return `(content, [])`.

### Mode A: Full-body merge

Used when the caller has produced a **rewritten compiled-truth body** (the new canonical state) plus exactly **one** new timeline entry. Callers: session-ingest Phase 4-2 (session update), session-ingest Phase 4-3 (sub-note append), and ingest-jira (issue upsert).

Inputs: `draft_body` (full new body — includes `---` + `## Timeline` + one new bullet), `existing_content` (current doc).

1. `(draft_above, draft_entries) = split_body(draft_body)`. `draft_entries` must contain exactly one bullet. If it contains zero, treat as drafter error → fall back to `akb_update` with `draft_body` unchanged and log a warning.
2. `(_, existing_entries) = split_body(existing_content)`.
3. Compose merged content:
   ```
   {draft_above}

   ---

   ## Timeline

   {draft_entries[0]}
   {existing_entries}
   ```
4. Newest entry first. Never edit or remove past entries.

### Mode B: Entry-only append

Used when the caller produced **only a new timeline bullet** and wants the existing compiled-truth zone preserved verbatim. Callers: session-ingest Phase 4-4 (task resolve) and the decision supersede path, ingest-doc re-ingest, and ingest-confluence re-ingest.

Inputs: `new_entry` (a single bullet line, possibly with sub-bullets), `existing_content`.

1. `(existing_above, existing_entries) = split_body(existing_content)`.
2. If `existing_content` had no `## Timeline` section, append `"\n\n---\n\n## Timeline\n\n"` to `existing_above` before proceeding.
3. Compose merged content:
   ```
   {existing_above}

   ---

   ## Timeline

   {new_entry}
   {existing_entries}
   ```
4. **Never rewrite the compiled-truth zone in this mode.** If the caller needs to change compiled truth, it must use Mode A instead.
