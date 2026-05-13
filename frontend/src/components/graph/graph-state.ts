// frontend/src/components/graph/graph-state.ts
import {
  ALL_NODE_KINDS,
  ALL_RELATIONS,
  DEFAULT_VIEW,
  type GraphView,
  type NodeKind,
  type RelationKind,
} from "./graph-types";

const ALL_KIND_SET = new Set<NodeKind>(ALL_NODE_KINDS);
const ALL_REL_SET = new Set<RelationKind>(ALL_RELATIONS);

function setsEqual<T>(a: Set<T>, b: Set<T>): boolean {
  if (a.size !== b.size) return false;
  for (const v of a) if (!b.has(v)) return false;
  return true;
}

export function viewToQuery(v: GraphView): string {
  const p = new URLSearchParams();
  if (v.entry) p.set("entry", v.entry);
  if (v.depth !== DEFAULT_VIEW.depth) p.set("depth", String(v.depth));
  if (!setsEqual(v.types, ALL_KIND_SET)) {
    p.set("types", [...v.types].join(","));
  }
  if (!setsEqual(v.relations, ALL_REL_SET)) {
    p.set("rel", [...v.relations].join(","));
  }
  if (v.selected) p.set("sel", v.selected);
  return p.toString();
}

function parseDepth(raw: string | null): 1 | 2 | 3 {
  if (raw === "1") return 1;
  if (raw === "3") return 3;
  return 2;
}

export function queryToView(q: URLSearchParams): GraphView {
  const types = q.get("types");
  const rel = q.get("rel");
  return {
    entry: q.get("entry") || undefined,
    depth: parseDepth(q.get("depth")),
    types: types
      ? new Set(
          types.split(",").filter((s): s is NodeKind => (ALL_KIND_SET as Set<string>).has(s)),
        )
      : new Set(ALL_NODE_KINDS),
    relations: rel
      ? new Set(
          rel.split(",").filter((s): s is RelationKind => (ALL_REL_SET as Set<string>).has(s)),
        )
      : new Set(ALL_RELATIONS),
    selected: q.get("sel") || undefined,
  };
}
