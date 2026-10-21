import { Vector3, type PerspectiveCamera } from "three";
import type { SpatialPoint } from "./graph-spatial-layout";

/** The direction the camera looks from: straight above a flat map, from a raised corner in 3D. */
export const TOP_VIEW = { x: 0, y: 0, z: 1 } as const;
export const CORNER_VIEW = { x: 0.55, y: 0.32, z: 1 } as const;

export function cameraForPoints(points: SpatialPoint[], aspect: number, from: SpatialPoint = CORNER_VIEW) {
  const box = points.length ? points : [{ x: 0, y: 0, z: 0 }];
  const min = new Vector3(Infinity, Infinity, Infinity), max = new Vector3(-Infinity, -Infinity, -Infinity);
  for (const p of box) { min.min(new Vector3(p.x, p.y, p.z)); max.max(new Vector3(p.x, p.y, p.z)); }
  const center = min.clone().add(max).multiplyScalar(0.5);
  const radius = Math.max(30, ...box.map(p => center.distanceTo(new Vector3(p.x, p.y, p.z))));
  const angle = Math.min(Math.PI / 8, Math.atan(Math.tan(Math.PI / 8) * Math.max(0.15, aspect)));
  const distance = radius / Math.sin(angle) * 1.2 + 25;
  const position = new Vector3(from.x, from.y, from.z).normalize().multiplyScalar(distance).add(center);
  return { position: { x: position.x, y: position.y, z: position.z }, target: { x: center.x, y: center.y, z: center.z }, distance };
}

/** Reveal an obscured selection without resetting the whole scene orientation.
 * Translation uses the selected point's depth, not the orbit pivot's depth. */
export function focusSpatialCamera(camera: PerspectiveCamera, target: Vector3, point: SpatialPoint, width: number, height: number) {
  camera.updateMatrixWorld();
  const world = new Vector3(point.x, point.y, point.z);
  let depth = -world.clone().applyMatrix4(camera.matrixWorldInverse).z;
  if (depth < 80 || depth > camera.far * 0.8) {
    const offset = camera.position.clone().sub(target);
    if (offset.lengthSq() === 0) offset.set(0.55, 0.32, 1);
    offset.setLength(Math.max(240, Math.min(1500, offset.length())));
    target.copy(world); camera.position.copy(world).add(offset); camera.lookAt(target); camera.updateMatrixWorld();
    depth = -world.clone().applyMatrix4(camera.matrixWorldInverse).z;
  }
  const projected = world.clone().project(camera);
  const x = (projected.x + 1) / 2 * width, y = (-projected.y + 1) / 2 * height;
  const safeRight = width >= 768 ? width - 390 : width - 65;
  const safeBottom = width >= 768 ? height - 110 : height * 0.27;
  const dx = Math.max(75, Math.min(safeRight, x)) - x;
  const dy = Math.max(65, Math.min(safeBottom, y)) - y;
  const scale = 2 * depth * Math.tan(camera.fov * Math.PI / 360) / Math.max(1, height);
  const offset = new Vector3().setFromMatrixColumn(camera.matrixWorld, 0).multiplyScalar(-dx * scale)
    .addScaledVector(new Vector3().setFromMatrixColumn(camera.matrixWorld, 1), dy * scale);
  camera.position.add(offset); target.add(offset); camera.updateMatrixWorld();
}

/** ``flat`` bends parallel relationships within the z = 0 plane, so a top-down camera still sees them apart. */
export function edgeCurvePoints(start: SpatialPoint, end: SpatialPoint, index: number, count: number, flat = false): SpatialPoint[] {
  const a = new Vector3(start.x, start.y, start.z), b = new Vector3(end.x, end.y, end.z);
  if (a.distanceToSquared(b) < 0.001) {
    return Array.from({ length: 25 }, (_, i) => {
      const angle = i / 24 * Math.PI * 2, radius = 14 + index * 6;
      return { x: a.x + Math.sin(angle) * radius, y: a.y + (1 - Math.cos(angle)) * radius, z: a.z + (flat ? 0 : Math.sin(angle) * radius * 0.4) };
    });
  }
  const direction = b.clone().sub(a);
  // Use one orientation for a pair, even when its relationships run both ways.
  const sign = direction.x || direction.y || direction.z;
  if (sign < 0) direction.negate();
  const normal = direction.clone().cross(flat ? new Vector3(0, 0, 1) : new Vector3(0, 1, 0));
  if (normal.lengthSq() < 0.001) normal.copy(direction).cross(new Vector3(1, 0, 0));
  normal.normalize();
  const bend = (index - (count - 1) / 2) * 22;
  const control = a.clone().add(b).multiplyScalar(0.5).addScaledVector(normal, bend);
  return Array.from({ length: 17 }, (_, i) => {
    const t = i / 16, p = a.clone().multiplyScalar((1 - t) ** 2).addScaledVector(control, 2 * (1 - t) * t).addScaledVector(b, t * t);
    return { x: p.x, y: p.y, z: p.z };
  });
}

/** ``x``/``y`` is the label's anchor; ``ax``/``ay`` say where the box sits relative to it, as fractions of its size
 *  (-0.5 centred, 0 to the right/below, -1 to the left/above). */
export interface ProjectedLabel { uri: string; x: number; y: number; depth: number; priority: number; width?: number; lines?: number; ax?: number; ay?: number }
export function visibleLabels<T extends ProjectedLabel>(labels: T[], width: number, height: number): T[] {
  const result: T[] = [];
  for (const label of [...labels].sort((a, b) => b.priority - a.priority || a.depth - b.depth)) {
    if (label.depth < -1 || label.depth > 1 || label.x < 20 || label.x > width - 20 || label.y < 8 || label.y > height - 62) continue;
    // Keep label boxes apart by their own size (up to 224 px wide, one or two lines).
    const box = (l: ProjectedLabel) => {
      const w = l.width ?? 160, h = (l.lines ?? 1) * 16 + 8;
      return { w, h, cx: l.x + w * ((l.ax ?? -0.5) + 0.5), cy: l.y + h * ((l.ay ?? 0) + 0.5) };
    };
    const mine = box(label);
    if (result.some(other => { const theirs = box(other); return Math.abs(theirs.cx - mine.cx) < (mine.w + theirs.w) / 2 && Math.abs(theirs.cy - mine.cy) < (mine.h + theirs.h) / 2; })) continue;
    result.push(label);
    if (result.length === 30) break;
  }
  return result;
}
