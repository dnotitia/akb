# Graph workspace implementation

Status: implemented; pending review
Updated: 2026-09-29

## Rendering and layout

The graph uses Three.js/WebGL2 perspective rendering, OrbitControls and an
off-thread d3-force-3d simulation. Spatial exploration retains real x/y/z
coordinates and orbit, pan and zoom, without shaded 3D objects or automatic
camera rotation. The renderer is lazy-loaded outside the initial workspace
bundle and replaces react-force-graph-2d.

Small unlit camera-facing circles, squares and diamonds distinguish documents,
tables and files. Kind colors use shared tokens; a separate selection ring
preserves each resource's identity. Screen-sized marks remain bounded when a
single resource is fitted or zoomed. Plain projected labels prioritize selected,
hovered, related and pinned resources, with a maximum of 30 visible labels.
Suppressing an overlapping label never removes its graph node.

Deterministic volumetric seeds feed a bounded 200-tick worker simulation.
Expansion seeds new resources near their actual neighbors while keeping
existing coordinates fixed. Selection, filtering and theme changes preserve
the layout. Request generations isolate stale responses; unmount releases GPU
objects, controls, listeners, workers and timers.

## Controls and inspection

- Floating tools separate search, Graph/List selection, filters and secondary
  actions. A labelled kind legend acts as a multi-select filter; counts refer
  to the loaded scene and remain available when a kind is hidden.
- Filter actions compose against the latest dispatched view, avoiding stale
  router state when users toggle several filters in rapid succession.
- Graph and List share the same filtered scene. List keeps resource kinds
  visible on narrow screens and provides an alternative when WebGL2 is absent
  or its context is lost.
- The inspector overlays rather than resizes the canvas. Preview returns to
  the existing graph and camera; Open in vault navigates to the resource.
- Visible orbit, pan, zoom and fit controls complement pointer and keyboard
  interaction. The camera moves on selection only when necessary to expose
  a resource obscured by the inspector or behind the camera.

## Relationship identity and data boundaries

Solid and dashed curves distinguish explicit relationships and body links.
Parallel and reversed edges use separate curves; self-relations use loops.
Selection shows direction, relation type and server-reported provenance.

Following an inspector relation carries its provenance into the scene. Both
materialization and merge deduplicate by source, target, relation and provenance.
Consequently, fetching the same connection again does not add a duplicate,
while a real explicit relationship and body link between the same resources
remain distinct. Missing provenance is not inferred from a relation label.

Backend APIs are unchanged. Optional coverage metadata remains unknown when
omitted by older servers. Only a server-confirmed degree of zero identifies
an orphan; a truncated or filtered map cannot establish that a resource has no
connections. Existing overview/neighborhood limits and incomplete BFS-edge
coverage remain. Account or permission changes reset the scene and history.

## Regression coverage

- Real force-layout tests cover non-coplanar volume, deterministic ordering,
  pinned coordinates, incremental expansion and larger fixture scenes.
- Presentation tests exercise perspective math, clipping, safe-area focus,
  parallel/reverse curves, self-loops and label priority.
- Browser scenarios use Chromium/WebGL2 with intercepted HTTP fixtures:
  empty, single, isolated and mixed scenes; both rotation axes; direct node
  and edge selection; preview return; light/dark themes; mobile controls;
  rapid filters; canonical typed expansion; request failure and retry; and
  stale-response isolation.
- Incoming and outgoing traversal regressions verify that implicit and
  explicit connections stay distinct and repeated expansion does not inflate
  relationship counts.
- Browser fixture checks are not live-backend integration tests or production
  performance guarantees. The normal design, type, lint, unit and build gates
  remain applicable.

## References

- [Obsidian Graph view](https://help.obsidian.md/plugins/graph)
- [Three.js](https://threejs.org/docs/)
- [OrbitControls](https://threejs.org/docs/pages/OrbitControls.html)
- [d3-force-3d](https://github.com/vasturiano/d3-force-3d)
- [Primer SegmentedControl](https://primer.style/product/components/segmented-control/)
- [Primer ActionBar](https://primer.style/product/components/action-bar/)
