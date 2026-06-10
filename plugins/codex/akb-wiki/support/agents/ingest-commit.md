---
name: ingest-commit
description: Record a single git commit as an immutable git-commit document in an AKB vault — mechanical git metadata plus a faithful restatement of what the diff changed.
model: gpt-5.4-mini
reasoning_effort: medium
---

# AKB Git Commit Ingest

Record a **single git commit** as an immutable `git-commit` document in the target AKB vault — the commit-level episodic capture path of `akb-wiki`.

Scope boundaries:

- **One commit per invocation.** No loops, cursor, or range — backfill / sweeping / batching live in an orchestration layer.
- **No filtering policy.** Merge commits, bot authors, trivial commits are recorded as-is (marked in frontmatter); the caller decides what to skip.
- **No semantic compilation.** PR / release pages are produced by sibling skills; consolidation pages by the external maintenance layer.
- **Write-once.** Commit docs are never mutated here. The external maintenance layer may append timeline entries for consolidation membership; PR / release linkage lives on the parent's `depends_on` graph edge — no commit-side backlink frontmatter fields.

## Inputs (provided by the /akb-ingest router)

```text
/ingest-commit <sha> --vault {vault_name} [--repo {path}]
```

- `<sha>` (required, positional) — full or short commit SHA to ingest.
- `--vault` (required) — target AKB vault.
- `--repo` (optional) — path to the git repository. Defaults to the current working directory.

## Prerequisites

- AKB MCP server is accessible.
- `--vault {name}` provided.
- `git` is available on PATH.

## Workflow

### Step 1 — Resolve inputs

Common resolution every git ingest subagent performs before its own skill-specific validation.

- Resolve `repo_path` from `--repo` or `pwd`. If `git -C {repo_path} rev-parse --git-dir` fails, fail with `Not a git repository: {repo_path}`.
- Resolve `vault_name` from `--vault`. Hard-fail if missing: `--vault {name} required.`
- Resolve `repo_name` as the last path segment of `repo_path` (normalized). All git ingest subagents use this same rule so they agree on the `repo` axis.

Then validate the SHA: `git -C {repo_path} rev-parse --verify {sha}^{commit}`. Fail with `SHA not found in repository: {sha}` on error. Capture the resolved full SHA as `full_sha`. (This single `rev-parse` subsumes the repo-directory check above, so no separate `--git-dir` invocation is needed after it succeeds.)

### Step 2 — Dedup check

`akb_search(query={full_sha}, vault={vault_name}, collection=git-commits, type=reference, tags=["git", "kind:commit", "project:{repo_name}"], limit=5)`. Among hits, find the one whose frontmatter `sha` equals `full_sha`. If found, return without writing:

```markdown
## ingest-commit: exists
- sha: {full_sha}
- doc_id: {existing_doc_id}
- vault: {vault_name}
```

### Step 3 — Collect git data in a single pass

```bash
git -C {repo_path} show \
  --format='%H%x00%h%x00%P%x00%aN%x00%ae%x00%aI%x00%cN%x00%ce%x00%cI%x00%s%x00%b' \
  --stat --unified=0 {full_sha}
```

`%aN` / `%cN` apply `.mailmap` automatically — no separate `git log` needed. Parse three zones:

1. **Metadata line** (null-separated): `sha`, `short_sha`, `parents` (space-separated), `author_name` (mailmap-applied), `author_email` (raw), `authored_at`, `committer_name` (mailmap-applied), `committer_email` (raw), `committed_at`, `message_subject`, `message_body`.
2. **Stat block**: extract `{files, additions, deletions}` from the summary line (e.g. `3 files changed, 47 insertions(+), 12 deletions(-)`); collect per-file stat lines for the body.
3. **Diff block**: cap at `500` lines (set `truncated: true`); detect `Binary files ... differ` markers and record their paths.

Derive `is_merge = (len(parents) > 1)`.

### Step 4 — Resolve canonical author

`author_name` from Step 3 is mailmap-normalized. Override with `data/plugins/akb-wiki.yaml` `author_aliases` keyed on the **raw** `author_email` if a non-empty entry exists; otherwise keep Step 3's value. `author_email` is never rewritten — preserved as the alias-lookup key.

### Step 5 — Faithful restatement

Produce two artifacts from the Step 3 diff. Use no context beyond commit message + diff + file list — never consult sibling commits, PR metadata, issue trackers, or external state.

**5a. `summary` (frontmatter, 1 line)** — single-line factual description of what the diff changed. Imperative/neutral tone (not evaluative); describes **structure** (renames, adds, removes, moves), not **intent**; derived from the diff (when the message subject is vague — `fix stuff`, `wip`, `asdf` — or misleading, reconstruct from the diff). Omit author names, issue numbers, dates.

Acceptable: `Rename AuthService to AuthManager across src/auth and tests/auth`, `Add SSOProvider class with login/logout/validate_token in src/auth/sso.py`. Forbidden (interpretation leaking in): `Improve auth module for better SSO support` (evaluative), `Part of the Q2 security refactor` (cross-commit context), `Fix the bug where users couldn't log in` (intent).

**5b. `Changes (faithful)` block (body, bullet list)** — concrete bullets, each naming **what** and **where**. Group by file or change kind; spell out renames (`Rename X → Y`); name new symbols/sections and their locations on adds; name what was removed and its former location; describe dimensions on bulk edits (`Bump 4 dependency versions in package.json`); one line per binary (`Binary change in {path}`). On truncation, end with `Diff truncated at 500 lines; remaining changes not enumerated.`

**5c. `diff_scope` (frontmatter, categorical list)** — rule-based labels from diff shape (not message). Pick any: `add` (net new file), `remove`, `rename`, `modify` (in-place edit), `config` (only top-level config/CI: `*.yaml`, `*.toml`, `Makefile`, `.github/**`), `docs` (only `*.md`, `docs/**`, comment-only), `test` (only `tests/**`, `spec/**`, `*_test.*`), `binary`. Multiple labels co-occur (e.g. `[modify, docs]`).

**5d. Conventional commit parse** — match `message_subject` against `<type>(<scope>): <desc>` or `<type>: <desc>` with `type ∈ feat|fix|refactor|docs|test|chore|ci|build|perf|style`. Both `conv_type` / `conv_scope` are `null` on no match.

**5e. Body-section scope tag** — `scope_tag = conv_scope or top_level_paths[0] or null`. Emitted as `scope:{scope_tag}` in Step 6 when non-null; the external maintenance layer consumes it directly without reading a `paths` frontmatter field.

### Step 6 — Assemble the page

Frontmatter:

```yaml
type: reference
status: active

repo: "{repo_name}"                # last path component of repo_path (normalized)
project: "{repo_name}"             # mirrors repo; `repo` is the source-system identifier, `project` is the project-rollup namespace
sha: "{full_sha}"
short_sha: "{short_sha}"
parents: [{parent_sha}, ...]

committer: "{canonical_committer}"
authored_at: "{authored_at_iso}"
committed_at: "{committed_at_iso}"

stats: { files: N, additions: N, deletions: N }
is_merge: {true|false}
truncated: {true|false}

conv_type: "{feat|fix|...|null}"
conv_scope: "{scope|null}"
diff_scope: [{rule_based_labels}]

summary: "{faithful_1_line}"
message_subject: "{commit_message_first_line}"
tags: ["git", "project:{repo_name}", "kind:commit", "scope:{scope_tag}", "type:{conv_type_if_any}", "author:{canonical_author}"]

related_to: []
```

`scope:{scope_tag}` and `type:{conv_type}` are omitted when Step 5e/5d produced `null`. `kind:commit` is the discriminator the external maintenance layer reads when grouping per-source summary rows. Per-commit `paths` array is not frontmatter — its only consumer (the external maintenance layer's consolidation) reads `scope:{val}` off tags. `author` / `author_email` likewise live only on tags as `author:{canonical_author}`. The `type:{conv_type}` namespace collides with `/ingest-jira`'s `type:{issue_type}` deliberately — `kind:*` (`kind:commit` vs `kind:atlassian-issue`) disambiguates.

Body:

```markdown
# {summary}

## Changes (faithful)
- {bullet 1}
- {bullet 2}
...

## Author's message
> {message_subject}

{message_body if non-empty, else omit this paragraph}

## Stats
+{additions} / -{deletions} across {files} files

### Paths
- {full_path_a}
- {full_path_b}
...

## Source

- Author: {canonical_author} <{raw_author_email}>
```

Notes on assembly:

- `## Source` keeps the canonical author + raw email visible for human readers — their frontmatter carriers don't survive the AKB MCP whitelist. The YAML example above lists `sha` / `short_sha` / `parents` / dates / conv-type / diff-scope / stats / flags for documentation only; downstream skills read those axes from `tags` instead (see `_fetch_commit_docs.md.j2`).
- `### Paths` is the full file list; the external maintenance layer's body-section axis is the `scope:{val}` tag, not a separate frontmatter array.
- `tags` drops null-valued entries (e.g. `conv_type` / `scope:` when absent).
- Never include the full unified diff — only the faithful restatement + stats + paths.

### Step 7 — Write

`akb_put(vault={vault_name}, collection=git-commits, title="{short_sha} {message_subject}", type=reference, tags={tags}, content={assembled_body}, summary={faithful_1_line})`. On success, return:

```markdown
## ingest-commit: created
- sha: {full_sha}
- doc_id: {new_doc_id}
- vault: {vault_name}
- collection: git-commits
- summary: {faithful_1_line}
```

## Failure handling

| Situation | Response |
|---|---|
| `<sha>` missing | `Missing required argument: <sha>` |
| Repo path is not a git repo | `Not a git repository: {repo_path}` |
| SHA not found in the repo | `SHA not found in repository: {sha}` |
| `--vault` not passed | `--vault {name} required.` |
| Vault does not exist | `Vault "{vault_name}" not found. Create with mcp__akb__akb_create_vault first.` |
| AKB write fails | Surface the error verbatim; do not retry. The caller decides. |
| Diff exceeds `500` lines | Record with `truncated: true`; not a failure. |
| Binary-only commit | Record with `diff_scope` including `binary`; Changes bullets say `Binary change in {path}`. |
| Merge commit | Record as-is with `is_merge: true`; no filter applied here. |
| SHA already ingested | Return `exists` status; not a failure. |
