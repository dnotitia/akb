---
status: accepted
stage: implemented
created: 2026-09-14
updated: 2026-09-14
---

# Home workspace summary — context without dashboard cards

## Implemented decision

Restore workspace-scale context as one quiet, cardless summary row at the top
of Home's content. Do not restore four statistic cards, a right-hand summary
rail, or another Home title. Indexing remains exclusively in the compact global
and Vault-specific badges described in the
[accepted indexing design](../2026-09-14-workspace-search-status/README.md).

Illustrative values only, not measurements of the local account:

```text
App header: Home                              [indexing] [Search] [Bell] [Profile]

8 Vaults · 1,284 Documents · 12 Tables · 86 Files

Recently viewed
[Document]          [Document]          [Document]          [Document]

Your vaults                                                   [View all vaults →]
[Vault]             [Vault]             [Vault]             [Vault]

Recent updates                         Watched documents
[Existing ledger]                      [Existing ledger]

                                                [Connect an agent ↗]  (floating)
```

Recently viewed still disappears when there is no accessible browser history.
The summary has no visible `Available to you` heading; its accessible description
retains the current-access scope without adding another visual label.
The connection entry moves out of section headers into the Home-only floating
invitation described below. The summary does not become another setup,
creation, filter, or navigation toolbar.

## Connection invitation — revised after discoverability feedback

Replace the top guide and first-section connection action, rather than adding a
third entry beside them. The invitation is separate from workspace inventory:
a labelled launcher at the bottom right, with a small introductory panel for
eligible first-time users. It is not a chatbot, notification, or connection
health monitor.

```text
┌──────────────────────────────────────┐
│ [Plug icon] Use AKB in your AI tools ×│
│ Search your vaults from your AI tool. │
│                                      │
│ [Connect an agent →]                  │
└──────────────────────────────────────┘

After minimizing:      [Plug icon  Connect an agent ↗]
```

### Presentation and placement

- Home only. Keep Settings → Agent connections as the durable management route;
  do not introduce floating chrome into editors, Vault pages, or public views.
- Mount the floating region in a body portal, outside animated route ancestors.
  A fixed descendant of the page animation could otherwise sit at the document's
  bottom and disappear below the viewport. Verify actual viewport intersection
  and stable coordinates before/after scrolling, not CSS visibility alone.
- Desktop: anchor to the viewport's bottom/right edges with 24px offsets, not
  to the header or a particular update column. Expanded panel is approximately
  304px wide with natural content height; compact launcher is text-sized and at
  least 44px high. Do not allocate a full right-hand content rail.
- Use the existing opaque `surface`, `foreground`, `foreground-muted`, `border`,
  `link`, and `primary` tokens, `radius-md`, and `shadow-md`. Light mode stays
  paper-white; dark mode uses the existing slate surface and a visible boundary.
  Title 14px semibold, explanation 13–14px, action 14px medium. One small plug
  glyph identifies configuration, not a speech bubble, avatar, or fake status dot.
- Teal action and a restrained tinted icon are sufficient emphasis. Do not add
  orange, a gradient banner, tool-logo parade, unread badge, pulsing animation,
  or progress count. Create a vault retains the marquee orange action.
- Small screens and short landscape viewports start with the compact labelled
  launcher only. Use 16px plus safe-area offsets; never place a desktop-sized
  automatic panel over a mobile update list. Browser keyboard/zoom must not trap
  content beneath it.
- Reserve only compact-launcher clearance at the end of Home's scroll area so
  the final content can be scrolled above it. An expanded invitation must collapse
  if it would obscure another keyboard-focused control; padding alone cannot
  protect intermediate rows. Closing it must always leave the body usable.

### Visibility rules, not invented connection status

| Observed state | Invitation |
| --- | --- |
| Verified empty PAT list, at least one accessible Vault, available setup, no prior dismissal | One initial expanded invitation on desktop; compact on small/short screens |
| No Vaults yet | Compact launcher; do not compete with Create your first vault |
| PAT exists, guide previously dismissed, or setup previously opened | Compact launcher, no automatic expansion |
| PAT/auth lookup loading, failed, or unsupported | Neutral compact entry; never an incomplete/failed connection warning |

An empty PAT list is only an eligibility hint, not proof that no tool is
connected: OAuth users may already be connected without a PAT. Copy therefore
describes an optional capability, never "Finish setup", "Not connected", or
"0 agents". Token creation likewise does not prove successful external use.

Record initial presentation/minimization per account and browser, honor the
existing `akb.homeConnectionGuideDismissed:<userId>` preference, and tolerate
blocked storage. Within a session, navigation and refresh callbacks must not
repeatedly expand the hint. If persistent storage is unavailable, cross-reload
suppression cannot be guaranteed; do not imply cross-device synchronization.

### Interaction and integration

- The panel's X minimizes it and focuses the compact launcher. Its accessible
  name is "Minimize connection guide"; it is not a destructive close action.
- Both the panel CTA and compact launcher directly open the existing
  [QuickstartDialog](../../../../frontend/src/components/quickstart-dialog.tsx).
  Do not add an intermediate menu, another setup wizard, or an automatic modal.
- Preserve the shared three-phase `ConnectionSetup`, supported PAT/OAuth paths,
  read-only test prompt, and unsaved-secret confirmation. Token creation may
  suppress a future hint, but must never close the wizard or discard the newly
  displayed secret. Do not equate closing setup with successful connection.
- Use `z-sticky`, below global header and modal overlays. Suspend floating chrome
  while search, notifications, document preview, vault creation, or setup owns
  a modal layer. In particular, notifications use a transparent desktop overlay:
  a lower z-index alone would leave the invitation visually visible underneath.
  Prefer shared overlay state over brittle DOM polling. Restore focus to the
  launcher only after its setup closes; do not override another modal's return
  focus or a navigation destination.
- Semantic buttons, visible focus, adequate hit areas, and reduced-motion
  support are required. The invitation is a labelled nonmodal complementary
  region, not `role=alert`, a live region, or a focus trap. Appearance never
  steals focus. No new bottom-right toast stack is needed.
- No new backend contract is needed for this invitation. Existing data loading
  must remain account-scoped, cancellation-safe, and tolerant of older servers.

The targeted UI/UX skill lookup supported skippable onboarding and consistent
help placement. Exact dimensions, the two visual states, and Home-only scope
are project-specific design decisions, not externally validated usability
results. Populated and empty Home states were checked with browser fixtures,
including compact and wide layouts, light/dark themes, and the setup flow.

## Presentation

- Same left/right content inset as the first Home section; never full-bleed and
  never aligned to the viewport rather than the workspace. No shell changes.
- Natural single-line desktop layout, approximately 40–48px with padding, not a
  fixed-height card. Leave 16–20px before the following section. Do not inherit
  the full 28–32px inter-section gap in addition to another wrapper's padding.
- Existing surface background, no independent fill, shadow, enclosing border,
  bottom rule, oversized number, gradient, animation, or colored icon tile.
- Pretendard: 12px muted scope label, 14px metric labels, 16px medium/semibold
  tabular values. Existing foreground/muted tokens serve light and dark modes.
  Teal stays for actual links; these statistics remain neutral, passive text.
- Keep each value and its label together. Use 20–24px between metric groups;
  separators are decorative. Mobile stacks the scope label above a compact
  two-column group, without horizontal scrolling or shrinking labels.
- Use a labelled section with a semantic description list; no new visible H1
  or landmark-sized heading. One polite loading message, no four competing live
  regions, hover-only definitions, or fake clickable statistics.

## Information ownership

| Information | Location / rule |
| --- | --- |
| Overall accessible Vault / document / table / file counts | Summary row |
| A particular Vault's counts | Its existing Home card; preserve them |
| Directory navigation | Your vaults → View all vaults; remove only the repeated `(N)` suffix |
| Pending indexing | Existing global and scoped badges; never include in inventory totals |
| Collections, members, access-role breakdowns, total table rows | Omit from Home summary |
| PATs, setup progress, agent connection health | Existing optional connection flow, not inventory |

The row means **accessible to the signed-in account**, not owned, favorited, or
limited to four preview cards. It follows the accessible Vault directory,
including shared/public access and the system-admin branch. Keep archived
Vaults/documents in scope to match current directory and resource counts; do not
silently label this as only active content. Guide documents are not subtracted
as a client heuristic. Tables count table resources, not their data rows.
Files use the existing confirmed-file predicate; deleted resources do not count.

## Evidence and data design

[Home](../../../../frontend/src/pages/home.tsx) currently requests
`/api/v1/my/vaults`, then enriches **at most four preview Vaults** with `/info`.
Summing its `metrics` object is therefore not a workspace total. The
[accessible directory](../../../../backend/app/services/access_service.py)
returns names/roles and metadata, but no document/table/file counts.

Calling `/vaults/{name}/info` for every Vault is not the recommended solution:
that endpoint also reads ownership, access, activity and graph counts, plus
table schemas, full table row counts and sample rows. A four-number Home summary
should not multiply this detail work across a large account.

Implementation uses one additive, authenticated counts-only read:
`GET /api/v1/my/workspace-summary`. It resolves the same current readable Vault
set as the directory before aggregation, reusing its access policy rather than
inventing a second ACL rule.
Count only within those Vault IDs and the current document authority through
[document_counters](../../../../backend/app/services/document_counters.py).
Do not use instance-wide `/stats`, `/health`, cached admin totals, table samples,
search chunks, or browser-side document enumeration.

It returns only version, observation time, explicit accessible scope and optional
`vault_count`, `document_count`, `table_count`, `file_count` integers. Directory
and set-based counts use one read-only repeatable-read transaction. The response
is private/no-store. No stored counters or migration are needed.

The client owns one account/scoped-directory/revision query while Home is visible,
cancels obsolete requests, and revalidates after shell foreground identity proof,
including unchanged local identities. It refreshes the directory before totals;
checking and failed scope never expose cached private values. New Home visits
also revalidate, with an 8-second request ceiling. Short-lived in-memory query
state is not used as proof of current access. Do not attach this inventory to the
15-second indexing loop or persist private totals in browser storage.

## Loading, compatibility and failure

- Reserve the compact row geometry while loading, with inline skeleton values.
  Never flash zeros or count up through partially loaded Vaults.
- A supported field is displayed only when it is a valid complete-scope,
  nonnegative safe integer. Missing/null/invalid data displays `—`, not `0`.
- An older backend can still supply an exact Vault total from the successful
  directory response. Other totals remain `—` with one concise `Totals unavailable`
  explanation; do not fetch every Vault detail as a compatibility workaround.
- Never claim a preview subtotal is a complete resource count. Individual
  numeric fields may remain visible if their completeness is independently known.
- An initial error must not become an empty workspace. Keep the existing Vault
  list error/retry flow; summary failures never block opening documents or Vaults.
- Following permission/session invalidation, hide previous private totals until
  verification succeeds. A failed refresh must not present stale values as current.
- When the verified directory has no Vaults, keep only `0 Vaults`; the existing
  Create your first vault empty state remains the primary message, not four zeros.

## Implementation and acceptance

Implemented through `HomeWorkspaceSummary`, `HomeConnectionInvitation`, the
count-only backend service, shared modal visibility and the shell's access-proof
revision. Home's design-system rules now match this structure. Search-status
observations remain separate; no database migration or production changes.

Required checks: 4/20/100-Vault accounts; duplicate access bases; reader/public/
admin/archived scopes; bare-Git and Native document authority; confirmed versus
staged files; old-server and partial fields; identity/revocation races; no `/info`
fan-out; 375/768/1440/2560 layouts in both themes and zoom/reduced-motion checks.
Existing card navigation, connection setup behavior, and both update columns
must remain unchanged. Add invitation checks for prior dismissal, OAuth without
PATs, unknown auth state, account changes, blocked storage, initial presentation,
keyboard focus occlusion, transparent notification overlays, and preservation of
new secrets. Backend tests must prove counts never include inaccessible resources.
