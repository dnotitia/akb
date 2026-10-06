import { layoutSpatialGraph, type SpatialLayoutInput } from "./graph-spatial-layout";

/** All layout computation remains off the renderer's main thread. */
self.onmessage = (event: MessageEvent<SpatialLayoutInput>) => {
  try {
    self.postMessage({ positions: layoutSpatialGraph(event.data) });
  } catch {
    self.postMessage({ error: "The 3D layout could not be calculated. Retry or use List; your resources are still available." });
  }
};
