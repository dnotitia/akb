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
import { hashHue } from "@/lib/utils";
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

/**
 * Stable cluster color for a group key, drawn from the tokenized, CVD-vetted
 * categorical scale (--color-cat-1..6, resolved by readColors() — already
 * theme-correct, so there is no per-component light/dark fork here). hashHue
 * buckets the group deterministically into one of the `cat` colors.
 */
export function groupColor(group: string, cat: string[]): string {
  if (!cat.length) return "gray"; // emergency fallback if the token scale didn't resolve
  return cat[hashHue(group) % cat.length];
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
