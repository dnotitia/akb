import {
  BufferGeometry, CircleGeometry, Color, DynamicDrawUsage,
  Float32BufferAttribute, Group, InstancedMesh, LineBasicMaterial, LineDashedMaterial,
  LineSegments, MOUSE, Matrix4, Mesh, MeshBasicMaterial, PerspectiveCamera, Plane, PlaneGeometry,
  Raycaster, RingGeometry, Scene, Spherical, TOUCH, Vector2, Vector3, WebGLRenderer,
  type Material, type Object3D,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { HOP_TOKENS, categoryOf, edgeId, edgeStrength, shortName, type GraphEdge, type GraphNode } from "./graph-types";
import type { GraphDimension, SpatialPoint } from "./graph-spatial-layout";
import { CORNER_VIEW, TOP_VIEW, cameraForPoints, edgeCurvePoints, focusSpatialCamera, visibleLabels, type ProjectedLabel } from "./graph-spatial-view";

interface Callbacks {
  onSelect(uri: string | undefined): void;
  onContext(node: GraphNode, x: number, y: number): void;
  onEdge(edge: GraphEdge): void;
  onLabels(labels: ProjectedLabel[]): void;
  onCameraChange(): void;
  onContextLost(): void;
  /** A resource was dragged to a new position (already written to the shared position map). */
  onMove?(uri: string): void;
}


/** GPU rendering and camera only. Data, permissions and force layout stay outside. */
export class SpatialGraphRenderer {
  private readonly scene = new Scene();
  private readonly camera = new PerspectiveCamera(45, 1, 0.1, 100000);
  private readonly renderer: WebGLRenderer;
  private readonly controls: OrbitControls;
  private readonly resources = new Group();
  private readonly ray = new Raycaster();
  private readonly nodeObjects = new Map<InstancedMesh, GraphNode[]>();
  private readonly edgeObjects = new Map<LineSegments, number[]>();
  private selectionRing?: Mesh;
  private nodes: GraphNode[] = [];
  private edges: GraphEdge[] = [];
  private positions = new Map<string, SpatialPoint>();
  private selected?: string;
  private edge?: GraphEdge;
  private hovered?: string;
  private pinned = new Set<string>();
  private neighbors = new Set<string>();
  private degrees = new Map<string, number>();
  private adjusting = false;
  private frame = 0;
  private disposed = false;
  private down?: { x: number; y: number; button: number };
  private drag?: { uri: string; pointerId: number; plane: Plane; grab: Vector3; moved: boolean };
  private dragFrame = 0;
  private hops?: Map<string, number>;
  private weak = new Set<string>();
  private topInset = 0;
  private showCategory = false;
  private tree?: Set<string>;
  private groupTokens?: Map<string, string>;
  private colors: Record<string, string> = {};

  constructor(private host: HTMLDivElement, private callbacks: Callbacks, private dimension: GraphDimension = 2) {
    this.renderer = new WebGLRenderer({ antialias: true, alpha: false, powerPreference: "high-performance", preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.domElement.setAttribute("aria-hidden", "true");
    this.host.append(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = false;
    this.controls.autoRotate = false;
    this.controls.minDistance = 12;
    this.controls.maxDistance = 50000;
    this.controls.screenSpacePanning = true;
    this.controls.addEventListener("change", this.changed);
    this.applyDimension();
    this.scene.add(this.resources);
    this.camera.position.set(100, 60, 260);
    // Capture phase: a press on a resource must disable the camera controls
    // before their own pointerdown handler starts an orbit or a pan.
    this.renderer.domElement.addEventListener("pointerdown", this.pointerDown, { capture: true });
    this.renderer.domElement.addEventListener("pointerup", this.pointerUp);
    this.renderer.domElement.addEventListener("pointercancel", this.pointerCancel);
    this.renderer.domElement.addEventListener("lostpointercapture", this.pointerCancel);
    this.renderer.domElement.addEventListener("pointermove", this.pointerMove);
    this.renderer.domElement.addEventListener("pointerleave", this.pointerLeave);
    this.renderer.domElement.addEventListener("contextmenu", this.contextMenu);
    this.renderer.domElement.addEventListener("webglcontextlost", this.contextLost);
    this.theme(); this.resize();
  }

  private changed = () => { if (!this.adjusting) this.callbacks.onCameraChange(); this.draw(); };
  private contextLost = (event: Event) => { event.preventDefault(); this.callbacks.onContextLost(); };
  private contextMenu = (event: Event) => event.preventDefault();
  /** 2D: a flat map seen from straight above; left-drag pans. 3D: left-drag orbits, right-drag pans. */
  private applyDimension() {
    const flat = this.dimension === 2;
    this.controls.enableRotate = !flat;
    this.controls.mouseButtons = flat
      ? { LEFT: MOUSE.PAN, MIDDLE: MOUSE.DOLLY, RIGHT: MOUSE.PAN }
      : { LEFT: MOUSE.ROTATE, MIDDLE: MOUSE.DOLLY, RIGHT: MOUSE.PAN };
    this.controls.touches = flat ? { ONE: TOUCH.PAN, TWO: TOUCH.DOLLY_PAN } : { ONE: TOUCH.ROTATE, TWO: TOUCH.DOLLY_PAN };
    this.camera.up.set(0, 1, 0);
  }

  setDimension(dimension: GraphDimension) {
    if (dimension === this.dimension) return;
    this.dimension = dimension; this.applyDimension();
  }

  private pointerDown = (event: PointerEvent) => {
    this.down = { x: event.clientX, y: event.clientY, button: event.button };
    if (event.button !== 0) return;
    const node = this.hit(event).node, point = node && this.positions.get(node.uri);
    if (!node || !point) return;
    // Drag on the plane through the resource that faces the camera (z = 0 on a flat map).
    const at = new Vector3(point.x, point.y, point.z);
    const normal = new Vector3(); this.camera.getWorldDirection(normal);
    const plane = new Plane().setFromNormalAndCoplanarPoint(normal, at);
    const grab = this.pointOnPlane(event, plane)?.sub(at) ?? new Vector3();
    this.drag = { uri: node.uri, pointerId: event.pointerId, plane, grab, moved: false };
    this.controls.enabled = false;
    this.renderer.domElement.setPointerCapture?.(event.pointerId);
    this.host.style.cursor = "grabbing";
  };
  private pointOnPlane(event: PointerEvent, plane: Plane) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.ray.setFromCamera(new Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1), this.camera);
    return this.ray.ray.intersectPlane(plane, new Vector3()) ?? undefined;
  }
  private dragTo(event: PointerEvent) {
    const drag = this.drag, down = this.down;
    if (!drag || event.pointerId !== drag.pointerId) return;
    if (!drag.moved && down && Math.hypot(event.clientX - down.x, event.clientY - down.y) <= 5) return;
    const hit = this.pointOnPlane(event, drag.plane); if (!hit) return;
    hit.sub(drag.grab);
    if (!drag.moved) {
      // The move counts from the first frame past the click threshold, so a
      // layout that finishes mid-gesture cannot overwrite the dragged position.
      drag.moved = true;
      this.callbacks.onMove?.(drag.uri);
    }
    this.positions.set(drag.uri, { x: hit.x, y: hit.y, z: this.dimension === 2 ? 0 : hit.z });
    // One rebuild per frame keeps the lines attached while the pointer moves.
    if (!this.dragFrame) this.dragFrame = requestAnimationFrame(() => { this.dragFrame = 0; if (!this.disposed) this.rebuild(); });
  }
  /** Ends the drag of ``event``'s pointer; ``commit`` places the resource at the release point first. Always restores the camera controls. */
  private endDrag(event: PointerEvent, commit: boolean) {
    const drag = this.drag; if (!drag || event.pointerId !== drag.pointerId) return;
    try {
      if (commit && drag.moved) this.dragTo(event);
    } finally {
      this.drag = undefined; this.controls.enabled = true;
      if (this.renderer.domElement.hasPointerCapture?.(drag.pointerId)) this.renderer.domElement.releasePointerCapture(drag.pointerId);
      this.host.style.cursor = "pointer";
    }
  }
  private pointerCancel = (event: PointerEvent) => {
    if (this.drag?.pointerId !== event.pointerId) return;
    this.endDrag(event, false); this.down = undefined;
  };
  private pointerUp = (event: PointerEvent) => {
    // Another pointer (a second finger) neither ends nor clicks during a drag.
    if (this.drag && event.pointerId !== this.drag.pointerId) return;
    const dragged = this.drag?.moved; this.endDrag(event, true);
    const down = this.down; this.down = undefined;
    if (dragged) return;
    if (!down || Math.hypot(event.clientX - down.x, event.clientY - down.y) > 5) return;
    const hit = this.hit(event);
    if (down.button === 2) { if (hit.node) this.callbacks.onContext(hit.node, event.clientX, event.clientY); }
    else if (down.button === 0) {
      if (hit.node) this.callbacks.onSelect(hit.node.uri);
      else if (hit.edge) this.callbacks.onEdge(hit.edge);
      else this.callbacks.onSelect(undefined);
    }
  };
  private pointerMove = (event: PointerEvent) => {
    if (this.drag) { this.dragTo(event); return; }
    if (event.buttons) return;
    const hit = this.hit(event), uri = hit.node?.uri;
    this.host.style.cursor = hit.node || hit.edge ? "pointer" : "grab";
    if (uri !== this.hovered) { this.hovered = uri; this.draw(); }
  };
  private pointerLeave = () => { if (this.drag) return; this.hovered = undefined; this.host.style.cursor = "grab"; this.draw(); };

  private hit(event: PointerEvent) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.ray.setFromCamera(new Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1), this.camera);
    const nodeHit = this.ray.intersectObjects([...this.nodeObjects.keys()], false)[0];
    if (nodeHit?.instanceId != null) return { node: this.nodeObjects.get(nodeHit.object as InstancedMesh)?.[nodeHit.instanceId] };
    this.ray.params.Line.threshold = this.camera.position.distanceTo(this.controls.target) * 0.004;
    const edgeHit = this.ray.intersectObjects([...this.edgeObjects.keys()], false)[0];
    if (edgeHit?.index != null) {
      const index = this.edgeObjects.get(edgeHit.object as LineSegments)?.[Math.floor(edgeHit.index / 2)];
      if (index != null) return { edge: this.edges[index] };
    }
    return {};
  }

  update(nodes: GraphNode[], edges: GraphEdge[], positions: Map<string, SpatialPoint>) {
    this.nodes = nodes; this.edges = edges; this.positions = positions;
    this.rebuild();
  }

  selection(selected: string | undefined, edge: GraphEdge | undefined, pinned: Set<string>) {
    this.selected = selected; this.edge = edge; this.pinned = pinned; this.rebuild();
  }

  /** Distance from the focused resource; while set, a resource's colour is its distance and its shape its kind. */
  setHops(hops: Map<string, number> | undefined, weak: Set<string> = new Set(), tree?: Set<string>, groupTokens?: Map<string, string>) {
    this.hops = hops; this.weak = weak; this.tree = tree; this.groupTokens = groupTokens; this.rebuild();
  }

  theme() {
    const css = getComputedStyle(document.documentElement);
    this.colors = Object.fromEntries(["surface", "cat-1", "cat-2", "cat-3", "cat-4", "cat-5", "cat-6", "graph-edge", "link"].map(key => [key, css.getPropertyValue(`--color-${key}`).trim()]));
    this.scene.background = new Color(this.colors.surface);
    this.rebuild();
  }

  private clearObjects() {
    this.resources.traverse(object => {
      const disposable = object as Object3D & { geometry?: BufferGeometry; material?: Material | Material[]; dispose?: () => void };
      disposable.geometry?.dispose();
      if (Array.isArray(disposable.material)) disposable.material.forEach(material => material.dispose());
      else disposable.material?.dispose();
      if (object instanceof InstancedMesh) object.dispose();
    });
    this.resources.clear(); this.nodeObjects.clear(); this.edgeObjects.clear(); this.selectionRing = undefined;
  }

  private rebuild() {
    this.clearObjects(); this.neighbors = new Set(); this.degrees = new Map();
    this.showCategory = new Set(this.nodes.map(categoryOf)).size > 1;
    for (const edge of this.edges) {
      this.degrees.set(edge.source, (this.degrees.get(edge.source) ?? 0) + 1);
      this.degrees.set(edge.target, (this.degrees.get(edge.target) ?? 0) + 1);
      if (edge.source === this.selected) this.neighbors.add(edge.target);
      if (edge.target === this.selected) this.neighbors.add(edge.source);
    }
    const background = new Color(this.colors.surface);
    for (const kind of ["document", "table", "file"] as const) {
      const nodes = this.nodes.filter(node => node.kind === kind && this.positions.has(node.uri));
      if (!nodes.length) continue;
      // Small flat symbols remain identifiable without colour or material shading.
      const geometry = kind === "table" ? new PlaneGeometry(1.7, 1.7)
        : kind === "file" ? new CircleGeometry(1.2, 4) : new CircleGeometry(1, 24);
      const mesh = new InstancedMesh(geometry, new MeshBasicMaterial(), nodes.length);
      mesh.instanceMatrix.setUsage(DynamicDrawUsage);
      mesh.renderOrder = 1;
      nodes.forEach((node, i) => {
        const active = node.uri === this.selected, nearby = this.neighbors.has(node.uri);
        const hop = this.hops?.get(node.uri);
        const group = this.groupTokens && (this.groupTokens.get(node.group ?? "") ?? "cat-6");
        const color = new Color(this.colors[this.hops
          ? HOP_TOKENS[Math.min(hop ?? HOP_TOKENS.length - 1, HOP_TOKENS.length - 1)]
          : group ?? (kind === "document" ? "cat-1" : kind === "table" ? "cat-3" : "cat-4")]);
        if (this.weak.has(node.uri) && !active) color.lerp(background, 0.55);
        if (this.selected && !active && !nearby) color.lerp(background, 0.65);
        mesh.setColorAt(i, color);
      });
      this.nodeObjects.set(mesh, nodes); this.resources.add(mesh);
    }
    if (this.selected && this.nodes.some(node => node.uri === this.selected) && this.positions.has(this.selected)) {
      this.selectionRing = new Mesh(new RingGeometry(1.6, 1.85, 32), new MeshBasicMaterial({ color: this.colors.link, depthTest: false }));
      this.selectionRing.renderOrder = 2;
      this.resources.add(this.selectionRing);
    }
    const pairs = new Map<string, number[]>();
    this.edges.forEach((edge, i) => {
      const key = JSON.stringify([edge.source, edge.target].sort());
      pairs.set(key, [...(pairs.get(key) ?? []), i]);
    });
    // Weak connections (body links, or a relation made weak) are dashed; strong ones solid.
    for (const implicit of [false, true]) {
      const vertices: number[] = [], colors: number[] = [], indices: number[] = [];
      this.edges.forEach((edge, edgeIndex) => {
        if ((edgeStrength(edge) === "weak") !== implicit) return;
        const a = this.positions.get(edge.source), b = this.positions.get(edge.target);
        if (!a || !b) return;
        const parallel = pairs.get(JSON.stringify([edge.source, edge.target].sort()))!;
        const points = edgeCurvePoints(a, b, parallel.indexOf(edgeIndex), parallel.length, this.dimension === 2);
        const active = edge.source === this.selected || edge.target === this.selected || this.isInspected(edge);
        const color = new Color(this.colors[active ? "link" : "graph-edge"]);
        if (!active) color.lerp(background, 0.25);
        // Around a focus the walk's backbone carries the picture; other strong edges among the kept resources stay as faint context.
        if (!active && this.tree && edgeStrength(edge) === "strong" && !this.tree.has(edgeId(edge))) color.lerp(background, 0.85);
        if (this.selected && !active) color.lerp(background, 0.82);
        for (let i = 0; i < points.length - 1; i++) {
          const from = points[i], to = points[i + 1];
          vertices.push(from.x, from.y, from.z, to.x, to.y, to.z);
          colors.push(color.r, color.g, color.b, color.r, color.g, color.b); indices.push(edgeIndex);
        }
      });
      if (!vertices.length) continue;
      const geometry = new BufferGeometry(); geometry.setAttribute("position", new Float32BufferAttribute(vertices, 3)); geometry.setAttribute("color", new Float32BufferAttribute(colors, 3));
      const material = implicit ? new LineDashedMaterial({ vertexColors: true, dashSize: 9, gapSize: 5, depthWrite: false }) : new LineBasicMaterial({ vertexColors: true, depthWrite: false });
      const lines = new LineSegments(geometry, material); lines.computeLineDistances(); this.edgeObjects.set(lines, indices); this.resources.add(lines);
    }
    this.draw();
  }

  /** Label priority without a selection: around a focus by distance (focus, then hop 1, 2, 3; weak last), else by connections. */
  private labelRank(uri: string) {
    if (this.neighbors.has(uri)) return 70;
    if (this.hops) {
      if (this.weak.has(uri)) return 10;
      const hop = this.hops.get(uri);
      return hop === undefined ? 5 : [95, 80, 60, 40][Math.min(hop, 3)];
    }
    return (this.pinned.has(uri) ? 20 : 0) + Math.min(19, this.degrees.get(uri) ?? 0);
  }

  private isInspected(edge: GraphEdge) { return !!this.edge && edge.source === this.edge.source && edge.target === this.edge.target && edge.relation === this.edge.relation && edge.kind === this.edge.kind; }

  private dotRadius(node: GraphNode) {
    const degree = this.degrees.get(node.uri) ?? 0;
    return Math.min(6.5, 4.5 + Math.sqrt(degree) * 0.5) + (node.uri === this.selected ? 1.5 : 0);
  }

  private faceDotsToCamera(height: number) {
    this.camera.updateMatrixWorld();
    const matrix = new Matrix4(), position = new Vector3(), scale = new Vector3();
    const unitsPerPixelAtUnitDepth = 2 * Math.tan(this.camera.fov * Math.PI / 360) / Math.max(1, height);
    for (const [mesh, nodes] of this.nodeObjects) {
      nodes.forEach((node, i) => {
        const p = this.positions.get(node.uri)!;
        position.set(p.x, p.y, p.z);
        const depth = -position.clone().applyMatrix4(this.camera.matrixWorldInverse).z;
        const radius = this.dotRadius(node) * Math.max(this.camera.near, depth) * unitsPerPixelAtUnitDepth;
        matrix.compose(position, this.camera.quaternion, scale.setScalar(radius));
        mesh.setMatrixAt(i, matrix);
        if (node.uri === this.selected && this.selectionRing) {
          this.selectionRing.position.copy(position);
          this.selectionRing.quaternion.copy(this.camera.quaternion);
          this.selectionRing.scale.copy(scale);
        }
      });
      mesh.instanceMatrix.needsUpdate = true;
      // Both frustum culling and pointer picking use the updated billboard bounds.
      mesh.computeBoundingSphere();
    }
  }

  private draw() {
    cancelAnimationFrame(this.frame);
    this.frame = requestAnimationFrame(() => {
      if (this.disposed) return;
      const width = this.host.clientWidth, height = this.host.clientHeight;
      this.faceDotsToCamera(height);
      this.renderer.render(this.scene, this.camera);
      const screen = (p: SpatialPoint) => { const v = new Vector3(p.x, p.y, p.z).project(this.camera); return { x: (v.x + 1) / 2 * width, y: (-v.y + 1) / 2 * height, z: v.z }; };
      // Around a focus each label sits on the far side of its resource, away from the focus, so ring labels do not pile onto the centre.
      const focusUri = this.hops && [...this.hops].find(([, hop]) => hop === 0)?.[0];
      const focusPoint = focusUri && this.positions.get(focusUri) ? screen(this.positions.get(focusUri)!) : undefined;
      const labels = this.nodes.flatMap(node => {
        const p = this.positions.get(node.uri); if (!p) return [];
        const at = screen(p), gap = this.dotRadius(node) + 3;
        const chars = [...shortName(node)].length + (this.showCategory ? categoryOf(node).length + 1 : 0);
        const size = { width: 16 + chars * 6.8, lines: 1 };
        const priority = node.uri === this.selected ? 100 : node.uri === this.hovered ? 90 : this.labelRank(node.uri);
        const dx = focusPoint ? at.x - focusPoint.x : 0, dy = focusPoint ? at.y - focusPoint.y : 0, len = Math.hypot(dx, dy);
        if (!focusPoint || node.uri === focusUri || len < 1) return [{ uri: node.uri, x: at.x, y: at.y + gap, depth: at.z, ...size, ax: -0.5, ay: 0, priority }];
        const ux = dx / len, uy = dy / len;
        return [{ uri: node.uri, x: at.x + ux * gap, y: at.y + uy * gap, depth: at.z, ...size, priority,
          ax: ux > 0.35 ? 0 : ux < -0.35 ? -1 : -0.5, ay: uy < -0.35 ? -1 : uy > 0.35 ? 0 : -0.5 }];
      });
      // Reserve the helper/empty-relationship notice and camera-control areas.
      const readable = labels.filter(label => label.y > (this.edges.length ? 44 : 110)
        && !(label.x > width - 210 && label.y > height - 140));
      this.callbacks.onLabels(visibleLabels(readable, width, height));
    });
  }

  resize() {
    const width = Math.max(1, this.host.clientWidth), height = Math.max(1, this.host.clientHeight);
    this.renderer.setSize(width, height); this.camera.aspect = width / height; this.camera.updateProjectionMatrix(); this.draw();
  }

  /** Height of the floating tools over the top of the canvas; a fit keeps the map below them. */
  setTopInset(px: number) { this.topInset = Math.max(0, px); }

  fit() {
    const height = Math.max(1, this.host.clientHeight), free = Math.max(height * 0.5, height - this.topInset);
    const view = cameraForPoints(this.nodes.flatMap(n => this.positions.has(n.uri) ? [this.positions.get(n.uri)!] : []), this.camera.aspect * height / free,
      this.dimension === 2 ? TOP_VIEW : CORNER_VIEW);
    this.adjusting = true;
    const target = new Vector3(view.target.x, view.target.y, view.target.z);
    // Fit into the area below the tools: back off by the lost height, then shift the map down by half of it.
    const position = new Vector3(view.position.x, view.position.y, view.position.z).sub(target).multiplyScalar(height / free).add(target);
    this.camera.position.copy(position); this.controls.target.copy(target);
    this.controls.update(); this.adjusting = false;
    if (height > free) this.pan(0, (height - free) / 2, false);
    this.draw();
  }

  focus(uri: string) {
    const point = this.positions.get(uri); if (!point) return;
    this.adjusting = true;
    focusSpatialCamera(this.camera, this.controls.target, point, this.host.clientWidth, this.host.clientHeight);
    this.controls.update(); this.adjusting = false; this.draw();
  }

  pan(x: number, y: number, user = true) {
    const distance = this.camera.position.distanceTo(this.controls.target);
    const scale = 2 * distance * Math.tan(Math.PI / 8) / Math.max(1, this.host.clientHeight);
    const offset = new Vector3().setFromMatrixColumn(this.camera.matrix, 0).multiplyScalar(-x * scale)
      .addScaledVector(new Vector3().setFromMatrixColumn(this.camera.matrix, 1), y * scale);
    this.adjusting = !user; this.camera.position.add(offset); this.controls.target.add(offset); this.controls.update(); this.adjusting = false; this.draw();
  }
  orbit(x: number, y: number) {
    if (this.dimension === 2) return;
    const sphere = new Spherical().setFromVector3(this.camera.position.clone().sub(this.controls.target));
    sphere.theta += x; sphere.phi = Math.max(0.1, Math.min(Math.PI - 0.1, sphere.phi + y));
    this.camera.position.copy(new Vector3().setFromSpherical(sphere).add(this.controls.target)); this.controls.update(); this.draw();
  }
  zoom(factor: number) {
    const offset = this.camera.position.clone().sub(this.controls.target);
    offset.setLength(Math.max(this.controls.minDistance, Math.min(this.controls.maxDistance, offset.length() * factor)));
    this.camera.position.copy(this.controls.target).add(offset); this.controls.update(); this.draw();
  }

  dispose() {
    this.disposed = true; cancelAnimationFrame(this.frame); cancelAnimationFrame(this.dragFrame);
    this.controls.removeEventListener("change", this.changed); this.controls.dispose(); this.clearObjects();
    const canvas = this.renderer.domElement;
    canvas.removeEventListener("pointerdown", this.pointerDown, { capture: true }); canvas.removeEventListener("pointerup", this.pointerUp);
    canvas.removeEventListener("pointercancel", this.pointerCancel); canvas.removeEventListener("lostpointercapture", this.pointerCancel);
    canvas.removeEventListener("pointermove", this.pointerMove); canvas.removeEventListener("pointerleave", this.pointerLeave);
    canvas.removeEventListener("contextmenu", this.contextMenu); canvas.removeEventListener("webglcontextlost", this.contextLost);
    this.renderer.dispose(); this.renderer.forceContextLoss(); canvas.remove();
  }
}
