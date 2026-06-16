# Graph viewer v2 — knowledge-base graph, re-thought

Status: **accepted (Phase 1 in progress)** · Scope: frontend renderer + interaction
rebuild (Phase 1); backend BFS / overview endpoint / in-graph relation editing
(Phase 2).

## Problem

The graph page works but reads as a *demo*, not a daily tool. Concretely
(`pages/graph.tsx`, `components/graph/GraphCanvas.tsx`):

- **Global-first default.** A bare `/graph` dumps the whole vault — which past
  ~200 nodes collapses into the "hairball" (every reference says the same: a
  global force graph is beautiful in a screenshot and useless in practice).
- **Labels vanish at overview zoom** (`LABEL_ALL_ZOOM = 1.2`): below it only the
  hovered/selected node is named, so the overview is a smear of glyph squares.
- **Fragile interactions**: click/double-click via a 250 ms timer; the context
  menu is a no-op `TODO`; shift-drag pin is unimplemented; selection re-fits.
- **Neighborhood fetch** loops `getRelations` per frontier node per hop on the
  client instead of using the backend BFS.
- **Accessibility** is a parallel `sr-only` top-50 list with no edge traversal.

## Research synthesis (6 angles)

A multi-agent research pass (current-impl audit + reference UX + 2D-vs-3D +
libraries + interaction/IA + AKB fit/perf) converged hard:

1. **2D, not 3D.** 3D's only real win is "wow"/marketing — the axis explicitly
   deprioritized. On a flat display 3D forfeits its theoretical wins (which need
   stereo/VR) while losing label legibility, orientation, click accuracy, and
   accessibility (Obsidian ships both modes; the community treats 3D as
   demo-candy). **3D is deferred to an optional easter-egg at most.**
2. **Local-first beats global-first.** The single most-copied, highest-value
   interaction across Obsidian/Logseq/Roam/Foam/Bloom/Kumu is the *local graph*:
   "graph of the thing I'm looking at, N hops out," grown ring by ring. Global is
   a secondary *overview/diagnostic* mode, not the landing experience.
3. **Keep the data/state layer; replace the renderer + interaction model.** The
   current ~3500 LOC is well-architected. Reusable as-is: `graph-types`,
   `graph-state` (URL codec), `use-graph-history`, the pure transforms in
   `use-graph-data` (`mergeGraph`/`applyFilters`/`docIdFromUri`), `cluster`
   (`groupOf`/`groupColor`), `GraphSidebar`, `GraphDetailPanel`. Replace only
   `GraphCanvas` + the cluster forces, behind the existing prop contract
   (`nodes/edges/selected/pinned/hidden/onSelect/onContextMenu` + the
   `centerOnNode` handle).
4. **Library: keep `react-force-graph-2d` (PATH A).** At AKB's real scale (tens
   to low-thousands of nodes/vault) every option clears 60fps; the owner's
   complaint is UX/architecture, not perf. Canvas keeps the Tailwind-token
   theming + `design:check` color governance that a WebGL swap (sigma.js) would
   forfeit. Sigma/graphology stays a documented future upgrade if raw node count
   ever dominates.

## Decisions

| Axis | Decision |
|---|---|
| Dimensionality | **2D** daily-driver; 3D deferred (optional, data-layer-reuse only) |
| Library | **Keep `react-force-graph-2d`** (Canvas, MIT, token-themed); sigma.js = future upgrade |
| Default view | **Local/ego graph** when seeded from a doc; whole-vault is an explicit "Show whole graph" escalation with hairball mitigations |
| Architecture | Reuse data/state layer; rebuild `GraphCanvas` + interaction model only |
| Rollout | **Phase 1 frontend-only** (ship + deploy), **Phase 2 backend** |

## Must-have interaction set (renderer-agnostic)

1. **Local graph default** — seeded on the current/entered doc, N hops out.
2. **Depth/hops control** — grow the neighborhood ring by ring.
3. **Hover → highlight neighbors + dim the rest**; **click → select** (no
   relayout) → detail panel.
4. **Double-click → expand neighborhood** (stop overloading it for navigate —
   open-doc moves to context menu / Enter / detail-panel button).
5. **Right-click → context menu** — Open · Open in new tab · Expand neighbors ·
   Pin/Unpin · Hide · Copy URI · Focus here (finish the existing `TODO`).
6. **Drag → pin** (fix position); pinned nodes show a marker and survive relayout.
7. **Search → focus/isolate** (temporary dim, Esc restores) kept distinct from
   **filter** (persistent query).
8. **Typed-edge encoding** — color + directional arrows + solid (structural:
   `depends_on`/`implements`/`attached_to`) vs dashed (associative:
   `references`/`related_to`/`derived_from`/`links_to`) — already in
   `RELATION_DASH`.
9. **LOD labels with collision avoidance** — at overview zoom, label the
   high-degree nodes first and skip overlaps, instead of all-or-nothing.
10. **Legend** — node kinds, edge relation styles, cluster colors.
11. **Accessibility** — a real (not `sr-only`-only) text/list alternative +
    keyboard node traversal (focus a node, arrow to neighbor, Enter to expand,
    Space to open). The list/table is the primary AT path; arrow-nav is an
    enhancement.

## Killer use-cases the redesign must nail

- **(a) Impact analysis** *(Phase 1)* — from any doc, an instant typed local
  graph with `depends_on`/`implements` highlighted: "what does this depend on,
  and what depends on it," used before editing/deleting.
- **(b) Orphans & hubs** *(Phase 2, needs server degree-ranking)* — a whole-vault
  overview ranked by degree flagging zero-degree (undiscoverable) docs and
  over-connected hubs → a KB-health audit.
- **(c) Promote implicit links** *(Phase 2, needs `akb_link` over REST)* — body
  wikilinks surface as dashed `links_to` edges, one-click upgradable to typed
  relations.

## Phase split

- **Phase 1 (frontend-only, this branch `feat/graph-viewer-v2`):** rebuild
  `GraphCanvas` + interaction model on the existing data layer — local-first
  default, real click/dblclick/right-click context menu, hover-neighbor
  highlight + dim, drag-to-pin, LOD collision labels, legend, first-class
  keyboard/list a11y, impact highlighting (a). No backend change; reuses the
  existing `/graph?center=…&hops=…` and the data-layer hooks.
- **Phase 2 (backend):** route neighborhood through the server BFS; add a
  degree-ranked top-K vault-overview endpoint (with honest "showing N of M");
  expose `akb_link`/`akb_unlink` over REST for drag-to-connect / retype / unlink
  in the graph; killer use-cases (b) and (c).

## Risks / guardrails

- Keep `uri` as the canonical node id; preserve the freshen-edges discipline in
  `mergeGraph` (a node-mutating renderer must not silently drop edges).
- A theme change must re-read the CSS color tokens (`readColors()` on `theme`).
- Selection must never reheat the layout (keep the `structureKey` remount split).
- Whole-vault mode must ship collection-cluster collapse + a degree cap together,
  or it reintroduces the hairball.
- Don't over-invest in physics/aesthetics over the high-value loop (anchor +
  depth + hover-highlight + expand + search-isolate).
