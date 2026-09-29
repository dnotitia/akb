import type { GraphMeta } from "./use-graph-data";

export function graphCoverage(meta?: GraphMeta): { label: string; detail: string } {
  const partial = meta?.truncated === true || meta?.orphanTruncated === true;
  const connected = meta?.returned != null && meta.nodesTotal != null
    ? `${meta.returned} of ${meta.nodesTotal} connected resources loaded.` : "Connected-resource coverage is not supplied by this server.";
  const isolated = meta?.orphanReturned != null
    ? `${meta.orphanReturned} unconnected resources loaded${meta.orphanTruncated ? " (limited)" : ""}.` : "Unconnected-resource coverage is unknown.";
  return {
    label: partial ? "Partial map" : "Loaded map",
    detail: `${connected} ${isolated} Filters apply to this loaded map. Neighborhoods may omit additional resources and relationships.`,
  };
}
