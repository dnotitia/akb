---
name: ingest-release
description: Record a single release tag as a git-release document in an AKB vault — release notes from the matching GitHub Release (annotated tag message fallback), commits bucketed by conv_type. Requires range commits pre-ingested.
model: sonnet
tools: Bash(git *), Bash(gh *), Read, mcp__akb__akb_search, mcp__akb__akb_get, mcp__akb__akb_relations, mcp__akb__akb_put, mcp__akb__akb_update
---

# AKB Git Release Ingest

Record a **single release/tag event** as a `git-release` document in the target AKB vault. Mechanical assembly only — the release notes (from a GitHub Release fetched via `gh release view`, or the annotated tag message as fallback) are quoted as compiled-truth, and the commit/PR membership is derived from documents already in the vault. No LLM synthesis. This skill does not accept hand-supplied payloads — the canonical sources of release-notes text are GitHub itself (via `gh`) and `git` itself (annotated tag message).

Scope boundaries:

- **One tag per invocation.** No loops, no cross-release consolidation.
- **GitHub-first, git-fallback for release notes.** A GitHub Release for the tag (fetched via `gh release view --json`) wins as compiled-truth body; if no GitHub Release exists, an annotated tag message is the fallback; otherwise the body is `(no release notes)`. Lightweight tags with no GitHub Release legitimately have no body.
- **Commits must be pre-ingested.** Every commit in the tag window must already exist in `git-commits` as a `git-commit` document.
- **No LLM synthesis.** Release notes are quoted verbatim; commits are bucketed by `conv_type` mechanically, not summarized by an agent.
- **No release asset / binary handling.** Checksums, artifact URLs, draft/prerelease flags, and signing metadata are out of scope.
- **No semver interpretation.** The `tag` string is treated opaquely — this skill does not parse version components or enforce ordering rules.
- **Upsert.** Dedup by `(repo, tag)`. Re-ingesting the same tag silently overwrites — legitimate when release notes are edited after the fact on GitHub.

## Inputs (provided by the /akb-ingest router)

```text
/ingest-release <tag> --vault {vault_name} [--prev-tag {tag}] [--repo {path}]
```

- `<tag>` (required, positional) — release tag name (e.g. `v3.0.0`). Must resolve inside the target repo.
- `--vault` (required) — target AKB vault.
- `--prev-tag` (optional) — the previous release tag; overrides auto-discovery. First releases (no prior tag) are valid and enumerate from root.
- `--repo` (optional) — path to the local clone of the GitHub repository. Defaults to the current working directory.

## Prerequisites

- AKB MCP server is accessible.
- `--vault {name}` provided.
- `git` and `gh` are available on PATH.
- `gh auth status` succeeds — required so the skill can attempt to fetch a GitHub Release for the tag. (If the repo has no GitHub remote or no Release exists for the tag, the skill falls back to the annotated tag message — `gh` itself is still required.)
- `{tag}` resolves inside the target repo.
- Every commit in the `prev_tag..tag` (or root..tag, for first release) range has a `git-commit` document in the vault.

## Workflow

### Step 1 — Resolve inputs

Common resolution every git ingest subagent performs before its own skill-specific validation.

- Resolve `repo_path` from `--repo` or `pwd`. If `git -C {repo_path} rev-parse --git-dir` fails, fail with `Not a git repository: {repo_path}`.
- Resolve `vault_name` from `--vault`. Hard-fail if missing: `--vault {name} required.`
- Resolve `repo_name` as the last path segment of `repo_path` (normalized). All git ingest subagents use this same rule so they agree on the `repo` axis.

Then validate that `gh` is available and authenticated:

```bash
gh auth status
```

Fail with `gh CLI unavailable or not authenticated. Run gh auth login.` on non-zero exit.

Then validate the tag: `git -C {repo_path} rev-parse --verify {tag}`. Fail with `Tag not found in repository: {tag}` on error. Also resolve `tagged_commit_sha = git -C {repo_path} rev-parse {tag}^{commit}`.

### Step 2 — Extract tag metadata

Determine the tag kind:

```bash
git -C {repo_path} cat-file -t {tag}
```

- If the output is `tag` → **annotated**. Parse the tag object:
  ```bash
  git -C {repo_path} cat-file tag {tag}
  ```
  Extract `tagger` (name + email), `tagged_at` (ISO 8601), and the tag message (everything after the blank line separator). Normalize the tagger name using the same rules as `ingest-commit` (`.mailmap` + `author_aliases`).
- If the output is `commit` → **lightweight**. Use the tagged commit's committer as `tagger` and its `committed_at` as `tagged_at`. `tag_message` is `null`.

Record `tag_kind` as `"annotated"` or `"lightweight"`.

#### Fetch GitHub Release notes

Attempt `(cd {repo_path} && gh release view {tag} --json tagName,name,body,publishedAt,author)`. Outcome handling:

- Exit 0 → set `gh_release.body` and `gh_release.published_at` from the response. Step 9 prefers `body` over the tag message only when non-empty after trimming whitespace.
- `gh` reports "release not found" / no GitHub remote → set `gh_release = null` and continue. Not a failure — many tags have no corresponding GitHub Release; non-GitHub remotes still ingest using only the annotated tag message.
- Any other non-zero exit → fail with `gh release view failed for {tag}: {stderr}.`

### Step 3 — Resolve `prev_tag`

1. Use the `--prev-tag` CLI argument if provided.
2. Otherwise auto-discover:
   ```bash
   git -C {repo_path} describe --tags --abbrev=0 {tag}^
   ```
   If this fails (no prior tag exists), treat as the first release. Emit an informational line in the final report (`first release — enumerating from root`) — not an error.

If `--prev-tag` was given, validate with `git -C {repo_path} rev-parse --verify {prev_tag}`. Fail with `prev_tag not found in repository: {prev_tag}` if the override is invalid.

### Step 4 — Enumerate commits

If `prev_tag` is resolved:

```bash
git -C {repo_path} log --reverse --format=%H {prev_tag}..{tag}
```

If `prev_tag` is `null` (first release):

```bash
git -C {repo_path} log --reverse --format=%H {tag}
```

Call the resulting ordered list `sha_list` (chronologically ascending). If `sha_list` is empty:

- For first-release case with empty history: fail with `No commits in tag {tag}.`
- For `prev_tag..tag` yielding empty: fail with `No commits between {prev_tag} and {tag}; check the range.`

### Step 5 — Dedup check

```text
mcp__akb__akb_search(
  query="release {tag} {repo_name}",
  vault="{vault_name}",
  collection="git-releases",
  type="reference",
  tags=["git", "kind:release", "project:{repo_name}"],
  limit=5
)
```

Among the hits, find the one whose frontmatter has `repo == repo_name AND tag == tag`. If found, capture `existing_doc_id` for the update path; otherwise, proceed to create.

### Step 6 — Fetch commit documents

Given a chronologically-ordered list `sha_list`, resolve each SHA to its commit document (`type=reference, kind:commit`) in the vault.

**Concurrency is required.** Issue all searches in a single parallel tool-use block, then issue any required `akb_get` calls in a second parallel block. Sequential per-SHA loops are forbidden — a 30-commit input is two batched round trips, not 30 or 60.

```text
mcp__akb__akb_search(
  query="{sha}",
  vault="{vault_name}",
  collection="git-commits",
  type="reference",
  tags=["git", "kind:commit"],
  limit=3
)
```

For each search, pick the hit whose title prefix matches `{short_sha}` — the per-commit summary doc's title is `{short_sha} {message_subject}`, and `akb_search` returns the title in its hit payload, so the search query (`query="{full_sha}"`) plus a title-prefix check uniquely identifies the matching commit. The downstream-needed fields below are all reachable from the `akb_search` hit (`path`, `title`, `summary`, `tags`); only fall back to `akb_get` if a specific consumer needs body content (e.g., for surfacing author detail in a human-readable table).

For each `commit_records[i]`, populate the downstream-needed fields from the AKB primitives that durably persist them:

- `doc_id`, `path`, `title`, `summary` — from the search hit (whitelist fields exposed by `akb_search`).
- `short_sha` — first 7 chars of the full SHA (already known from the search query); also recoverable from the title prefix.
- `author` — parse from the row's `tags` array as `tag[len("author:"):]` for the first `author:*` tag emitted by `/ingest-commit` Step 6. Single-author commits emit one such tag; merge commits emit either the merging committer or whichever name `.mailmap` resolved. `null` if no `author:*` tag is present (defensive — the commit ingest always emits one).
- `conv_type` — parse from the row's `tags` array as `tag[len("type:"):]` for the first `type:*` tag. `null` when the commit subject did not match the conventional-commit pattern.
- `scope` — parse `tag[len("scope:"):]` for the first `scope:*` tag. Drives PR-level `pr_scope` aggregation downstream without re-deriving from a `paths` list.

Skip the secondary `akb_get` whenever the search hit already exposes everything in the list above — PR / release ingest only needs `path` / `summary` / `tags`, all reachable from `akb_search`. A 30-commit PR is one batched `akb_search` round trip, not 30+30.

The full file list, per-commit stats, and diff scope are not aggregated across commits — the per-commit body's `### Paths` and `## Stats` sections are the audit surface, reachable via the PR's `depends_on` graph edge.

If any SHA in `sha_list` has no matching hit, **fail** before proceeding:

```text
Missing commit(s) in vault: {missing_sha_list}.
Run `/akb-ingest <sha>` for each before re-running.
```

Never re-fetch diffs or invoke git for individual commits here. The ingested `summary` and `Changes` block are authoritative — diff re-reading would defeat the reason ingest paid that cost.

Collect the results into `commit_records`, aligned with `sha_list`.

### Step 7 — Derive the PR axis

There is no commit-side backlink to read — PR-to-commit linkage lives on each PR document's outgoing `depends_on` graph edge (set by `ingest-pr` Step 7). Discover candidate PRs by searching the vault, then intersect each PR's `depends_on` with the release's commit set: `range_commit_uris = { "akb://{vault_name}/doc/{record.path}" for record in commit_records }`.

Search for PRs: `akb_search(vault="{vault_name}", query="pr {repo_name}", collection="git-prs", type="reference", tags=["git", "kind:pr", "project:{repo_name}"], limit=500)`. Hits return summaries (`path`, `title`, `summary`, `tags`, fusion score) but no frontmatter or graph edges — hydrate separately:

1. **Hydration block** (one parallel tool-use block) — `akb_get(vault="{vault_name}", doc_id={hit.path})` for every hit. Capture `path`, frontmatter `repo` / `pr_number` / `merged_at`, and `title`. Filter to rows whose `repo == repo_name`.
2. **Outgoing-edge block** (second parallel tool-use block) — `akb_relations(vault="{vault_name}", resource_uri="akb://{vault_name}/doc/{pr.path}", direction="outgoing", type="depends_on")` for each surviving PR. Sequential per-PR fetches are forbidden.

For each PR, set `pr.commit_uris` to the outgoing edges and compute `pr.commit_uris ∩ range_commit_uris`. Drop PRs with empty intersection (out of range) and PRs with empty `depends_on` (their commits surface as orphans in the body). Order survivors by `merged_at` ascending into `pr_records` with `doc_id` / `path` / `pr_number` / `title` / `merged_at` / `covered_commit_uris`.

Compose:

```
pr_doc_refs         = [ "akb://{vault_name}/doc/{pr.path}" for pr in pr_records ]   # full URIs, chronological
covered_commit_uris = ⋃ { pr.covered_commit_uris for pr in pr_records }
orphan_commit_uris  = range_commit_uris − covered_commit_uris
```

`covered_commit_uris` and `orphan_commit_uris` partition `range_commit_uris`. Orphans render as individual commit rows in the body.

### Step 8 — Compose the release's graph edge

From `commit_records`, the only downstream-needed value is `commit_shas` — an ordered list of full SHAs (chronological), preserved in the release doc's frontmatter for cheap audit lookup without traversing the `depends_on` chain. No per-commit aggregation (stats / conv-types / diff-scope / authors / paths) is performed at the release level — those keys are silent-dropped by the `akb_put` whitelist anyway. The body's `## Changes` / `## All commits` / Range sections render directly from `commit_records` and `pr_records` for human readers; the external maintenance layer does not aggregate from release docs.

Compose the release's `depends_on` set — the PR-or-orphan-commit edges this release covers (PR-covered commits are reachable transitively via the PR's own `depends_on`, so the release does not duplicate them):

```
release_depends_on = pr_doc_refs + sorted(orphan_commit_uris by chronological order in commit_records)
```

### Step 9 — Assemble the page

Frontmatter:

```yaml
type: reference
status: active

repo: "{repo_name}"
tag: "{tag}"
prev_tag: "{prev_tag | null}"

tagger: "{canonical_tagger_name}"
tagger_email: "{raw_tagger_email}"
tagged_at: "{tagged_at_iso}"
tagged_commit_sha: "{tagged_commit_sha}"
tag_kind: "{annotated | lightweight}"

commit_shas: [{sha}, ...]

tags: ["git", "project:{repo_name}", "kind:release"]
summary: "{tag} ({len(commit_records)} commits, {len(pr_records)} PRs)"
```

Commit and PR membership lives entirely on the `depends_on` graph edge passed to `akb_put` / `akb_update` in Step 10 (PR-covered commits transitively, orphans directly) — not in frontmatter. `akb_relations` traverses both layers.

Body (bucket by `conv_type`):

```markdown
# {tag}

## Release notes
> {gh_release.body (if non-empty) OR tag_message OR "(no release notes)"}

## Changes

### New Features
- PR #{pr_number}: {pr_title}
- Commit `{short_sha}`: {commit_summary}

### Fixes
- ...

### Other
- ...

## Range
- prev_tag → tag: `{prev_tag | "—"}` → `{tag}`
- tagged_commit: `{short_tagged_commit_sha}`
- tagged_at: {tagged_at}
- kind: {tag_kind}

## All commits
| # | SHA | PR | Author | Summary |
|---|-----|-----|--------|---------|
| 1 | `{short_sha}` | `#{pr_number}` or `—` | {author} | {commit_summary} |
| 2 | ... | ... | ... | ... |
```

#### Bucketing rules for `## Changes`

Render three subsections: **New Features**, **Fixes**, **Other**.

1. Assign each PR in `pr_records` to a bucket by its dominant `conv_type`:
   - At least one commit with `conv_type == feat` → **New Features**
   - Else at least one with `conv_type == fix` → **Fixes**
   - Else → **Other**
   Render one row per PR: `- PR #{pr_number}: {pr_title}`.
2. Assign each orphan commit (whose URI is in `orphan_commit_uris` from Step 7) by its own `conv_type`:
   - `feat` → **New Features**
   - `fix` → **Fixes**
   - Anything else (including `null` conv_type) → **Other**
   Render one row per orphan commit: ``- Commit `{short_sha}`: {commit_summary}``.
3. Omit any bucket that is empty (do not print an empty section header).

This bucketing is **rule-based**, not LLM-driven. Do not reword the PR titles or commit summaries — quote them as they already exist in the vault.

#### `## All commits` table

Lists **every** commit in `sha_list` (chronological), regardless of PR membership. Columns:

- `#` — 1-based index.
- `SHA` — `short_sha` in code formatting.
- `PR` — `#{pr_number}` if the commit's URI appears in any `pr.covered_commit_uris` from `pr_records`, else `—`.
- `Author` — from the commit doc.
- `Summary` — the commit doc's `summary` (faithful 1-line). Append `(truncated)` if its `truncated` flag is true.

### Step 10 — Write the release document

**Create.** `akb_put(vault="{vault_name}", collection="git-releases", title="Release {tag}", type="reference", tags={tags}, content={assembled_body}, summary="{tag} ({len(commit_records)} commits, {len(pr_records)} PRs)", depends_on={release_depends_on})`.

**Update.** `akb_update(vault="{vault_name}", doc_id={existing_doc_id}, title="Release {tag}", tags={tags}, content={assembled_body}, summary="{tag} ({len(commit_records)} commits, {len(pr_records)} PRs)", depends_on={release_depends_on})`.

`depends_on` is passed as a top-level argument so AKB stores it as graph edges. The release-to-PR-and-orphan-commit relationship lives entirely on this edge — no member-side `release` backlink is written. PR-covered commits are reachable transitively via the PR's own `depends_on`. Re-ingest of the same tag rewrites the edge set idempotently. Capture the resulting `release_doc_id` and `release_doc_path`.

### Step 11 — Return

```markdown
## ingest-release: {created|updated}
- repo: {repo_name}
- tag: {tag}
- prev_tag: {prev_tag | "— (first release)"}
- doc_id: {release_doc_id}
- commits: {len(commit_records)} ({len(orphan_commit_uris)} orphan, {len(commit_records) - len(orphan_commit_uris)} PR-covered)
- prs: {len(pr_records)}{ ", {K} PR(s) skipped (empty depends_on)" if K > 0 }
- vault: {vault_name}
- collection: git-releases
- tag_kind: {annotated | lightweight}
```

## Failure handling

| Situation | Response |
|---|---|
| `<tag>` missing | `Missing required argument: <tag>` |
| Tag not in repo | `Tag not found in repository: {tag}` |
| `--prev-tag` override not in repo | `prev_tag not found in repository: {prev_tag}` |
| prev_tag auto-discovery fails | Proceed as first release; report it in the final summary. |
| `gh` not on PATH or not authenticated | `gh CLI unavailable or not authenticated. Run gh auth login.` |
| `gh release view` failed for a non-"not found" reason | `gh release view failed for {tag}: {stderr}.` |
| `gh release view` reports no release for the tag, or repo has no GitHub remote | Continue with `gh_release = null`; the annotated tag message becomes the release-notes body. Not an error. |
| Repo path is not a git repo | `Not a git repository: {repo_path}` |
| `--vault` not passed | `--vault {name} required.` |
| Vault does not exist | `Vault "{vault_name}" not found. Create with mcp__akb__akb_create_vault first.` |
| Empty commit range | `No commits between {prev_tag} and {tag}; check the range.` (or `No commits in tag {tag}.` for first release) |
| One or more commits missing in vault | `Missing commit(s) in vault: {sha_list}. Ingest the commit(s) via /akb-ingest first.` |
| A PR in the repo has empty `depends_on` | Drop silently from `pr_records`; affected commits surface as orphans. Reported in the return summary as informational, not an error. |
| AKB write fails | Surface verbatim; do not retry. |
| Dedup hit | Update existing release doc silently. |
