# Graph Viewer Redesign — Design Spec

> Brainstormed 2026-05-13. Replaces the current 951-line `frontend/src/pages/graph.tsx` (cytoscape + fcose + cola) with a 3-column page built on `react-force-graph-2d`, exposing the backend's relations / drill-down / provenance / history APIs through a dedicated right detail panel.

## Goal

Make the AKB graph page feel as fluid as the reference viewer in `/Users/kwoo2/Desktop/storage/seahorse-graph` (Canvas + d3-force) while preserving the AKB visual tone (mono / [10px] uppercase / border / no-rounding). Surface backend graph capabilities (`/graph`, `/relations`, `/drill-down`, `/provenance`, `/history`) directly in the page so the user can explore without leaving it.

## Non-goals

- No new backend endpoints. Everything composes from existing routes.
- No collaboration / shared-saved-views (backend-stored). URL + localStorage covers the present need; backend `saved_graph_views` is a deliberate later step.
- No graph editing (create/delete edges from the UI). Read-only viewer.
- No timeline / animation playback over versions.

## High-level architecture

3-column page layout below the existing TitleBar. Detail panel only mounts when a node is selected.

```
TitleBar (h-9, existing)
─────────────────────────────────────────────────────
[ 240px sidebar | 1fr canvas | 320px detail (conditional) ]
       ▲              ▲                ▲
   GraphSidebar   GraphCanvas      GraphDetailPanel
                  (full-bleed,
                   Canvas-rendered)
```

- Sidebar and detail panel both collapsible via `PanelLeftClose` / `PanelRightClose` icons in the canvas corner.
- Canvas is full-bleed; no inner scroll, no padding. Top-left holds a small control cluster: freeze/unfreeze simulation, fit-to-view, zoom in/out.
- Width is not user-resizable (KISS).

## File structure

```
frontend/src/
  pages/graph.tsx                       — page shell + state plumbing  (~150 lines, rewrite)
  components/graph/
    GraphCanvas.tsx                     — react-force-graph-2d wrapper, custom canvas paint  (~250 lines)
    GraphSidebar.tsx                    — entry search · depth · type/relation filters · recent · saved  (~200 lines)
    GraphDetailPanel.tsx                — node detail with lazy fetch sections  (~250 lines)
    graph-state.ts                      — URL ↔ GraphView codec, default trimming  (~150 lines)
    graph-types.ts                      — Node/Edge/View/RelationKind + visual tokens  (~80 lines)
    use-graph-data.ts                   — hybrid fetch (full vs neighborhood), BFS expand, dedup merge  (~150 lines)
  hooks/use-graph-history.ts            — localStorage recent + named saves  (~80 lines)
```

Each unit has one responsibility. Page shell only owns URL state and child plumbing; data layer is isolated in `use-graph-data.ts`; visual tokens (colors, dash patterns, sizes) live in `graph-types.ts` so the renderer doesn't bake them in.

## Tech stack

- **New dep:** `react-force-graph-2d` (~80 KB gzipped, Canvas + d3-force).
- **Removed deps:** `cytoscape`, `cytoscape-fcose`, `cytoscape-cola`.
- React 19 + TypeScript (existing).
- TanStack Query for fetch caching (existing).
- Tailwind v4 + AKB design tokens (existing).

## Data model

```ts
type RelationKind =
  | "depends_on" | "implements"
  | "references" | "related_to"
  | "attached_to" | "derived_from";

type NodeKind = "document" | "table" | "file";

interface GraphNode {
  uri: string;          // akb://{vault}/{kind}/{path-or-id} — primary key
  name: string;         // display label
  kind: NodeKind;
  // From browse/get metadata. Only what canvas paint or detail panel reads.
  doc_id?: string;
  doc_type?: string;
  // Layout state owned by the simulation, set by the canvas wrapper:
  x?: number; y?: number; vx?: number; vy?: number; fx?: number; fy?: number;
}

interface GraphEdge {
  source: string;       // uri
  target: string;       // uri
  relation: RelationKind;
}

interface GraphView {
  entry?: string;                       // doc_id; absence = full mode
  depth: 1 | 2 | 3;                     // applies only when entry is set; default 2
  types: Set<NodeKind>;                 // default = all three
  relations: Set<RelationKind>;         // default = all six
  selected?: string;                    // selected node URI; mirrors detail panel state
}
```

## URL ↔ state contract

URL is the canonical representation of `GraphView`. Reload, share, and bookmark all round-trip.

- `?entry=<doc_id>` — set entry point (e.g. `d-94d8657f`)
- `?depth=1|2|3` — hop depth in neighborhood mode
- `?types=document,table,file` — node-type filter
- `?rel=depends_on,implements,references,related_to,attached_to,derived_from` — relation filter
- `?sel=<uri>` — selected node URI (URL-encoded)

**Default trimming:** if a field equals its default, it is omitted from the URL. The query string never includes the full default set; this keeps share-links short.

Codec lives in `graph-state.ts`:

```ts
export function viewToQuery(v: GraphView): string;
export function queryToView(q: URLSearchParams): GraphView;
```

Roundtrip tested with unit tests.

## Fetch strategy (hybrid)

`use-graph-data.ts` orchestrates three flows:

| Mode | Trigger | API call | Behavior |
|---|---|---|---|
| **Full** | `entry` absent | `GET /graph/{vault}` ×1 | Load all nodes + edges; client applies type/relation filters. |
| **Neighborhood** | `entry` present | `GET /relations/{vault}/{entry}` → BFS frontier for depth−1 more hops | Seed expands hop-by-hop. Each hop is one `/relations` call per new node. React Query caches per-node. |
| **Auto-expand (click)** | Click on any node | `GET /relations/{vault}/{clicked}` 1-hop | Merge response into the active graph (dedup by URI). Simulation absorbs new nodes smoothly. No-op if that node has already been expanded in this session. |

**Cache key:** `["graph", vault, mode, entry?, depth?]` for the top-level fetch; `["relations", vault, doc_id]` for per-node 1-hop. React Query dedups identical in-flight calls.

**Concurrency:** BFS hops fire all their per-node `/relations` calls in parallel without an explicit cap. React Query dedup is the only governor. If real traffic shows browser-level connection limits hurting UX, add a per-hop concurrency cap as a follow-up.

**Degrade rule:** in Full mode, if response has `> 500` nodes, the canvas does not run the simulation. The sidebar surfaces a banner "Pick an entry point to explore" and the entry-search input is auto-focused (no keyboard shortcut — programmatic focus on render).

The threshold is evaluated against the **raw response node count**, not the filtered visible count. Filtering the visible set below 500 does **not** auto-resume the simulation — once degraded, the user must pick an entry point or reload. (Rationale: filter state oscillates; resuming/pausing on every toggle would feel jittery.)

## Interaction model

| Action | Result |
|---|---|
| Click node | Select + open detail panel + auto-fetch its 1-hop neighbors (if not already expanded). Updates URL `?sel=...`. |
| Double-click node | `navigate(/vault/{v}/doc/{path})` (or `/table/{name}`, `/file/{id}`). |
| Drag node | Temporary reposition; release returns to simulation. **Shift+drag = pin** (`fx`/`fy` set; node stays put until unpinned). |
| Hover node | Tooltip near cursor with `title · TYPE · depth from entry`. |
| Right-click node | Context menu: Pin / Unpin / Hide / Copy URI / Open in new tab (`window.open(<same route as double-click>, "_blank")`). |
| Click empty canvas | Clear selection (close detail panel; remove `?sel`). |
| Scroll wheel | Zoom in/out. |

No keyboard shortcuts. Context menu closes on outside-click (no Esc handler).

### Canvas controls (top-left, small)

```
[⏸ freeze]  [⌖ fit]  [− zoom out]  [＋ zoom in]
```

- **freeze**: toggles `alphaTarget=0` to pause physics; click again to resume.
- **fit**: camera fits all visible (non-filtered) nodes.
- **zoom buttons**: ± for trackpad-less users.

## Visual encoding

All custom-painted via `nodeCanvasObject` / `linkCanvasObject` so AKB tone (mono / no-rounding / border-heavy) is enforced and not inherited from the library defaults.

### Nodes

| Kind | Shape | Fill | Border |
|---|---|---|---|
| document | 16×16 square | `surface-muted` | `foreground`, 1.5 px |
| table | 16×16 square | `surface` | `accent`, 1.5 px |
| file | 16×16 square | transparent | `foreground-muted`, 1 px dashed |
| **selected** | (same) | (same) | `accent`, 2.5 px + outer glow |
| **pinned** | (same) + small ▣ marker | | |
| **filtered out** | opacity 0.15 | | |

Labels: mono [10px] uppercase below the node, truncated at 16 chars with ellipsis. Below a zoom threshold (~0.5×), labels hide to reduce clutter.

### Edges

| Relation | Stroke | Color |
|---|---|---|
| `depends_on` | solid 1.5 px | `foreground` |
| `implements` | solid 1.5 px | `accent` |
| `references` | dashed [4, 4] | `foreground-muted` |
| `related_to` | dashed [2, 2] | `foreground-muted` |
| `attached_to` | solid 1 px | `foreground-muted` |
| `derived_from` | dashed [6, 2] | `accent` |

Direction marker: small ▶ painted at the target end. Hover thickens stroke by 1 px and brightens color.

### Theme sync

`useTheme()` (existing at `frontend/src/hooks/use-theme.ts`) triggers a `getComputedStyle(document.documentElement)` re-read on theme change. The visual-token resolver returns the resolved hex values, which the canvas paint functions pull on each frame. (Same approach as the current cytoscape implementation — proven.)

## Left sidebar (240 px)

Vertical stack; each section separated by `border-b border-border`. Labels are `coord` (mono [10px] uppercase, foreground-muted).

```
┌──────────────────────────────┐
│ § ENTRY POINT                │
│ [🔍 _______________________] │   debounce 300 ms, calls /search
│  ▸ doc title           type  │   max 8 results
│  ▸ doc title           type  │
├──────────────────────────────┤
│ § DEPTH                      │
│ ◯ 1   ◉ 2   ◯ 3              │   radio, disabled when no entry
├──────────────────────────────┤
│ § TYPES                      │
│ ☑ DOCUMENT  ☑ TABLE  ☑ FILE  │   Badge variant=outline toggle
├──────────────────────────────┤
│ § RELATIONS                  │
│ ☑ depends_on    ☑ implements │   6 toggles, color matches edge encoding
│ ☑ references    ☑ related_to │
│ ☑ attached_to   ☑ derived    │
├──────────────────────────────┤
│ § RECENT       [clear]       │
│  ‹ entry 1 (title)           │   localStorage, max 5
│  ‹ entry 2 (title)           │
├──────────────────────────────┤
│ § SAVED VIEWS  [+ save]      │
│  ★ view name 1         [×]   │   localStorage, named views
│  ★ view name 2         [×]   │
└──────────────────────────────┘
```

### Behavior

- **Entry input:** Clearing it removes `?entry` → page snaps to Full mode. Selecting a result sets `?entry=<doc_id>&depth=2`.
- **Depth radio:** Disabled (visually muted) when no entry is set. Changing it updates `?depth=N` immediately and re-fetches.
- **Types / Relations toggles:** Click a Badge to toggle. Only the *changes* from default are encoded in the URL (default-all sends nothing).
- **Recent:** Auto-pushed when entry changes. Dedup. Max 5. `[clear]` empties localStorage and the list.
- **Saved views:** `[+ save]` prompts for a name (small inline input, not a modal), then stores `{ name, url }` in localStorage. Clicking a saved view `navigate()`s to its URL. `[×]` removes it. **Duplicate names overwrite** the existing entry (no separate "already exists" branch — the simplest UX, the user gets the latest snapshot for that name).

### localStorage keys

- `akb-graph-recent:{vault}` — array of `{ doc_id, title }`, max 5
- `akb-graph-saves:{vault}` — array of `{ name, url }`, max 20

Eviction: oldest first on overflow. On localStorage quota error, surface a toast and skip the write.

## Right detail panel (320 px, conditional)

Mounted only when `selected` (i.e. `?sel`) is set.

```
┌─────────────────────────────────────┐
│ [×] close                           │
│                                     │
│ § DOCUMENT · D-94D8657F             │   coord: type · doc_id
│ ───────────────────────────────────│
│ Title in serif                      │
│ collection/path/here                │   coord-muted
│                                     │
│ [Open document] [Copy URI] [Pin]    │   Button row, h-7
│                                     │
│ § SUMMARY                           │
│ Summary text from akb_get metadata. │
│                                     │
│ § TAGS                              │
│ [tag1] [tag2] [tag3]                │   Badge variant=outline
│                                     │
│ § RELATIONS  [12]                   │
│ ─ depends_on (3) ────────────       │
│   → target doc title          [⌖]   │   click row = select that node
│   → target doc title          [⌖]   │   [⌖] = camera-fit only
│ ─ references (2) ────────────       │
│   ← source doc title          [⌖]   │
│                                     │
│ § PREVIEW  [show sections ▾]        │
│ ─────────────────────────────────── │
│ First 40 lines of doc.content       │   font-mono [11px], whitespace-pre-wrap
│ ...                                 │
│ [more →]                            │   → /vault/{v}/doc/{path}
│                                     │
│ § META (collapsible)                │
│   author · created · updated        │
│   provenance: source                │   /provenance, lazy
└─────────────────────────────────────┘
```

### Lazy fetch sections

| Section | Endpoint | Fires when |
|---|---|---|
| Header / summary / tags | `GET /documents/{vault}/{doc_id}` | Selection changes |
| Relations | `GET /relations/{vault}/{doc_id}` | Selection changes (shared with auto-expand cache) |
| Preview | First 40 lines of content (from the `documents` response) | Selection changes |
| Sections | `GET /drill-down/{vault}/{doc_id}` | "show sections ▾" clicked |
| Provenance | `GET /provenance/{doc_id}` | META section expanded |

(History is intentionally out of scope here — there is no per-document REST history endpoint today, only vault-level `/activity/{vault}`. Adding a document-level history view is a separate feature.)

### Table / File node variants

- **Table:** Replace Summary with column list (from `akb_browse` metadata). No Preview. "Open" → `/vault/{v}/table/{name}`.
- **File:** Show `mime_type` and `size_bytes`. No Preview (binary). "Open" → `/vault/{v}/file/{id}`.

### Row click vs `[⌖]`

- **Row click** (relation entry): change `?sel` to that node. Detail panel re-renders with the new node. Graph camera pans to center on it. Simulates "drill through the graph from the panel."
- **`[⌖]` button**: camera fit only; selection unchanged. For locating a node visually when the graph is dense without losing the current panel context.

## Migration plan

Bite-sized, TDD-friendly. Each step ends in a green test run before moving on.

1. **Foundation**
   - Create `graph-types.ts` with `GraphView` / `GraphNode` / `GraphEdge` / `RelationKind`.
   - Create `graph-state.ts` with `viewToQuery` / `queryToView`. Unit-test roundtrip + default trimming.
   - Create `hooks/use-graph-history.ts`. Unit-test recent push (dedup, max-5) + saved-view CRUD + quota error handling.

2. **Data layer**
   - Create `use-graph-data.ts`. Implement Full mode, Neighborhood mode (BFS), and click-expand merge. Mock `fetch` (or `lib/api`) in tests. Cover dedup on merge, visited-set on expand, depth boundary, and `> 500` degrade flag.

3. **Canvas**
   - `GraphCanvas.tsx` renders `react-force-graph-2d` with custom paint. No tests beyond "renders without throwing"; visual correctness is manual.
   - Wire freeze/fit/zoom controls.

4. **Sidebar**
   - `GraphSidebar.tsx` reads `GraphView`, emits `(next: GraphView) => void`. Test: entry search → debounce → result list, type/relation toggles emit the right diff, recent push on entry change, saved-view save/load/delete.

5. **Detail panel**
   - `GraphDetailPanel.tsx` with the section/lazy-fetch matrix. Test: selection change triggers immediate-fetch sections; collapsible META defers fetches until expanded; table/file variants render the right sections.

6. **Page integration**
   - Rewrite `pages/graph.tsx` (≤ 150 lines): own URL ↔ view sync, hold `selected`, render the 3-col grid, plumb props.

7. **Cleanup**
   - Delete the old `graph.tsx` body in the same commit that lands the integration.
   - Run `grep -r "cytoscape" frontend/src` to confirm zero references.
   - Remove `cytoscape`, `cytoscape-fcose`, `cytoscape-cola` from `package.json`.
   - Run `npm install` and confirm `package-lock.json` shrinks. Confirm `vite build` succeeds and bundle size dropped.

## Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Full mode on a large vault (> 1000 nodes) drops frames | Janky simulation, poor first impression | `> 500` degrade rule (above). Sidebar banner + auto-focus entry input. |
| Theme switch leaves stale canvas colors | Wrong palette on toggle | `useTheme()` dependency re-reads computed styles and triggers a paint pass. |
| Auto-expand on every click floods network | Spike on rapid clicking | React Query dedup + per-session visited-set; no debounce (user expects responsiveness). |
| URL grows long with verbose filter combos | Broken bookmarks at platform limits | Default-trimming codec. Verified by unit test. |
| localStorage quota exceeded (rare but possible) | Silent loss / thrown error | Catch and toast; cap saved views at 20 with oldest-first eviction. |
| Stray cytoscape import elsewhere | Build fails on cleanup | Pre-flight grep before deletion; cleanup PR can be split if needed. |

## Test plan

- **Unit (Vitest):** URL codec roundtrip; localStorage hooks (recent, saved views, quota); BFS expand algorithm; filter applicator pure-function.
- **Component (Vitest + Testing Library):** `GraphSidebar` (entry input → URL; toggles → diff; recent push); `GraphDetailPanel` (selection → fetch fires; META expand → lazy fetch; table/file variants).
- **Integration (manual):** dev server, two vaults — `gnu-weekly` (small, Full mode), `seahorse-kb` (large, Full → degrade → Neighborhood). Verify auto-expand, pin, saved view roundtrip.
- **E2E:** none added. The backend graph/relations endpoints are already covered by existing e2e suites; this work is frontend-only.

## Open questions

None remaining from the brainstorming session. All design decisions A–E in the chat are recorded above.

## Out of scope (deliberately deferred)

- Backend-stored shared saved views (`saved_graph_views` table). The URL representation is forward-compatible — when the table is added later, server-stored views become `(name, url)` rows that paste straight into the same flow.
- Path-finding ("show me the route from A to B").
- Cluster / community detection visualization.
- Annotations / per-node user notes inside the viewer.
