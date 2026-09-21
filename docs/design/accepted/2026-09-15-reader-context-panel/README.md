# Resource identity and reader context

Status: accepted and implemented

Date: 2026-09-15

Updated: 2026-09-21 — floating reader context

Scope: frontend resource breadcrumbs and the document reader; no API changes.

## Problem

The breadcrumb did not distinguish Vault, Collection and resource types at a
glance. Last-edit metadata existed in document responses but was not visible.
The overflow menu repeated the inspector's Info, Outline and History choices.
The initial docked inspector then reduced the article's width during quick
lookups, changing wrapping and interrupting reading. The revised design keeps
one discoverable edge rail with temporary floating context.

## Decision

- Use one leading Box for the Vault, text-only Collection paths, and a trailing
  FileText, File or Table2 beside the resource title. Vault sections share the
  same breadcrumb without repeating section icons. Keep labels readable; icons
  supplement them and are hidden from screen readers, while the resource kind
  remains available as accessible text.
- Put `Last edited` in the existing command-row metadata, not a new title band.
  Show relative time there and an exact local timestamp with timezone on
  hover/focus and in Info. Missing/invalid values are not inferred.
- Replace duplicated menu entries and inspector tabs with a 48px right-edge
  rail: Info, Table of contents, Relations, History. Each view has one entry,
  an accessible name, tooltip, selected/expanded state and a 44px target.
- Keep the panel closed initially. Clicking a view opens it; clicking the same
  edge control closes it. Close and Escape also work. Hover is a hint, never the
  only way to discover or operate a control.
- With at least 48rem of **available reader width** and 20rem height, float a
  24rem panel just left of the edge controls. Preserve article geometry and
  scrolling, leave the background interactive and undimmed, and size height to
  real content up to the reader boundary. Use an opaque surface, thin border,
  restrained radius and soft shadow. Small/short readers retain a right modal
  overlay with backdrop and focus containment.
- Outside click and keyboard focus leaving for the article dismiss floating
  context without swallowing the clicked action or stealing its focus. Edge
  controls switch content in place. Close/Escape restore the selected control.
  Child property/relation dialogs and their menus remain usable, and adaptive
  presentation changes must not unmount an active input flow.
- Give the selected view the available panel height. In modal mode, context
  controls move inside the accessible modal while the background rail is hidden
  from assistive technology. Closing restores the selected edge control.
- In either presentation, selecting a heading closes the panel and focuses the
  heading. Raw/diff views explain the absence of heading navigation instead of
  offering links that cannot reach rendered headings.
- Keep the rail out of Edit. Existing metadata editing, relations, version
  browsing, publishing and permissions retain their original contracts.

## Timestamp contract

`GET /documents` exposes optional `updated_at` and `created_at`. Historical
reads still return current metadata timestamps. Therefore historical/diff
command rows use `Version saved` from the matching history entry, including
short-hash matching. Info distinguishes `Latest edit`, `Version saved` and
`Created`. A missing history date must not fall back to today's time or the
live document's timestamp. The author field is not relabelled as last editor.
After a save, use a returned timestamp when available and refresh the document
snapshot. Older write responses without dates stay unknown until the read
completes; browser-clock guesses are not shown as exact server timestamps.
Metadata edits also refresh the displayed date independently of local content
overrides. Heading jumps preserve the route-backed preview's navigation state.

## Design rationale and references

The frontend-design and ui-ux-pro-max skills informed hierarchy, progressive
disclosure, consistent icons, touch targets and focus handling. Their local
search results did not contain an exact document-inspector layout prescription;
the responsive choice is an AKB-specific design decision, not a quoted rule.

- [Material supporting panes](https://developer.android.com/develop/adaptive-apps/guides/build-a-supporting-pane-layout)
  separates primary content from optional context and adapts side-by-side panes
  for smaller windows. AKB applies that principle, not Android implementation.
- [Carbon right panel](https://carbondesignsystem.com/components/UI-shell-right-panel/usage/)
  illustrates a consistent, explicitly triggered supplemental surface. It is a
  system-level shell pattern; AKB's rail stays local to the document instead of
  adding more global-header actions.

A permanently open sidebar was rejected because it competes with the two
existing navigation rails. Review of the docked implementation also favored
stable article geometry over simultaneous side-by-side reading; pin/dock
controls are deliberately not added. The floating placement is AKB-specific;
[Radix's dismissing and layering primitives](https://www.radix-ui.com/primitives/docs/components/popover)
provide a reference for nonmodal focus and outside interaction. The inspector
uses DismissableLayer and Dialog; publication controls use Popover. Declare
DismissableLayer and Popover as direct dependencies instead of relying on
transitive packages from the existing Radix component family.
A hover-only or drag-only opener was rejected for
touch, keyboard and discoverability. More actions in the command row would
reintroduce the crowding this design removes.

## Verification contract

Unit tests cover resource icons, missing/invalid/latest/version timestamps,
nonduplicated entries, adaptive width, modal semantics and focus restoration.
Browser fixtures cover light/dark, narrow/wide screens, unchanged article bounds
and scrolling, outside actions, mode changes, nested editing/preview closure
and outline navigation. These
fixtures isolate responses and do not mutate a user's Vault. Deployment and
live-data validation are separate from these checks.
