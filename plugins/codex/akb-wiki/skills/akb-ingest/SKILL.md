---
name: akb-ingest
description: Ingest whatever you point at into an AKB vault — a local file, a web URL, a GitHub PR/release/commit, a Confluence page, or a Jira issue. Auto-detects the source type and dispatches to a specialized ingest subagent; the router fetches and writes nothing itself. One target per invocation; globs expand to a sequential loop of document ingests.
---

# AKB Ingest

Ingest **one target** into the target AKB vault. Point at a local file, a web URL, a GitHub PR / release / commit, a Confluence page, or a Jira issue — this skill classifies the source type and dispatches to a specialized ingest subagent. It is the single ingest entry point of the `akb-wiki` plugin.

**Workflow classification.** LLM-wiki **Ingest** workflow. This is a **router**: it classifies a target and delegates the actual fetch + write to one subagent. Document-style sources pay down **intent debt** (and substrate health) as five-section synthesis pages; decision-record sources (PR / release / commit / issue) pay down **intent debt** as faithful mechanical episodic records. The archetype split (synthesis vs mechanical) is a dispatch decision, invisible to the caller.

Scope boundaries:

- **One target per invocation.** A glob pattern is the only fan-out: it expands into a sequential loop of `ingest-doc` runs, one per matched file.
- **The router fetches nothing and writes nothing.** It only classifies, validates flags, dispatches one subagent, and relays the subagent's report. Each subagent owns exactly one fetch path (file/URL read, `git`, `gh`, or the Atlassian MCP server) — there is no pre-fetched-payload escape hatch at any layer.
- **No cross-source wiring at ingest.** Consolidation into concept pages and cross-reference across sources are handled by an external maintenance layer (dream-cycle) over the same vault, not here.

## Arguments

```text
/akb-ingest <target> --vault {vault_name} [--repo {path}] [--prev-tag {tag}] [--lang {language}] [--project {name}] [--raw | --no-raw]
```

- `<target>` (required, positional) — what to ingest. A local path (absolute, `./…`, `~/…`, or a glob), an `http(s)://` URL, a bare git commit SHA, or a Jira issue key (e.g. `SDDEV-360`).
- `--vault` (required) — target AKB vault name; forwarded to every subagent.
- `--repo {path}` (optional) — local git clone path for commit / PR / release targets. Defaults to the current working directory.
- `--prev-tag {tag}` (optional) — previous release tag; release targets only.
- `--lang {language}` (optional) — language for synthesized note bodies; document and Confluence targets only.
- `--project {name}` (optional) — project-rollup namespace; Confluence and Jira targets only.
- `--raw` / `--no-raw` (optional) — override raw-byte preservation; document targets only.

## Prerequisites

- AKB MCP server is accessible.
- `--vault {name}` provided.
- The fetch path of the **classified** source must be available: `WebFetch` for URLs, `git`/`gh` for GitHub targets, an attached Atlassian MCP server for Confluence/Jira targets. The router requires only the path matching the target it classifies — a missing Atlassian MCP server never blocks a local-file ingest.

## Workflow

### Step 1 — Parse

Parse `--vault` from arguments. Hard-fail if missing: `--vault {name} required.` Capture `<target>` and any supplied flags. Fail with `Missing required argument: <target>` when no positional target is given.

### Step 2 — Classify the target

Determine the source type and the subagent to dispatch. Apply the rules top to bottom; the first match wins.

| Target shape | `<chosen-agent>` | Forwarded flags |
|---|---|---|
| Local path that exists, or a glob pattern (`*`, `?`, `[`) | `ingest-doc` | `--lang`, `--raw`/`--no-raw` |
| `https?://…/…/pull/<n>` on `github.com` | `ingest-pr` | `--repo` |
| `https?://…/…/releases/tag/<tag>` on `github.com` | `ingest-release` | `--repo`, `--prev-tag` |
| `https?://…/…/commit/<sha>` on `github.com` | `ingest-commit` | `--repo` |
| `https?://<host>.atlassian.net/wiki/…` | `ingest-confluence` | `--lang`, `--project` |
| `https?://<host>.atlassian.net/browse/<KEY>` | `ingest-jira` | `--project` |
| Any other `http(s)://` URL | `ingest-doc` | `--lang`, `--raw`/`--no-raw` |
| Bare hex string 7–40 chars, confirmed a commit via `git -C {repo} cat-file -t {target}` → `commit` | `ingest-commit` | `--repo` |
| Matches `^[A-Z][A-Z0-9_]+-\d+$` (Jira key) | `ingest-jira` | `--project` |
| A name that resolves to a git tag (`git -C {repo} rev-parse --verify {target}`) and `--repo`/cwd is a repo | `ingest-release` | `--repo`, `--prev-tag` |

**Resolve the target before dispatch.** Each subagent expects a *bare* identifier, not a URL. When a GitHub or Jira **URL** matched, set `resolved_target` to the identifier captured from the path — do not forward the raw URL:

- `…/pull/<n>` → `<n>` (the PR number)
- `…/releases/tag/<tag>` → `<tag>`
- `…/commit/<sha>` → `<sha>`
- `…/browse/<KEY>` → `<KEY>` (the Jira issue key)

For a GitHub URL the local clone still comes from `--repo` (or cwd), **not** the URL — only the number / tag / SHA is taken from the URL. Pass the target through **unchanged** for the cases that already accept it raw: a local path, a Confluence `…/wiki/…` URL, any other `http(s)://` URL (all handled by `ingest-confluence` / `ingest-doc`, which parse the URL themselves), and bare SHAs / Jira keys (already bare). `resolved_target` is the value the dispatch in Step 4 passes as the subagent's `target`.

Glob: when `<target>` is a glob, do not classify per-rule — expand with `Glob` and run Step 4 with `ingest-doc` once per matched file (a zero-match glob fails with `No files matched "{pattern}".`).

**Ambiguity.** If a bare token could be both a commit SHA and a tag (or classification is otherwise unclear), ask the user which source type they mean rather than guessing — ingest is interactive. Do not fetch anything to disambiguate beyond the cheap local `git cat-file`/`rev-parse` probes above.

### Step 3 — Validate flags

Keep only the flags listed for the classified type in Step 2. If the caller supplied a flag that does not apply (e.g. `--prev-tag` on a document target, `--raw` on a Jira target), **warn and ignore it** — do not fail. `--vault` is always forwarded.

### Step 4 — Dispatch

Launch **one** subagent with `fork_context: false` — the one the classification selected (`<chosen-agent>` ∈ `ingest-doc / ingest-confluence / ingest-commit / ingest-pr / ingest-release / ingest-jira`). Open its prompt file and use the contents as the worker prompt, with model `gpt-5.4-mini` and reasoning effort `medium`:

- `./support/agents/<chosen-agent>.md`

Prepend an `[Ingest Context]` block instructing the worker to treat the resolved target, `--vault {vault_name}`, and any forwarded flags as its positional + named arguments. The worker owns the fetch path and the AKB write; the router fetches and writes nothing. Relay the worker's status report block unchanged.


### Step 5 — Report

Relay the subagent's status report block to the user unchanged. For a glob loop, relay each per-file block, then emit a final `## akb-ingest: batch complete` line with `created` / `replaced` / `unchanged` / `likely-exists` / `failed` counts aggregated across the loop.

## Failure handling

| Situation | Response |
|---|---|
| `<target>` missing | `Missing required argument: <target>` |
| `--vault` not passed | `--vault {name} required.` |
| Glob matched zero files | `No files matched "{pattern}".` |
| Target cannot be classified | `Could not classify target "{target}". Pass a local path, URL, commit SHA, or Jira key.` |
| Ambiguous target | Ask the user which source type they mean; do not guess. |
| Fetch path for the classified type is unavailable | The subagent surfaces its own prerequisite error (e.g. `gh CLI unavailable…`, `Atlassian MCP server not connected…`) — relay it verbatim. |

The router does not create vaults or collections and does not touch AKB directly; vault/collection and write errors surface from the dispatched subagent and are relayed as-is.
