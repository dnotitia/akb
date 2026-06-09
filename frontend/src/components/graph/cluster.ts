// frontend/src/components/graph/cluster.ts
//
// Force-based clustering for the graph canvas — NO drawn hull/outline. Nodes
// in the same group are pulled together and tinted, so dense groups settle
// into naturally round clumps (the way a force-directed graph clusters),
// without any border or background fill.
//
//   1. grouping  — groupOf(): which cluster a node belongs to
//   2. color     — groupColor(): a stable, theme-aware ring tint per group
//   3. forces    — forceCluster() pulls a group together; forceCollide()
//                  keeps nodes from overlapping (so clumps aren't cramped)
//
// Dependency-free. Grouping seam: the forces + color read node.group, which
// is assigned at data ingest (use-graph-data) from the node's collection.
import { parseUri } from "@/lib/uri";
import type { GraphNode } from "./graph-types";

// ── Tunables (exported so they're easy to find + adjust) ──────────────────
/** How many leading collection segments define a cluster. For nested vaults
 *  like `weekly-updates/<week>/<topic>` this groups by WEEK at depth 2
 *  (everything under one top-level collection would otherwise be one blob). */
export const GROUP_DEPTH = 2;
/** How hard same-group nodes are pulled toward their shared centroid. Gentle
 *  on purpose: a soft pull lets links + charge shape each group into an
 *  organic neighbourhood rather than crushing it into a tight ball. */
export const CLUSTER_STRENGTH = 0.15;
/** Minimum spacing radius per node (px) — breathing room so nodes read
 *  individually instead of cramming. */
export const COLLIDE_RADIUS = 20;
/** Global node repulsion while clustering. Less negative than d3's -30
 *  default, so separate clusters sit CLOSER together instead of flying apart;
 *  forceCollide handles local spacing instead of charge. */
export const CHARGE_STRENGTH = -16;
/** d3 / force-graph default many-body charge, restored when clustering off. */
export const DEFAULT_CHARGE = -30;

/**
 * The cluster a node belongs to: the first GROUP_DEPTH segments of its
 * collection path (from the canonical akb:// URI). Root-level resources
 * (no collection) are ungrouped → null.
 */
export function groupOf(uri: string): string | null {
  const p = parseUri(uri);
  if (!p || !p.collection) return null;
  return p.collection.split("/").slice(0, GROUP_DEPTH).join("/") || null;
}

// ── Color ─────────────────────────────────────────────────────────────────

/** Deterministic string → hue in [0,360) (FNV-1a). Same group key always
 *  maps to the same hue regardless of how many groups exist (no flicker). */
function hashHue(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) % 360;
}

/** Luminance check on a hex background so colors adapt to the active theme.
 *  Falls back to "dark" for non-hex / unparseable input. */
export function isDarkBg(bg: string): boolean {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(bg.trim());
  if (!m) return true;
  let hex = m[1];
  if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return 0.299 * r + 0.587 * g + 0.114 * b < 128;
}

const colorCache = new Map<string, string>();

// Curated on-brand hue set for cluster rings — cool teals/sage + warm
// oranges/amber, anchored on the teal/orange brand. Replaces the former
// full-spectrum 0-360 rainbow (which surfaced off-brand blues/purples/magenta).
// hashHue() (0-359) buckets a group into one of these, keeping clusters
// distinguishable without leaving the brand palette.
const CLUSTER_HUES = [192, 200, 178, 160, 30, 18, 40];

/** Stable, theme-aware ring color for a group key (memoized — called per
 *  visible node every frame). Darker on a light background so it reads. */
export function groupColor(group: string, dark = true): string {
  const key = `${group}|${dark ? 1 : 0}`;
  const cached = colorCache.get(key);
  if (cached) return cached;
  const h = CLUSTER_HUES[hashHue(group) % CLUSTER_HUES.length];
  const col = dark ? `hsl(${h}, 58%, 60%)` : `hsl(${h}, 52%, 38%)`;
  colorCache.set(key, col);
  return col;
}

// ── Forces ──────────────────────────────────────────────────────────────

export interface SimForce {
  (alpha: number): void;
  initialize: (nodes: GraphNode[]) => void;
}

/**
 * Pulls each node toward its group's live centroid (velocity-based, scaled by
 * the decaying alpha). Ungrouped nodes are left to the default forces. This is
 * what makes a group settle into a round clump.
 */
export function forceCluster(strength = CLUSTER_STRENGTH): SimForce {
  let nodes: GraphNode[] = [];
  const force = (alpha: number) => {
    const cent = new Map<string, { x: number; y: number; n: number }>();
    for (const n of nodes) {
      if (!n.group || n.x == null || n.y == null) continue;
      const c = cent.get(n.group);
      if (c) {
        c.x += n.x;
        c.y += n.y;
        c.n++;
      } else {
        cent.set(n.group, { x: n.x, y: n.y, n: 1 });
      }
    }
    for (const c of cent.values()) {
      c.x /= c.n;
      c.y /= c.n;
    }
    const k = strength * alpha;
    for (const n of nodes) {
      if (!n.group || n.x == null || n.y == null) continue;
      const c = cent.get(n.group);
      if (!c) continue;
      n.vx = (n.vx ?? 0) + (c.x - n.x) * k;
      n.vy = (n.vy ?? 0) + (c.y - n.y) * k;
    }
  };
  force.initialize = (ns: GraphNode[]) => {
    nodes = ns;
  };
  return force;
}

/**
 * Positional anti-overlap: nudges any two nodes closer than 2·radius apart so
 * clumps stay breathable rather than cramped. O(n²) — fine for the ≤200-node
 * graphs this viewer renders.
 */
export function forceCollide(radius = COLLIDE_RADIUS, strength = 0.8): SimForce {
  let nodes: GraphNode[] = [];
  const force = () => {
    const n = nodes.length;
    const min = radius * 2;
    const min2 = min * min;
    for (let i = 0; i < n; i++) {
      const a = nodes[i];
      if (a.x == null || a.y == null) continue;
      for (let j = i + 1; j < n; j++) {
        const b = nodes[j];
        if (b.x == null || b.y == null) continue;
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 === 0 || d2 >= min2) continue;
        const d = Math.sqrt(d2);
        const push = ((min - d) / d) * strength * 0.5;
        dx *= push;
        dy *= push;
        a.x += dx;
        a.y += dy;
        b.x -= dx;
        b.y -= dy;
      }
    }
  };
  force.initialize = (ns: GraphNode[]) => {
    nodes = ns;
  };
  return force;
}
