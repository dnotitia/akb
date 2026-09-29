import {
  BufferGeometry, CircleGeometry, Color, DynamicDrawUsage,
  Float32BufferAttribute, Group, InstancedMesh, LineBasicMaterial, LineDashedMaterial,
  LineSegments, Matrix4, Mesh, MeshBasicMaterial, PerspectiveCamera, PlaneGeometry,
  Raycaster, RingGeometry, Scene, Spherical, Vector2, Vector3, WebGLRenderer,
  type Material, type Object3D,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import type { GraphEdge, GraphNode } from "./graph-types";
import type { SpatialPoint } from "./graph-spatial-layout";
import { cameraForPoints, edgeCurvePoints, focusSpatialCamera, visibleLabels, type ProjectedLabel } from "./graph-spatial-view";

interface Callbacks {
  onSelect(uri: string | undefined): void;
  onContext(node: GraphNode, x: number, y: number): void;
  onEdge(edge: GraphEdge): void;
  onLabels(labels: ProjectedLabel[]): void;
  onCameraChange(): void;
  onContextLost(): void;
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
  private colors: Record<string, string> = {};

  constructor(private host: HTMLDivElement, private callbacks: Callbacks) {
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
    this.scene.add(this.resources);
    this.camera.position.set(100, 60, 260);
    this.renderer.domElement.addEventListener("pointerdown", this.pointerDown);
    this.renderer.domElement.addEventListener("pointerup", this.pointerUp);
    this.renderer.domElement.addEventListener("pointermove", this.pointerMove);
    this.renderer.domElement.addEventListener("pointerleave", this.pointerLeave);
    this.renderer.domElement.addEventListener("contextmenu", this.contextMenu);
    this.renderer.domElement.addEventListener("webglcontextlost", this.contextLost);
    this.theme(); this.resize();
  }

  private changed = () => { if (!this.adjusting) this.callbacks.onCameraChange(); this.draw(); };
  private contextLost = (event: Event) => { event.preventDefault(); this.callbacks.onContextLost(); };
  private contextMenu = (event: Event) => event.preventDefault();
  private pointerDown = (event: PointerEvent) => { this.down = { x: event.clientX, y: event.clientY, button: event.button }; };
  private pointerUp = (event: PointerEvent) => {
    const down = this.down; this.down = undefined;
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
    if (event.buttons) return;
    const hit = this.hit(event), uri = hit.node?.uri;
    this.host.style.cursor = hit.node || hit.edge ? "pointer" : "grab";
    if (uri !== this.hovered) { this.hovered = uri; this.draw(); }
  };
  private pointerLeave = () => { this.hovered = undefined; this.host.style.cursor = "grab"; this.draw(); };

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

  theme() {
    const css = getComputedStyle(document.documentElement);
    this.colors = Object.fromEntries(["surface", "cat-1", "cat-3", "cat-4", "graph-edge", "link"].map(key => [key, css.getPropertyValue(`--color-${key}`).trim()]));
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
        const color = new Color(this.colors[kind === "document" ? "cat-1" : kind === "table" ? "cat-3" : "cat-4"]);
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
    for (const implicit of [false, true]) {
      const vertices: number[] = [], colors: number[] = [], indices: number[] = [];
      this.edges.forEach((edge, edgeIndex) => {
        if ((edge.kind === "implicit") !== implicit) return;
        const a = this.positions.get(edge.source), b = this.positions.get(edge.target);
        if (!a || !b) return;
        const parallel = pairs.get(JSON.stringify([edge.source, edge.target].sort()))!;
        const points = edgeCurvePoints(a, b, parallel.indexOf(edgeIndex), parallel.length);
        const active = edge.source === this.selected || edge.target === this.selected || this.isInspected(edge);
        const color = new Color(this.colors[active ? "link" : "graph-edge"]);
        if (!active) color.lerp(background, 0.25);
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
      const labels = this.nodes.flatMap(node => {
        const p = this.positions.get(node.uri); if (!p) return [];
        const projected = new Vector3(p.x, p.y, p.z).project(this.camera);
        const labelOffset = this.dotRadius(node) + 3;
        return [{ uri: node.uri, x: (projected.x + 1) / 2 * width, y: (-projected.y + 1) / 2 * height + labelOffset, depth: projected.z,
          priority: node.uri === this.selected ? 100 : node.uri === this.hovered ? 90 : this.neighbors.has(node.uri) ? 50 : this.pinned.has(node.uri) ? 20 : 0 }];
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

  fit() {
    const view = cameraForPoints(this.nodes.flatMap(n => this.positions.has(n.uri) ? [this.positions.get(n.uri)!] : []), this.camera.aspect);
    this.adjusting = true;
    this.camera.position.set(view.position.x, view.position.y, view.position.z); this.controls.target.set(view.target.x, view.target.y, view.target.z);
    this.controls.update(); this.adjusting = false; this.draw();
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
    this.disposed = true; cancelAnimationFrame(this.frame);
    this.controls.removeEventListener("change", this.changed); this.controls.dispose(); this.clearObjects();
    const canvas = this.renderer.domElement;
    canvas.removeEventListener("pointerdown", this.pointerDown); canvas.removeEventListener("pointerup", this.pointerUp);
    canvas.removeEventListener("pointermove", this.pointerMove); canvas.removeEventListener("pointerleave", this.pointerLeave);
    canvas.removeEventListener("contextmenu", this.contextMenu); canvas.removeEventListener("webglcontextlost", this.contextLost);
    this.renderer.dispose(); this.renderer.forceContextLoss(); canvas.remove();
  }
}
