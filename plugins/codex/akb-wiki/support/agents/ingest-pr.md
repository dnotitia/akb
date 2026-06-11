---
name: ingest-pr
description: Record a single GitHub PR merge event as a git-pr document in an AKB vault — PR title/body quoted verbatim, commit summaries pulled from pre-ingested git-commit docs. Fetched live via gh pr view.
model: gpt-5.4-mini
reasoning_effort: medium
---

# AKB Git PR Ingest

Record a **single PR merge event** as a `git-pr` document in the target AKB vault. Mechanical assembly — PR title/body quoted as author-authored compiled-truth; commit summaries pulled verbatim from `git-commit` documents already in the vault. No LLM synthesis. PR metadata is fetched live via `gh pr view`.

Scope boundaries:

- **One PR per invocation.** No loops, no cross-PR consolidation.
- **GitHub-only via `gh` CLI.** GitLab / Bitbucket / self-hosted forges unsupported. The repo at `--repo` must have a GitHub remote `gh` can resolve.
- **Commits must be pre-ingested** in `git-commits` as `git-commit` documents.
- **No LLM synthesis.** PR title/body quoted as-is; agent work is pulling commit summaries into a table.
- **No review/approval metadata.** Reviewers, labels, check statuses, comment threads are out of scope — `gh pr view` returns more fields than this schema uses; extras are ignored.
- **Upsert.** Dedup by `(repo, pr_number)`; re-ingest silently overwrites (normal when post-merge edits land on the PR description).

## Inputs (provided by the /akb-ingest router)

```text
/ingest-pr <pr-number> --vault {vault_name} [--repo {path}]
```

- `<pr-number>` (required, positional) — GitHub PR number (integer). The PR must be merged.
- `--vault` (required) — target AKB vault.
- `--repo` (optional) — path to the local clone of the GitHub repository. Defaults to the current working directory.

## Prerequisites

- AKB MCP server is accessible.
- `--vault {name}` provided.
- `git` and `gh` are available on PATH.
- `gh auth status` succeeds — the `gh` CLI is signed in to a GitHub account with read access to the target repo.
- The repo at `--repo` (or `pwd`) has a GitHub remote `gh` can resolve.

## Workflow

### Step 1 — Resolve inputs

Common resolution every git ingest subagent performs before its own skill-specific validation.

- Resolve `repo_path` from `--repo` or `pwd`. If `git -C {repo_path} rev-parse --git-dir` fails, fail with `Not a git repository: {repo_path}`.
- Resolve `vault_name` from `--vault`. Hard-fail if missing: `--vault {name} required.`
- Resolve `repo_name` as the last path segment of `repo_path` (normalized). All git ingest subagents use this same rule so they agree on the `repo` axis.

Validate `gh` is authenticated: `gh auth status` (non-zero → `gh CLI unavailable or not authenticated. Run gh auth login.`).

Fetch: `(cd {repo_path} && gh pr view {pr_number} --json number,title,body,author,mergedAt,mergeCommit,baseRefName,headRefName,baseRefOid,headRefOid,closingIssuesReferences)`.

Failure paths: "no GitHub remote" / "not a github repository" → `Repo at {repo_path} has no GitHub remote that gh can resolve.`; "no pull request found" / "could not resolve" → `PR not found in the configured GitHub remote: #{pr_number}.`; any other non-zero → surface `gh` stderr and fail with `gh pr view failed for #{pr_number}: {stderr}.`

Parse JSON into `pr`. If `pr.mergedAt` is null, fail with `PR #{pr_number} is not merged; this skill records merge events only.`

Set `merge_sha = pr.mergeCommit.oid`; validate via `git -C {repo_path} rev-parse --verify {merge_sha}^{commit}` (failure → `merge_sha not found in local repository: {merge_sha}. Fetch latest from the GitHub remote.`).

### Step 2 — Enumerate the PR's commits

Apply the strategies below in order; the first match wins.

1. **Explicit boundary** — if `pr.baseRefOid` and `pr.headRefOid` are both present and resolve in the local repo:
   ```bash
   git -C {repo_path} log --reverse --format=%H {pr.baseRefOid}..{pr.headRefOid}
   ```
   Covers rebase-and-merge repos where the merge commit has only one parent but multiple PR commits landed on the base. If either oid does not resolve locally (e.g. the head branch was deleted and pruned), fall through to strategy 2.

2. **Merge commit** — else, if `git -C {repo_path} rev-list --parents -n 1 {merge_sha}` reports **2 parents**:
   ```bash
   git -C {repo_path} log --reverse --format=%H {merge_sha}^1..{merge_sha}^2
   ```

3. **Squash / single-commit** — else (merge_sha has 1 parent): the commit set is `[merge_sha]` itself.

Call the resulting ordered list `sha_list` (chronologically ascending). If `sha_list` is empty, fail with `No commits enumerated for PR #{pr_number}; the local repo may be missing branch refs — fetch latest from the GitHub remote.`

### Step 3 — Dedup check

`akb_search(query="PR #{pr_number} {repo_name}", vault={vault_name}, collection=git-prs, type=reference, tags=["git", "kind:pr", "project:{repo_name}"], limit=5)`. Among hits, find the one whose frontmatter has `repo == repo_name AND pr_number == pr_number`; on match capture `existing_doc_id` (update path), else create path.

### Step 4 — Fetch commit documents

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

### Step 5 — Aggregate frontmatter axes

Compute from `commit_records`:

- `commit_uris` — ordered list of full URIs `akb://{vault_name}/doc/{commit_doc_path}` (chronological). Passed as `depends_on` in Step 7. Always-URI convention even for single-vault deployments.
- `commit_shas` — ordered chronological SHAs, preserved for cheap lookup without graph traversal.
- `pr_scope` — most-frequent non-null `scope` across records (parsed from each record's `scope:*` tag in `_fetch_commit_docs`; the per-commit summary derives it once at ingest as `conv_scope or top_level_paths[0]`). Ties → chronologically earliest contributor. `null` when all commits' scopes are null. Emitted as `scope:{pr_scope}` in Step 6.

PR doc body renders from per-commit doc bodies via the `depends_on` edge — no per-commit aggregation here. PR-level author is `pr.author.login`; commit authors live on each commit doc's `## Source`.

Build `links` from `pr.closingIssuesReferences`: each entry becomes `#{number}` (cross-repo: `{owner}/{repo}#{number}`). Order preserved from `gh`.

### Step 6 — Assemble the page

Frontmatter:

```yaml
type: reference
status: active

repo: "{repo_name}"
project: "{repo_name}"             # mirrors repo; `repo` is the source-system identifier, `project` is the project-rollup namespace
pr_number: {pr.number}
title: "{pr.title}"

merged_at: "{pr.mergedAt}"
merge_sha: "{merge_sha}"
base_ref: "{pr.baseRefName | null}"
head_ref: "{pr.headRefName | null}"
base_sha: "{pr.baseRefOid | null}"
head_sha: "{pr.headRefOid | null}"

commit_shas: [{sha}, ...]

links: [{link}, ...]
tags: ["git", "project:{repo_name}", "kind:pr", "scope:{pr_scope}"]
summary: "{pr.title}"

related_to: []
```

PR commit membership lives on the `depends_on` graph edge passed as a top-level arg in Step 7 — not frontmatter. Outgoing `depends_on` is queryable via `akb_relations`; the release skill consumes it through `akb_search` + `set(pr.depends_on)`.

`scope:{pr_scope}` omitted when Step 5 produced null. `kind:pr` is the cross-plugin discriminator (read by the external maintenance layer and `/ingest-release` Step 7); `project:{repo_name}` is the rollup namespace and repo-identifier SSOT. Per-commit author / paths / stats / conv-type / diff-scope live on each commit doc's tags + `## Source`, not aggregated here.

Body:

```markdown
# {pr.title}

## Description
> {pr.body, verbatim, preserving blank lines and formatting}

## Linked
- {link_1}
- {link_2}
...

(If `links` is empty, omit this section entirely.)

## Commits
| # | SHA | Author | Summary |
|---|-----|--------|---------|
| 1 | `{short_sha}` | {author} | {summary from commit doc} |
| 2 | ... | ... | ... |

## Merge
- merge_sha: `{merge_sha}`
- base_ref → head_ref: `{pr.baseRefName}` → `{pr.headRefName}` (if provided)
- merged_at: {pr.mergedAt}
```

Assembly rules: quote the PR body as a single block-quote (no paraphrase / truncate / reflow); render `> (no description)` when `pr.body` is empty rather than omitting; preserve fenced code blocks inside the quote; pull Commits table `summary` from each commit doc (never re-derive); append `(truncated)` to a row when the commit doc has `truncated: true`.

### Step 7 — Write the PR document

**Create** — `akb_put(vault={vault_name}, collection=git-prs, title="PR #{pr.number}: {pr.title}", type=reference, tags={tags}, content={assembled_body}, summary={pr.title}, depends_on={commit_uris})`.

**Update** — `akb_update(doc_id={existing_doc_id}, vault={vault_name}, title="PR #{pr.number}: {pr.title}", tags={tags}, content={assembled_body}, summary={pr.title}, depends_on={commit_uris})`.

`depends_on` passes as a top-level arg so AKB stores graph edges (queryable via `akb_relations`), not opaque frontmatter. PR-to-commits lives entirely on this edge — no commit-side backlink. Re-ingest rewrites the edge set idempotently. Capture `pr_doc_id` / `pr_doc_path`.

### Step 8 — Return

```markdown
## ingest-pr: {created|updated}
- repo: {repo_name}
- pr_number: {pr.number}
- doc_id: {pr_doc_id}
- commits: {N}
- vault: {vault_name}
- collection: git-prs
- merge strategy: {explicit | merge_commit | squash}
```

## Failure handling

| Situation | Response |
|---|---|
| `<pr-number>` missing | `Missing required argument: <pr-number>` |
| Repo path is not a git repo | `Not a git repository: {repo_path}` |
| `gh` not on PATH or not authenticated | `gh CLI unavailable or not authenticated. Run gh auth login.` |
| Repo has no GitHub remote | `Repo at {repo_path} has no GitHub remote that gh can resolve.` |
| `gh pr view` reports PR not found | `PR not found in the configured GitHub remote: #{pr_number}.` |
| `gh pr view` failed for any other reason | `gh pr view failed for #{pr_number}: {stderr}.` |
| PR not yet merged (`mergedAt` is null) | `PR #{pr_number} is not merged; this skill records merge events only.` |
| `merge_sha` from `gh` not in local repo | `merge_sha not found in local repository: {merge_sha}. Fetch latest from the GitHub remote.` |
| `--vault` not passed | `--vault {name} required.` |
| Vault does not exist | `Vault "{vault_name}" not found. Create with mcp__akb__akb_create_vault first.` |
| Enumeration yielded 0 commits | `No commits enumerated for PR #{pr_number}; the local repo may be missing branch refs — fetch latest from the GitHub remote.` |
| One or more commits missing in vault | `Missing commit(s) in vault: {sha_list}. Ingest the commit(s) via /akb-ingest first.` |
| AKB write fails | Surface the error verbatim; do not retry. |
| Dedup hit | Update existing PR doc silently. Not an error. |
