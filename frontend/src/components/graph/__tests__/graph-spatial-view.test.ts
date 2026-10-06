import { describe, expect, it } from "vitest";
import { PerspectiveCamera, Vector3 } from "three";
import { cameraForPoints, edgeCurvePoints, focusSpatialCamera, visibleLabels } from "../graph-spatial-view";

describe("spatial graph presentation", () => {
  it.each([12, 400])("brings a selected point into the unobscured viewport from camera depth %i", distance => {
    const camera = new PerspectiveCamera(45, 1.5, 0.1, 100000);
    const target = new Vector3(); camera.position.set(0, 0, distance); camera.lookAt(target); camera.updateMatrixWorld();
    const point = { x: 400, y: 60, z: 50 };
    focusSpatialCamera(camera, target, point, 1200, 800);
    const projected = new Vector3(point.x, point.y, point.z).project(camera);
    expect(projected.z).toBeGreaterThan(-1); expect(projected.z).toBeLessThan(1);
    expect((projected.x + 1) / 2 * 1200).toBeGreaterThanOrEqual(74);
    expect((projected.x + 1) / 2 * 1200).toBeLessThanOrEqual(811);
    expect((-projected.y + 1) / 2 * 800).toBeGreaterThanOrEqual(54);
  });
  it("fits a volumetric scene from an oblique perspective and accommodates portrait viewports", () => {
    const points = [{ x: -100, y: -60, z: -80 }, { x: 100, y: 60, z: 80 }];
    const wide = cameraForPoints(points, 1.6), narrow = cameraForPoints(points, 0.6);
    expect(wide.target).toEqual({ x: 0, y: 0, z: 0 });
    expect(wide.position.x).toBeGreaterThan(0);
    expect(wide.position.y).toBeGreaterThan(0);
    expect(wide.position.z).toBeGreaterThan(100);
    expect(narrow.distance).toBeGreaterThan(wide.distance);
    expect(cameraForPoints([], 0).distance).toBeGreaterThan(0);
  });
  it("keeps parallel edges distinct and gives self-relations a visible loop", () => {
    const a = { x: 0, y: 0, z: 0 }, b = { x: 100, y: 20, z: 60 };
    const first = edgeCurvePoints(a, b, 0, 2), second = edgeCurvePoints(a, b, 1, 2);
    expect(first[0]).toEqual(a);
    expect(first.at(-1)).toEqual(b);
    expect(first[8]).not.toEqual(second[8]);
    // Reversing one relationship must not collapse its curve onto the other.
    expect(first[8]).not.toEqual(edgeCurvePoints(b, a, 1, 2)[8]);
    const loop = edgeCurvePoints(a, a, 0, 1);
    expect(loop.some(p => Math.hypot(p.x, p.y, p.z) > 5)).toBe(true);
  });
  it("prioritises selected labels and prevents overlapping labels without removing resources", () => {
    const labels = [{ uri: "normal", x: 100, y: 100, depth: 0, priority: 0 }, { uri: "selected", x: 105, y: 100, depth: 0, priority: 100 }, { uri: "behind-camera", x: 300, y: 200, depth: 2, priority: 0 }];
    expect(visibleLabels(labels, 800, 600).map(l => l.uri)).toEqual(["selected"]);
    expect(labels).toHaveLength(3);
  });
});
