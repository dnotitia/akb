import { describe, it, expect } from "vitest";
import {
  computeViewportRect,
  inViewportPoint,
  lodBand,
  nextBand,
  bandTargetCount,
  lodVisibleSet,
  fitZoomFromBbox,
  LOD_BANDS,
} from "../lod";

describe("computeViewportRect", () => {
  it("centers the world rect on (cx,cy) sized by canvas/zoom", () => {
    const r = computeViewportRect({ w: 800, h: 600, k: 1, cx: 0, cy: 0 });
    expect(r).toEqual({ minX: -400, maxX: 400, minY: -300, maxY: 300 });
  });

  it("halves the half-extents when zoom doubles", () => {
    const r = computeViewportRect({ w: 800, h: 600, k: 2, cx: 0, cy: 0 });
    expect(r).toEqual({ minX: -200, maxX: 200, minY: -150, maxY: 150 });
  });

  it("inflates by marginPx/k on every side", () => {
    const r = computeViewportRect({ w: 800, h: 600, k: 2, cx: 0, cy: 0, marginPx: 80 });
    // half-extents ±200/±150, plus 80/2 = 40 margin
    expect(r).toEqual({ minX: -240, maxX: 240, minY: -190, maxY: 190 });
  });

  it("offsets by the graph-space center", () => {
    const r = computeViewportRect({ w: 800, h: 600, k: 1, cx: 100, cy: -50 });
    expect(r).toEqual({ minX: -300, maxX: 500, minY: -350, maxY: 250 });
  });
});

describe("inViewportPoint", () => {
  const r = { minX: -400, maxX: 400, minY: -300, maxY: 300 };
  it("is inside just within the rect", () => {
    expect(inViewportPoint(390, 0, r)).toBe(true);
    expect(inViewportPoint(0, 299, r)).toBe(true);
  });
  it("is outside past an edge", () => {
    expect(inViewportPoint(410, 0, r)).toBe(false);
    expect(inViewportPoint(0, 320, r)).toBe(false);
  });
  it("includes the boundary", () => {
    expect(inViewportPoint(400, 300, r)).toBe(true);
  });
});

describe("lodBand", () => {
  it("maps relative zoom to a band (overview→near)", () => {
    expect(lodBand(1.0)).toBe(0); // fit / overview
    expect(lodBand(2.0)).toBe(1);
    expect(lodBand(3.0)).toBe(2);
    expect(lodBand(5.0)).toBe(3); // deep zoom-in
  });
});

describe("nextBand hysteresis", () => {
  it("does not oscillate on a small wiggle across a boundary", () => {
    // boundary at 1.5; dead-band ≈ [1.5/1.08, 1.5*1.08] = [1.389, 1.62]
    let band = 0;
    for (const z of [1.45, 1.55, 1.48, 1.52, 1.46]) {
      band = nextBand(z, band);
      expect(band).toBe(0); // stays in band 0 inside the dead-band
    }
  });

  it("steps up only past boundary·(1+HYST) and back only below boundary/(1+HYST)", () => {
    expect(nextBand(1.63, 0)).toBe(1); // > 1.5*1.08 → up
    expect(nextBand(1.55, 1)).toBe(1); // inside dead-band → stay
    expect(nextBand(1.38, 1)).toBe(0); // < 1.5/1.08 → down
  });

  it("handles a deliberate multi-band jump", () => {
    expect(nextBand(5.0, 0)).toBe(3);
    expect(nextBand(1.0, 3)).toBe(0);
  });
});

describe("bandTargetCount", () => {
  it("returns the band's count target, clamped", () => {
    expect(bandTargetCount(0)).toBe(LOD_BANDS[0].target);
    expect(bandTargetCount(3)).toBe(Infinity);
    expect(bandTargetCount(99)).toBe(Infinity); // clamped to last
  });
});

describe("lodVisibleSet", () => {
  const ranked = ["a", "b", "c", "d", "e"]; // already degree-desc
  it("returns the top-N URIs as a set (exact count, tie-proof)", () => {
    expect(lodVisibleSet(ranked, 2)).toEqual(new Set(["a", "b"]));
    expect(lodVisibleSet(ranked, 3)).toEqual(new Set(["a", "b", "c"]));
  });
  it("returns null (all visible) when the target covers everything", () => {
    expect(lodVisibleSet(ranked, 5)).toBeNull();
    expect(lodVisibleSet(ranked, 999)).toBeNull();
  });
  it("admits EXACTLY N even when many nodes would tie on a degree floor", () => {
    // 24 hubs (deg 13) + leaves (deg 1): a floor would admit all; the set is exact
    const r = Array.from({ length: 100 }, (_, i) => `n${i}`);
    expect(lodVisibleSet(r, 30)?.size).toBe(30);
  });
});

describe("fitZoomFromBbox", () => {
  const bbox = { x: [-100, 100] as [number, number], y: [-50, 50] as [number, number] }; // 200×100
  it("is min(w/bboxW, h/bboxH) × padding", () => {
    // w/bboxW = 800/200 = 4; h/bboxH = 600/100 = 6 → min 4 × 0.92
    expect(fitZoomFromBbox(bbox, 800, 600)).toBeCloseTo(4 * 0.92);
    // taller viewport → height dimension binds
    expect(fitZoomFromBbox(bbox, 800, 100)).toBeCloseTo(1 * 0.92); // min(4, 1)=1
  });
  it("returns 0 for a missing or degenerate bbox (→ relZoom falls back to 1)", () => {
    expect(fitZoomFromBbox(null, 800, 600)).toBe(0);
    expect(fitZoomFromBbox({ x: [5, 5], y: [0, 10] }, 800, 600)).toBe(0); // zero width
    expect(fitZoomFromBbox(bbox, 0, 600)).toBe(0);
  });
});
