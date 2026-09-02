---
status: implemented
stage: complete
issue: AKB-227
created: 2026-09-02
updated: 2026-09-02
---

# Document version diff UI

## Decision

Implement AKB-227, but narrow the launch scope to a **read-only unified diff of
one document revision against its immediate parent**.

The capability is justified: AKB already preserves immutable document history
and exposes a reader-authorized diff endpoint, while the current UI makes a
person open two complete revisions and compare them mentally. The useful loop is
small and common: History → choose a revision → see what that revision changed →
return to the same document and History position.

The original issue becomes excessive if “two versions” is interpreted as an
arbitrary revision picker, or if folding, custom search, split view, rich
Markdown structural comparison, restore, comments, and merge conflict handling
all ship together. Those are separate products. Launch should solve change
inspection without turning the document reader into a code-review system.

## Current-state audit

### What already exists

- `GET /api/v1/diff/{vault}/{doc_id}?commit=...` compares the selected commit
  with its immediate parent and is available to every Vault reader.
- `GET /api/v1/history/{vault}/{doc_id}` returns the document Resource's logical
  lineage newest-first, including revision hash, message, author, resolved
  author name, and time.
- `GET /api/v1/documents/{vault}/{doc_id}?version=...` already powers the
  route-addressable historical Rendered / Raw reader.
- Document previews launched from Search are route-backed. Query parameters can
  change without discarding the launching Search state.
- `@tanstack/react-query` and `@tanstack/react-virtual` are already dependencies,
  so immutable-result caching and bounded large-list rendering need no new
  framework.

### Gaps that must be fixed before adding Diff

1. The document History panel currently calls Vault activity with a path filter
   rather than the document History endpoint. A path-scoped activity feed is
   not the same as one Resource lineage: path reuse and moves can make it show
   events that do not belong to the current logical document.
2. History is held in local effects and untyped `any`; there is no frontend
   History or Diff API contract.
3. The generic API error path does not preserve the HTTP status for FastAPI
   string `detail` bodies. A new compatibility-sensitive helper cannot rely on
   `instanceof ApiError` to distinguish an old server's 404/405.
4. The Diff response is a raw unified patch. It contains no explicit base
   revision or change statistics. The UI can infer the immediate parent from
   the ordered History list and compute visible additions/deletions, but must
   label a missing base hash honestly as `Previous revision`.
5. Native revisions compare the stored Markdown payload, including frontmatter.
   System-managed metadata such as `updated_at` may therefore appear beside
   author changes. Launch should show the exact stored patch; a body-only or
   semantic metadata diff would require a separate backend contract.
6. A document body may be large enough that an unbounded patch should not be
   expanded into thousands of DOM rows. Diff must be requested only on demand,
   parsed after a client size guard, and virtualized above a measured threshold.

## Research synthesis

- GitHub and GitLab distinguish a file's own history from repository-wide
  history and provide explicit revision comparison. AKB should likewise anchor
  Diff in the document's logical History, not in the Vault activity feed.
- Unified view is the right default for AKB. The reader already shares width
  with persistent Vault navigation and may run inside a Search preview; a split
  view would halve the useful measure and add a mode without improving the
  immediate-parent task.
- A unified patch already omits most unchanged text and organizes changes into
  hunks. “Collapse unchanged” is therefore inherent at launch. Expanding
  omitted context requires the complete before/after bodies and is deferred.
- VS Code exposes a separate accessible unified diff representation with
  previous/next-difference navigation. AKB should use the same principle:
  visible hunk navigation and a text-first unified representation, not a purely
  visual red/green comparison.
- WCAG 2.2 SC 1.4.1 forbids color as the only distinction. Added and removed
  lines need literal `+` / `−` markers and programmatic labels in addition to
  semantic color.
- `react-diff-view`, Diff2Html, and CodeMirror Merge all cover broader review or
  editor use cases. Diff2Html generates foreign HTML/CSS, and CodeMirror Merge
  requires two complete documents and editor infrastructure. Both are too much
  for this read-only surface. `react-diff-view` is viable but brings its own
  renderer and styling vocabulary. AKB gets a smaller, more governable result
  from a zero-dependency unified-diff parser plus its existing virtualizer and
  design tokens.

References (reviewed 2026-09-02):

- [GitHub: comparing commits](https://docs.github.com/en/pull-requests/how-tos/commit-changes/comparing-commits)
- [GitHub: viewing and understanding files](https://docs.github.com/en/repositories/working-with-files/using-files/viewing-and-understanding-files)
- [GitLab: compare revisions](https://docs.gitlab.com/user/project/repository/compare_revisions/)
- [VS Code diff-editor accessibility](https://github.com/microsoft/vscode-docs/blob/main/docs/configure/accessibility/accessibility.md#diff-editor-accessibility)
- [W3C: Understanding SC 1.4.1, Use of Color](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color)
- [`parse-diff`](https://github.com/sergeyt/parse-diff)
- [`react-diff-view`](https://github.com/otakustay/react-diff-view)
- [CodeMirror Merge](https://codemirror.net/docs/ref/#merge)
- [Diff2Html](https://github.com/rtfpessoa/diff2html)

## Product scope

### Launch scope

- History uses the document-lineage endpoint, with an old-server activity
  fallback only for keeping the existing version list usable.
- Every eligible History row exposes `View version` and `View changes`.
- `View changes` opens the main document canvas in Diff mode; it does not open
  a nested modal or compress the patch into the 24rem document inspector.
- Diff compares the selected revision with its immediate parent.
- A compact comparison header shows base → target, author, time, commit message,
  additions, deletions, and hunk count.
- Unified raw Markdown patch with old/new line gutters, literal change markers,
  semantic labels, Copy patch, and previous/next hunk controls.
- First revision, unchanged, unknown revision, unsupported server, oversized
  patch, permission loss, network/server failure, and normal loading each have a
  stable state in the same viewer frame.
- URL-addressable mode and exact Viewer/Search-preview state restoration.

### Explicit non-goals

- Arbitrary base and target revision selection.
- Split/side-by-side mode.
- Rendered Markdown or AST-level semantic diff.
- Inline comments, approval, restore, revert, merge, or conflict resolution.
- Syntax highlighting and intra-line word diff.
- Expanding omitted unchanged source text.
- Diffing binary files, Vault files, tables, or whole commits.

These can be reconsidered only after usage shows that immediate-parent change
inspection is insufficient.

## Information architecture

Diff is a peer read mode inside the existing Document workspace, but it is
entered from History rather than made a permanently visible third tab for every
document.

```text
Document workspace header
  Title · Vault · Collection                         Full page · Edit · …

Historical / comparison status bar
  Comparing previous revision → a1b2c3d             Back to version

File-context row
  Author · time · commit message          History · Document panel toggle

Diff viewer frame
  +12  −4 · 3 changes                Previous change · Next change · Copy
  ───────────────────────────────────────────────────────────────────────
  old  new  marker  unified Markdown line
   18   18           context
   19    —      −    removed line
    —   19      +    added line
```

The document title is not repeated. The primary identity header and outer
8–12px document gutter stay unchanged. The document inspector remains an
overlay, so opening Info or History never reduces Diff width.

### History row

Use a two-line, content-sized row rather than the current 22px code-log row:

- line 1: commit message (primary) and relative time;
- line 2: resolved author and short immutable revision;
- stable actions: `View version` as the row's main action and a labelled
  `Changes` button at the trailing edge.

The Changes action is never hover-only. The active revision and active Diff
revision use text/icon plus `surface-selected`, not color alone.

## URL and state model

- Historical content remains `?commit={revision}`.
- Diff is `?commit={revision}&view=diff`.
- Opening Diff pushes one history entry and preserves `location.state` so a
  Search-launched preview remains a preview.
- `Back to version` removes only `view=diff`, retaining the selected commit.
- `Back to latest` removes both `view` and `commit`.
- Browser Back returns to the exact pre-Diff URL. The document component stays
  mounted, preserving the open History tab and its scroll offset.
- On any Diff exit, focus returns to the originating History row's Changes
  control when it still exists.
- A directly opened Diff URL is valid. If its History base is not loaded, the
  comparison label says `Previous revision` rather than fabricating a hash.

`Edit` remains a document mutation in the workspace header and is unavailable
for historical/Diff mode. Diff must not be represented as an editable state.

## Frontend contract and component boundaries

### Typed API functions

Add narrow helpers in `frontend/src/lib/api.ts`:

```ts
type DocumentHistoryEntry = {
  hash: string;
  message: string;
  author: string;
  author_name?: string | null;
  date: string;
};

type DocumentDiff = {
  file: string;
  commit: string;
  type: "added" | "deleted" | "modified" | "unknown" | "unchanged";
  diff: string;
  error?: string | null;
};
```

- `getDocumentHistory(vault, path, limit)` uses `/history`.
- `getDocumentDiff(vault, path, commit)` uses `/diff`.
- Both inspect `Response.status` before generic error conversion so 404/405 can
  be classified as `unsupported` for mixed-version frontend deployments.
- Do not probe Diff while the reader loads. Fetch only after `view=diff`.

### Query ownership

- History query key: `['document-history', vault, canonicalPath]`.
- Diff query key: `['document-diff', vault, canonicalPath, revision]`.
- A Diff result is immutable and may use `staleTime: Infinity`.
- Failed background History refresh keeps the last successful ledger and adds
  inline retry feedback; it must not blank the panel.

### Components

- `DocumentHistoryList`: typed lineage list, version/change actions, active
  state, focus restoration hook.
- `DocumentDiffView`: lazy-loaded read-only workspace surface.
- `document-diff.ts`: patch normalization, parsing, row model, statistics, hunk
  index, and compatibility-state mapping as pure functions.

Use `parse-diff` for unified-patch parsing rather than maintaining a partial Git
patch grammar. It is zero-dependency and small; pin the reviewed major/minor and
exercise AKB's headerless added/deleted compatibility fixtures. Render parsed
text as React text nodes—never `dangerouslySetInnerHTML`.

## Patch compatibility

The existing endpoint has two shapes that the client must normalize:

1. modified revisions generally include unified headers and hunk headers;
2. added/deleted revisions may be headerless streams of `+` or `−` lines.

For parsing only, synthesize `/dev/null`, file, and one hunk header around the
headerless first/deleted revision. Preserve the original response for Copy.
Unknown/error responses never enter the parser.

History should try the canonical `/history` endpoint first. If an old server
returns 404/405, retain today's path-scoped activity ledger as a compatibility
fallback and mark `View changes` as server-unavailable after the first rejected
Diff request. Do not silently call the fallback for authorization or 5xx errors.

## Rendering and performance

- Lazy-load the parser and Diff component; normal Rendered/Raw reading must not
  pay their bundle or parse cost.
- Parse in `useMemo` only after a successful immutable query.
- Keep lines unwrapped by default so a row has fixed height and old/new line
  columns remain aligned. The viewer owns horizontal scrolling.
- Use the existing virtualizer above a profiled row threshold; expose
  `aria-rowcount` / `aria-rowindex` on the virtualized grid.
- Initial safety guard: do not parse or render a patch over 2 MiB or 20,000
  patch lines. Show `This change is too large to display safely` with actions to
  open the previous and selected complete versions. Treat these numbers as
  measured guardrails, not a permanent public API contract.
- Hunk navigation targets the parsed row index, so it works with virtualization.
- Do not implement custom text search at launch. Virtualization makes browser
  find incomplete, and a correct indexed search UI is disproportionate to the
  primary task. Previous/next hunk navigation covers change scanning.

The frontend guard prevents DOM lockups but does not bound transfer or backend
patch construction. If production telemetry shows frequent oversized diffs,
follow up with an additive server cap/summary contract; do not expand AKB-227
into a streaming-diff backend rewrite pre-emptively.

## Accessibility

- Diff mode has an `h2` such as `Changes in {short revision}` and a concise
  description of the comparison.
- Every visual line exposes old/new line numbers and a text change kind. The
  visible `+` / `−` marker and an accessible `Added` / `Removed` label duplicate
  the soft success/destructive tint.
- Hunk headers are named navigation targets. Previous/next controls announce
  `Change 2 of 5` in one polite live region.
- The scrollable patch has a visible label, `tabIndex=0`, preserved focus ring,
  and no keyboard trap. Arrow/Page keys keep native scrolling behavior.
- Copy, retry, previous/next, Back, and Changes controls are native buttons with
  visible labels or tooltips plus `aria-label`.
- Loading uses one layout-stable `LoadingState`; errors use `Alert`; successful
  content remains visible during background refresh.
- Light and dark semantic line fills use AKB tokens. No raw red/green hex and no
  color-only state.

## State matrix

| State | Viewer behavior | Recovery |
|---|---|---|
| loading | keep frame and toolbar geometry; line skeletons | none |
| modified | render parsed unified hunks | navigate/copy/back |
| added / first revision | label `First version`; all lines added | open version |
| unchanged / empty patch | `No content changes in this revision` | open version |
| unknown + API error | explain unavailable revision without raw server detail | return to History |
| 404/405 old server | `Changes are not supported by this server yet` | open complete version |
| 403 | access changed; do not call it unsupported | return to Vault |
| network / 5xx | keep stable error frame | Retry |
| over guard | do not parse/mount rows | open previous/current versions |

## Backend evolution (not required for launch)

An additive response may later expose:

```json
{
  "base_commit": "... or null",
  "additions": 12,
  "deletions": 4,
  "hunks": 3,
  "truncated": false
}
```

This would make deep-link comparison headers and oversized summaries exact.
Launch must not depend on these fields, because the frontend is expected to run
against older backends. Arbitrary `base` / `target` query parameters are not
part of this proposal.

## Implementation sequence

1. Add typed History/Diff helpers and pure response/patch fixtures.
2. Move the History inspector from path-scoped activity to Resource History,
   retaining only the explicit old-server fallback.
3. Redesign History rows and add the stable Changes action.
4. Add URL-backed Diff mode and route-state/focus restoration tests.
5. Add the lazy parser and token-native unified renderer.
6. Add hunk navigation, Copy, large-patch guard, and complete state matrix.
7. Run the frontend design gate and E2E against both current and simulated
   404/405 legacy contracts.

## Test plan

### Unit

- modified, added, deleted, unchanged, malformed, and headerless patches;
- additions/deletions/hunk counts and old/new line numbers;
- 404/405 compatibility versus 403/5xx/network failure;
- size/line guard before parsing;
- History entry mapping from canonical History and activity fallback.

### Component

- two actions per History row remain keyboard reachable;
- add/delete is not conveyed by color alone;
- previous/next hunk navigation scrolls and announces position;
- Copy uses the original patch;
- all loading/empty/error states retain one stable frame;
- virtualized rows expose count and position semantics.

### Route integration

- `?commit=X&view=diff` deep link;
- Back to version retains `commit=X`;
- Back to latest clears revision state;
- browser Back restores Details/History and focused trigger;
- Search preview remains open across Rendered / Raw / Diff transitions;
- Edit promotes a Search preview to the full page and never edits Diff state.

### Backend/E2E reuse

Retain the existing reader/forbidden/unauthenticated matrix for History and
Diff. Add one lineage regression proving a moved/recreated path does not leak a
different Resource's revisions into the frontend History source.

## Acceptance criteria

- A reader can open Changes for any loaded document History entry and see that
  revision against its immediate parent.
- The comparison clearly names target, base or `Previous revision`, author,
  time, message, additions, deletions, and hunks.
- Added and removed lines remain distinguishable without color.
- Loading, first revision, no changes, unsupported old server, permission loss,
  unknown revision, oversized patch, and retryable failure are distinct.
- Closing or browser Back restores the original Viewer/Search context, History
  scroll position, and action focus.
- Normal document reading does not eagerly download or parse Diff code/data.
- No raw colors, unsafe HTML, arbitrary revision comparison, restore, or merge
  behavior enters this task.
- `npm run design:check && npm run typecheck && npm run lint && npm run test`
  passes before implementation is proposed for merge.

## Implementation outcome

AKB-227 is implemented in the frontend. Document History now reads the logical
lineage contract, with an explicit 404/405-only fallback for older servers. A
labelled `Changes` action opens a route-addressable, lazy-loaded unified Diff
canvas with line numbers, change markers, hunk navigation, patch copy, bounded
parsing, and virtualized long output. Returning to the selected version restores
the History panel and initiating control; Search preview route state is retained.

The implementation includes API compatibility tests, parser/guard tests,
component state tests, and route-level History/Diff/focus tests. The complete
frontend design, type, lint, test, and production build gates pass. No backend
schema or endpoint change is required.
