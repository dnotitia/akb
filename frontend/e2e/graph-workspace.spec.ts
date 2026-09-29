import { expect, test, type Page, type Route } from "@playwright/test";
import type { GraphApiEdge, GraphApiNode, GraphOverviewResponse } from "../src/lib/api";

// Real browser/renderer, isolated HTTP responses. No request reaches a Vault backend.
test.skip(process.env.AKB_FE_E2E_MODE === "mock", "HTTP fixtures require the frontend without its MSW worker.");
test.use({
  reducedMotion: "reduce",
  launchOptions: { args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] },
});

type FixtureNode = GraphApiNode & { name: string; resource_type: "document" | "table" | "file" };
type Scene = GraphOverviewResponse & { nodes: FixtureNode[] };

function fixtureScene(count = 30, connectedCount = 24): Scene {
  const nodes: FixtureNode[] = Array.from({ length: count }, (_, index) => {
    const resource_type = (["document", "table", "file"] as const)[index % 3];
    const segment = resource_type === "document" ? "doc" : resource_type;
    const leaf = index === 0 ? "api-gateway.md" : index === 1 ? "service_metrics"
      : index === 2 ? "asset-service-diagram" : resource_type === "document" ? `note-${index}.md` : `resource-${index}`;
    const name = index === 0 ? "API Gateway" : index === 1 ? "Service metrics"
      : index === 2 ? "Service diagram.png" : `리소스 ${index} · ${resource_type}`;
    return { uri: `akb://fixture/coll/architecture/${segment}/${leaf}`, name, resource_type, degree: 0 };
  });
  const edges: GraphApiEdge[] = Array.from({ length: Math.max(0, connectedCount - 1) }, (_, index) => ({
    source: nodes[index].uri, target: nodes[index + 1].uri,
    relation: index % 3 === 0 ? "depends_on" : "references",
    kind: index % 3 === 0 ? "explicit" : "implicit",
  }));
  if (connectedCount >= 2) edges.push(
    { source: nodes[0].uri, target: nodes[1].uri, relation: "references", kind: "explicit" },
    { source: nodes[0].uri, target: nodes[1].uri, relation: "references", kind: "implicit" },
  );
  for (const edge of edges) {
    for (const uri of [edge.source, edge.target]) {
      const node = nodes.find(item => item.uri === uri)!;
      node.degree = (node.degree ?? 0) + 1;
    }
  }
  return {
    nodes, edges, nodes_total: connectedCount, returned: connectedCount, edges_total: edges.length,
    truncated: false, orphans_returned: count - connectedCount, orphans_truncated: false,
  };
}

function neighborhood(scene: Scene, uri: string) {
  const node = scene.nodes.find(item => item.uri === uri) ?? scene.nodes[0];
  const next = scene.nodes[(scene.nodes.indexOf(node) + 1) % scene.nodes.length];
  return { nodes: [node, next], edges: [{ source: node.uri, target: next.uri, relation: "references", kind: "explicit" }] };
}

async function fixture(page: Page, scene = fixtureScene(), options: {
  dark?: boolean;
  onGraph?: (route: Route, url: URL) => Promise<boolean>;
} = {}) {
  const requests: URL[] = [];
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(dark => {
    localStorage.setItem("akb_token", "graph-browser-fixture");
    localStorage.setItem("akb_theme", dark ? "dark" : "light");
  }, options.dark ?? false);
  await page.route("**/health/**", route => route.fulfill({ json: {} }));
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    requests.push(url);
    const path = url.pathname;
    if (path.endsWith("/auth/config")) return route.fulfill({ json: {
      schema_version: 2, auth_mode: "local", local_auth: { enabled: true },
      keycloak: { enabled: false, browser_session_ready: false }, providers: [], mcp_oauth: { enabled: false },
    } });
    if (path.endsWith("/auth/me")) return route.fulfill({ json: {
      user_id: "graph-fixture-user", username: "reviewer", display_name: "Graph reviewer",
      email: "reviewer@example.invalid", auth_method: "local", key_class: null, is_admin: false,
    } });
    if (path.endsWith("/vaults")) return route.fulfill({ json: { vaults: [{ id: "graph-fixture-vault", name: "fixture", role: "owner" }] } });
    if (path.endsWith("/info")) return route.fulfill({ json: {
      name: "fixture", description: "Isolated graph browser fixture", role: "owner", public_access: "none",
      is_archived: false, is_external_git: false, collection_count: 1,
      document_count: scene.nodes.filter(node => node.resource_type === "document").length,
      table_count: scene.nodes.filter(node => node.resource_type === "table").length,
      file_count: scene.nodes.filter(node => node.resource_type === "file").length, member_count: 1,
    } });
    if (path.includes("/browse/")) return route.fulfill({ json: { items: [
      { type: "collection", path: "architecture", name: "architecture" },
    ] } });
    if (path.endsWith("/graph/overview")) return route.fulfill({ json: scene });
    if (path.endsWith("/graph")) {
      if (await options.onGraph?.(route, url)) return;
      return route.fulfill({ json: neighborhood(scene, url.searchParams.get("uri") ?? scene.nodes[0].uri) });
    }
    if (path.endsWith("/relations")) {
      const uri = url.searchParams.get("uri");
      const relations = scene.edges.flatMap(edge => {
        const direction = edge.source === uri ? "outgoing" : edge.target === uri ? "incoming" : null;
        if (!direction) return [];
        const other = scene.nodes.find(node => node.uri === (direction === "outgoing" ? edge.target : edge.source))!;
        return [{ uri: other.uri, name: other.name, resource_type: other.resource_type, direction, relation: edge.relation, kind: edge.kind }];
      });
      return route.fulfill({ json: { uri, relations } });
    }
    if (path.includes("/documents/fixture/")) {
      const id = decodeURIComponent(path.split("/documents/fixture/")[1]);
      const node = scene.nodes.find(item => item.resource_type === "document" && item.uri.endsWith(`/doc/${id.split("/").at(-1)}`));
      return route.fulfill({ json: {
        id: "fixture-document", path: id, title: node?.name ?? "Workspace guide",
        content: "# Reading the graph\n\nThe preview preserves the current map and selected resource.",
        summary: "Document content from an isolated HTTP fixture.", metadata: {}, tags: [],
        current_commit: "fixture-commit", status: "active", doc_type: "note",
        created_at: "2026-09-01T09:00:00Z", updated_at: "2026-09-28T09:00:00Z",
      } });
    }
    if (path.endsWith("/search")) return route.fulfill({ json: { results: [] } });
    return route.fulfill({ json: { items: [], history: [], relations: [], vaults: [], subscribed: false, unread_count: 0 } });
  });
  return { requests, errors };
}

// These are public projected label buttons, not renderer state or test hooks.
async function labelPositions(page: Page) {
  return page.getByLabel("Visible graph labels", { exact: true }).evaluate(element =>
    [...element.querySelectorAll<HTMLButtonElement>("button[data-graph-node]")].flatMap(button => {
      const box = button.getBoundingClientRect();
      const style = getComputedStyle(button);
      if (!box.width || !box.height || style.visibility === "hidden" || style.display === "none") return [];
      return [{
        uri: button.dataset.graphNode!,
        title: (button.getAttribute("aria-label") ?? "").replace(/^Inspect /, ""),
        x: Number((box.x + box.width / 2).toFixed(2)),
        y: Number((box.y + box.height / 2).toFixed(2)),
        width: box.width, height: box.height,
        fontPixels: Number.parseFloat(style.fontSize),
      }];
    }).sort((a, b) => a.uri.localeCompare(b.uri)),
  );
}

// Read the actual public canvas bitmap. A locator screenshot can also contain
// sibling overlays/tooltips, which are not graph geometry.
async function canvasPixels(page: Page) {
  const data = await page.getByTestId("graph-canvas").locator("canvas").first()
    .evaluate(canvas => (canvas as HTMLCanvasElement).toDataURL("image/png"));
  return Buffer.from(data.split(",")[1], "base64");
}

async function frames(page: Page) {
  await page.evaluate(() => new Promise<void>(resolve =>
    requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
  ));
}

async function stablePixels(page: Page) {
  let before = await canvasPixels(page);
  await expect.poll(async () => {
    await frames(page);
    const after = await canvasPixels(page);
    const stable = before.equals(after);
    before = after;
    return stable;
  }, { message: "The 3D scene must settle without perpetual camera or layout motion", timeout: 15_000 }).toBe(true);
  return before;
}

async function expectNonblankCanvas(page: Page) {
  const png = await canvasPixels(page);
  const pixels = await page.evaluate(async encoded => {
    const blob = new Blob([Uint8Array.from(atob(encoded), character => character.charCodeAt(0))], { type: "image/png" });
    const bitmap = await createImageBitmap(blob);
    const stage = new OffscreenCanvas(bitmap.width, bitmap.height);
    const context = stage.getContext("2d")!;
    context.drawImage(bitmap, 0, 0);
    const data = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
    const colors = new Set<number>();
    let foreground = 0;
    let left = bitmap.width, right = -1, top = bitmap.height, bottom = -1;
    for (let index = 0; index < data.length; index += 4) {
      colors.add(((data[index] >> 3) << 10) | ((data[index + 1] >> 3) << 5) | (data[index + 2] >> 3));
      if (Math.abs(data[index] - data[0]) + Math.abs(data[index + 1] - data[1]) + Math.abs(data[index + 2] - data[2]) > 30) {
        foreground++;
        const x = (index / 4) % bitmap.width, y = Math.floor(index / 4 / bitmap.width);
        left = Math.min(left, x); right = Math.max(right, x);
        top = Math.min(top, y); bottom = Math.max(bottom, y);
      }
    }
    const bounds = { x: left, y: top, width: right - left + 1, height: bottom - top + 1 };
    bitmap.close();
    return { colors: colors.size, foreground, bounds };
  }, png.toString("base64"));
  expect(pixels.colors, "The canvas must contain visible graph marks, not a blank background").toBeGreaterThan(1);
  expect(pixels.foreground, "Graph marks must remain visible with DOM labels hidden").toBeGreaterThan(20);
  return { ...pixels.bounds, fillRatio: pixels.foreground / (pixels.bounds.width * pixels.bounds.height) };
}

async function graphReady(page: Page, count: number) {
  const host = page.getByTestId("graph-canvas");
  await expect(host).toBeVisible();
  await expect(host).toHaveAccessibleName(new RegExp("^3D knowledge graph: " + count + " resources?\\b"));
  const canvas = host.locator("canvas").first();
  await expect(canvas).toBeVisible();
  await expect.poll(() => canvas.evaluate(element => {
    const context = (element as HTMLCanvasElement).getContext("webgl2");
    if (!(context instanceof WebGL2RenderingContext)) return null;
    return {
      version: String(context.getParameter(context.VERSION)),
      drawing: context.drawingBufferWidth > 0 && context.drawingBufferHeight > 0,
      lost: context.isContextLost(),
    };
  })).toEqual({ version: expect.stringContaining("WebGL 2.0"), drawing: true, lost: false });
  await expect(page.getByText(/Arranging.*map/)).toBeHidden({ timeout: 60_000 });
  await expect(page.getByRole("alert").filter({ hasText: /layout could not|WebGL|3D renderer/i })).toHaveCount(0);
  if (count > 0) {
    await expect.poll(async () => (await labelPositions(page)).length).toBeGreaterThan(0);
    const labels = await labelPositions(page);
    expect(labels.length).toBeLessThanOrEqual(Math.min(30, count));
    expect(labels.every(label => Number.isFinite(label.x) && Number.isFinite(label.y))).toBe(true);
    await expectNonblankCanvas(page);
  } else {
    await expect(page.getByLabel("Visible graph labels", { exact: true }).locator("button[data-graph-node]")).toHaveCount(0);
  }
}

async function expectReadableLabels(page: Page) {
  const labels = await labelPositions(page);
  expect(labels.length).toBeGreaterThan(0);
  expect(labels.every(label => label.fontPixels >= 12 && label.height >= 20)).toBe(true);
  await expect(page.getByLabel("Pan map", { exact: true })).toBeVisible();
}

async function selectFromSearch(page: Page, name: string) {
  await page.getByRole("combobox", { name: "Find a resource" }).fill(name);
  await page.getByRole("option").filter({ hasText: name }).first().click();
  await expect(page.getByRole("complementary", { name: "Inspector for " + name, exact: true })).toBeVisible();
}

async function clickRenderedDocument(page: Page) {
  const labels = await labelPositions(page);
  const host = await page.getByTestId("graph-canvas").boundingBox();
  // Prefer the inspector's safe region, so selection need not move the camera.
  const documents = labels.filter(label => label.uri.includes("/doc/"));
  const target = documents.find(label => label.x < host!.x + host!.width - 380) ?? documents[0];
  expect(target, "A projected document label should be available without a test-only engine API").toBeDefined();
  await page.getByLabel("Visible graph labels", { exact: true }).getByRole("button", { name: "Inspect " + target.title, exact: true }).click();
  await expect(page.getByRole("complementary", { name: "Inspector for " + target.title, exact: true })).toBeVisible();
  return target;
}

async function rotateByDragging(page: Page, axis: "x" | "y") {
  const host = page.getByTestId("graph-canvas");
  const point = await host.evaluate(element => {
    const box = element.getBoundingClientRect();
    for (const xRatio of [0.3, 0.5, 0.15, 0.65]) {
      for (const yRatio of [0.4, 0.25, 0.6]) {
        const x = box.x + box.width * xRatio, y = box.y + box.height * yRatio;
        const target = document.elementFromPoint(x, y);
        if (target?.tagName === "CANVAS" && element.contains(target)) return { x, y };
      }
    }
    return null;
  });
  expect(point, "Rotation must start on a visible part of the real canvas").not.toBeNull();
  await page.mouse.move(point!.x, point!.y);
  await page.mouse.down();
  await page.mouse.move(point!.x + (axis === "x" ? 100 : 0), point!.y + (axis === "y" ? 90 : 0), { steps: 12 });
  await page.mouse.up();
  await frames(page);
}

test("renders a genuine WebGL2 3D knowledge graph", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const { errors } = await fixture(page);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  expect(errors).toEqual([]);
});

test("resource kinds have visible canvas colors that survive selection", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page, fixtureScene(3, 0));
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 3);
  async function pixelsFor(token: string) {
    return page.getByTestId("graph-canvas").locator("canvas").evaluate((canvas, name) => {
      const bitmap = canvas as HTMLCanvasElement;
      const stage = new OffscreenCanvas(bitmap.width, bitmap.height);
      const context = stage.getContext("2d")!;
      context.fillStyle = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      context.fillRect(0, 0, 1, 1);
      const color = context.getImageData(0, 0, 1, 1).data;
      context.clearRect(0, 0, bitmap.width, bitmap.height);
      context.drawImage(bitmap, 0, 0);
      const pixels = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
      let matching = 0;
      for (let i = 0; i < pixels.length; i += 4) {
        if ([0, 1, 2].every(channel => Math.abs(pixels[i + channel] - color[channel]) <= 2)) matching++;
      }
      return matching;
    }, token);
  }
  for (const token of ["--color-cat-1", "--color-cat-3", "--color-cat-4"]) {
    await expect.poll(() => pixelsFor(token), { message: token + " must appear on actual resource marks" }).toBeGreaterThan(15);
  }
  await selectFromSearch(page, "Service metrics");
  await expect.poll(() => pixelsFor("--color-cat-3"), { message: "A selected table must retain its type color" }).toBeGreaterThan(15);
  await expect.poll(() => pixelsFor("--color-link"), { message: "Selection must add a separate visible marker" }).toBeGreaterThan(15);
});

for (const [index, kind, minimum, maximum] of [
  [0, "document", 0.65, 0.9], [1, "table", 0.92, 1], [2, "file", 0.4, 0.65],
] as const) {
  test(kind + " is distinguishable by silhouette without relying on color", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const scene = fixtureScene(3, 0);
    scene.nodes = [scene.nodes[index]];
    scene.orphans_returned = 1;
    await fixture(page, scene);
    await page.goto("/vault/fixture/graph");
    await graphReady(page, 1);
    const mark = await expectNonblankCanvas(page);
    // Circle, square, diamond have different filled areas in their bounding boxes.
    expect(mark.fillRatio).toBeGreaterThanOrEqual(minimum);
    expect(mark.fillRatio).toBeLessThanOrEqual(maximum);
  });
}

test("dragging horizontally and vertically rotates the rendered 3D scene", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const { errors } = await fixture(page);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  await page.screenshot({ path: testInfo.outputPath("graph-3d-before-rotation.png"), animations: "disabled" });
  for (const axis of ["x", "y"] as const) {
    const before = await stablePixels(page);
    const labelsBefore = await labelPositions(page);
    await rotateByDragging(page, axis);
    await expect.poll(async () => before.equals(await canvasPixels(page)),
      { message: axis + " drag must change the WebGL-rendered geometry" }).toBe(false);
    const labelsAfter = await labelPositions(page);
    expect(labelsAfter.some(after => {
      const prior = labelsBefore.find(label => label.uri === after.uri);
      return prior && Math.hypot(after.x - prior.x, after.y - prior.y) > 2;
    }), axis + " rotation must move the public projected resource labels").toBe(true);
    await page.screenshot({ path: testInfo.outputPath("graph-3d-after-" + axis + "-rotation.png"), animations: "disabled" });
  }
  expect(errors).toEqual([]);
});

for (const dark of [false, true]) {
  test("3D canvas labels support selection and readable inspection (" + (dark ? "dark" : "light") + ")", async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const { errors } = await fixture(page, fixtureScene(), { dark });
    await page.goto("/vault/fixture/graph");
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--color-graph-edge").trim()),
      "The WebGL edge color must be emitted for both themes, including JS-only token consumers").not.toBe("");
    await graphReady(page, 30);
    await expectReadableLabels(page);
    await expect(page.getByRole("complementary", { name: /^Inspector for/ })).toHaveCount(0);
    await clickRenderedDocument(page);
    await page.screenshot({ path: testInfo.outputPath("graph-3d-inspector.png"), animations: "disabled" });
    await page.getByRole("button", { name: "Close inspector", exact: true }).click();
    await expect(page.getByRole("complementary", { name: /^Inspector for/ })).toHaveCount(0);
    await expectNonblankCanvas(page);
    expect(errors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath("graph-3d-workspace.png"), animations: "disabled" });
  });
}

test("document preview returns to the same 3D pixels, projected labels, and selection", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const { errors } = await fixture(page);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  const target = await clickRenderedDocument(page);
  const before = await stablePixels(page);
  const labelsBefore = await labelPositions(page);
  const previousUrl = page.url();
  await page.getByRole("button", { name: "Preview", exact: true }).click();
  const preview = page.getByRole("dialog", { name: "Document preview", exact: true });
  await expect(preview).toBeVisible();
  await expect(preview.getByText("The preview preserves the current map and selected resource.", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(preview).toBeHidden();
  await expect(page).toHaveURL(previousUrl);
  await expect(page.getByRole("complementary", { name: "Inspector for " + target.title, exact: true })).toBeVisible();
  await expect.poll(async () => before.equals(await canvasPixels(page))).toBe(true);
  expect(await labelPositions(page)).toEqual(labelsBefore);
  expect(errors).toEqual([]);
});

test("a zero-edge Vault keeps isolated resources selectable in 3D", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const { errors } = await fixture(page, fixtureScene(12, 0));
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 12);
  await expect(page.getByTestId("graph-canvas")).toHaveAccessibleName(/0 relationships/);
  await clickRenderedDocument(page);
  await expect(page.getByText("No direct connections", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Preview", exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test("an empty Vault shows its empty state without inventing resources", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const { errors } = await fixture(page, fixtureScene(0, 0));
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 0);
  await expect(page.getByText("There is nothing to map yet", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Back to Overview", exact: true })).toBeVisible();
  await expect(page.getByTestId("graph-canvas")).toHaveAccessibleName(/0 relationships/);
  await expect(page.getByRole("complementary", { name: /^Inspector for/ })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("a single unlinked resource remains readable and selectable", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const { errors } = await fixture(page, fixtureScene(1, 0));
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 1);
  await expectReadableLabels(page);
  await clickRenderedDocument(page);
  await expect(page.getByRole("complementary", { name: "Inspector for API Gateway", exact: true })).toBeVisible();
  await expect(page.getByText("No direct connections", { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test("an isolated dot stays small when fitted or zoomed and remains directly clickable", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page, fixtureScene(1, 0));
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 1);
  for (let step = 0; step < 4; step++) {
    await stablePixels(page);
    const mark = await expectNonblankCanvas(page);
    expect(mark.width, "Fit/zoom must not inflate a single resource into a giant object").toBeLessThanOrEqual(20);
    expect(mark.width).toBeGreaterThanOrEqual(6);
    expect(Math.abs(mark.width - mark.height), "The dot must face the reader").toBeLessThanOrEqual(2);
    if (step < 3) await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  }
  await page.getByRole("button", { name: "Rotate up", exact: true }).click();
  await stablePixels(page);
  const mark = await expectNonblankCanvas(page);
  const host = await page.getByTestId("graph-canvas").boundingBox();
  await page.mouse.click(host!.x + mark.x + mark.width / 2, host!.y + mark.y + mark.height / 2);
  await expect(page.getByRole("complementary", { name: "Inspector for API Gateway", exact: true })).toBeVisible();
});

test("filters and hidden resources produce the same scene in Graph and List", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  await page.getByRole("button", { name: /^Filters/ }).click();
  await page.getByRole("menuitemcheckbox", { name: "table", exact: true }).click();
  await page.getByRole("menuitemcheckbox", { name: "file", exact: true }).click();
  await page.getByRole("menuitemcheckbox", { name: /Hide 6 unconnected resources/ }).click();
  await page.keyboard.press("Escape");
  await graphReady(page, 8);
  await expect(page.getByTestId("graph-canvas")).toHaveAccessibleName(/0 relationships/);
  await selectFromSearch(page, "API Gateway");
  await page.getByRole("button", { name: "Resource actions", exact: true }).click();
  await page.getByRole("menuitem", { name: "Hide from this view", exact: true }).click();
  await graphReady(page, 7);
  await page.getByRole("button", { name: "List", exact: true }).click();
  const list = page.getByRole("region", { name: "Relationship index", exact: true });
  await expect(list.locator("li > button")).toHaveCount(7);
  await expect(list).not.toContainText("API Gateway");
  for (const index of [3, 6, 9, 12, 15, 18, 21]) {
    await expect(list.getByText("리소스 " + index + " · document", { exact: true })).toBeVisible();
  }
});

test("toolbar groups adapt to workspace width and expose secondary tools", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1680, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  const search = page.getByRole("combobox", { name: "Find a resource" });
  const graph = page.getByRole("button", { name: "Graph", exact: true });
  const searchBox = await search.boundingBox(), graphBox = await graph.boundingBox();
  expect(Math.abs(searchBox!.y + searchBox!.height / 2 - graphBox!.y - graphBox!.height / 2)).toBeLessThan(2);
  expect(graphBox!.x + graphBox!.width).toBeLessThan(searchBox!.x);
  expect(searchBox!.width).toBeLessThanOrEqual(448);
  await page.getByRole("button", { name: /^Filters/ }).click();
  await expect(page.getByRole("menuitemcheckbox", { name: "table", exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("graph-filter-menu.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "More graph actions" }).click();
  for (const name of [/Fit view/, /Rearrange/, /Manage saved views/, /Graph help/]) {
    await expect(page.getByRole("menuitem", { name })).toBeVisible();
  }
  await page.screenshot({ path: testInfo.outputPath("graph-more-menu.png"), animations: "disabled" });
  await page.keyboard.press("Escape");
  // The app collapses its navigation rails at intermediate viewport widths;
  // use a genuinely narrow workspace rather than assuming those rails persist.
  await page.setViewportSize({ width: 620, height: 900 });
  await expect.poll(async () => (await search.boundingBox())!.y - (await graph.boundingBox())!.y).toBeGreaterThan(40);
  for (const name of ["Graph", "List", "Filters", "More graph actions", "Show documents", "Show tables", "Show files"]) {
    await expect(page.getByRole("button", { name, exact: true })).toBeInViewport({ ratio: 1 });
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("graph-narrow-workspace.png"), animations: "disabled" });
});

test("floating tools preserve the canvas and keep list rows and inspectors reachable", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  const workspace = page.locator("#graph-workspace");
  const canvas = page.getByTestId("graph-canvas");
  const tools = workspace.locator("header");
  const workspaceBox = await workspace.boundingBox(), canvasBox = await canvas.boundingBox();
  expect(Math.abs(canvasBox!.y - workspaceBox!.y)).toBeLessThan(2);
  expect(Math.abs(canvasBox!.height - workspaceBox!.height)).toBeLessThan(2);
  const toolsBox = await tools.boundingBox();
  expect(toolsBox!.y).toBeGreaterThan(canvasBox!.y);
  expect(toolsBox!.x).toBeGreaterThan(canvasBox!.x);

  await selectFromSearch(page, "API Gateway");
  const inspector = page.getByRole("complementary", { name: "Inspector for API Gateway", exact: true });
  expect((await inspector.boundingBox())!.y).toBeGreaterThan(toolsBox!.y + toolsBox!.height);
  await page.getByRole("combobox", { name: "Find a resource" }).fill("Service");
  const suggestion = page.getByRole("option").filter({ hasText: "Service metrics" }).first();
  await expect(suggestion).toBeVisible();
  expect(await suggestion.evaluate(element => {
    const bounds = element.getBoundingClientRect();
    return element.contains(document.elementFromPoint(bounds.right - 8, bounds.y + bounds.height / 2));
  }), "Search suggestions must appear above an already-open inspector").toBe(true);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Close inspector", exact: true }).click();
  await page.getByRole("button", { name: "List", exact: true }).click();
  const list = page.getByRole("region", { name: "Relationship index", exact: true });
  for (const width of [1440, 620, 375]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(async () => {
      const toolbarBounds = (await tools.boundingBox())!;
      const listBounds = (await list.boundingBox())!;
      return listBounds.y - toolbarBounds.y - toolbarBounds.height;
    }).toBeGreaterThan(0);
    // Scrolling cannot move a keyboard-focused row under the floating controls.
    await list.locator("li > button").last().focus();
    await expect(list.locator("li > button").last()).toBeInViewport();
    await list.locator("li > button").first().focus();
    await expect(list.locator("li > button").first()).toBeInViewport();
  }
  await page.screenshot({ path: testInfo.outputPath("floating-tools-mobile-list.png"), animations: "disabled" });
  await page.getByRole("button", { name: "More graph actions" }).click();
  await page.getByRole("menuitem", { name: "Graph help" }).click();
  const help = page.getByRole("dialog", { name: "Explore relationships" });
  await expect(help).toBeVisible();
  expect(await help.evaluate(element => {
    const bounds = element.getBoundingClientRect();
    return element.contains(document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + 24));
  }), "Page-local floating tools must not sit above a modal").toBe(true);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "More graph actions" })).toBeFocused();
});

test("the visible kind legend filters the map and keeps loaded counts recoverable", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  const tables = page.getByRole("button", { name: "Show tables", exact: true });
  const files = page.getByRole("button", { name: "Show files", exact: true });
  const documents = page.getByRole("button", { name: "Show documents", exact: true });
  for (const button of [documents, tables, files]) {
    await expect(button).toHaveAttribute("aria-pressed", "true");
    await expect(button).toContainText("10");
  }
  await tables.click();
  await expect(tables).toHaveAttribute("aria-pressed", "false");
  await graphReady(page, 20);
  await expect(tables).toContainText("10");
  await files.focus();
  await page.keyboard.press("Space");
  await expect(files).toHaveAttribute("aria-pressed", "false");
  await graphReady(page, 10);
  await documents.click();
  await expect(documents).toHaveAttribute("aria-pressed", "false");
  await graphReady(page, 0);
  await documents.click();
  await graphReady(page, 10);
  await page.getByRole("button", { name: "List", exact: true }).click();
  await expect(page.getByRole("region", { name: "Relationship index", exact: true }).locator("li > button")).toHaveCount(10);
});

test("mobile starts in List and offers a usable 3D Graph and inspector", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const { errors } = await fixture(page, fixtureScene(12, 0));
  await page.goto("/vault/fixture/graph");
  await expect(page.getByRole("button", { name: "List", exact: true })).toHaveAttribute("aria-pressed", "true");
  const list = page.getByRole("region", { name: "Relationship index", exact: true });
  await expect(list.locator("li > button")).toHaveCount(12);
  for (const [title, kind] of [["API Gateway", "document"], ["Service metrics", "table"], ["Service diagram.png", "file"]]) {
    await expect(list.getByRole("button", { name: new RegExp(title) }).getByText(kind, { exact: true }).first()).toBeVisible();
  }
  await list.getByRole("button", { name: /API Gateway/ }).click();
  const inspector = page.getByRole("complementary", { name: "Inspector for API Gateway", exact: true });
  await expect(inspector.getByRole("button", { name: "Open in vault", exact: true })).toBeInViewport({ ratio: 1 });
  await page.screenshot({ path: testInfo.outputPath("graph-mobile-inspector.png"), animations: "disabled" });
  await inspector.getByRole("button", { name: "Close inspector", exact: true }).click();
  await page.getByRole("button", { name: "Graph", exact: true }).click();
  await graphReady(page, 12);
  await expectReadableLabels(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("graph-mobile-3d.png"), animations: "disabled" });
  expect(errors).toEqual([]);
});

test("3D rotation, zoom, fit and pan have visible button alternatives", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  for (const name of ["Rotate left", "Rotate right", "Rotate up", "Rotate down", "Zoom in", "Zoom out"]) {
    const before = await stablePixels(page);
    await page.getByRole("button", { name, exact: true }).click();
    await expect.poll(async () => before.equals(await canvasPixels(page)), { message: name + " must move the real 3D view" }).toBe(false);
  }
  await page.getByRole("button", { name: "Fit graph", exact: true }).click();
  const fitted = await stablePixels(page);
  await page.getByRole("button", { name: "Fit graph", exact: true }).click();
  await expect.poll(async () => fitted.equals(await canvasPixels(page))).toBe(true);
  await page.getByLabel("Pan map", { exact: true }).click();
  const beforePan = await stablePixels(page);
  await page.getByRole("button", { name: "Pan left", exact: true }).click();
  await expect.poll(async () => beforePan.equals(await canvasPixels(page))).toBe(false);
});

test("table and file connection loads preserve their canonical resource URI", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const scene = fixtureScene();
  const { requests } = await fixture(page, scene);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  for (const node of [scene.nodes[1], scene.nodes[2]]) {
    await selectFromSearch(page, node.name);
    await page.getByRole("button", { name: "Load connections", exact: true }).click();
    await expect.poll(() => requests.some(url => url.pathname.endsWith("/graph") && url.searchParams.get("uri") === node.uri && url.searchParams.get("limit") === "100")).toBe(true);
    await expect(page.getByRole("button", { name: "Load connections", exact: true })).toBeEnabled();
  }
});

test("an expansion arriving after focus changes cannot join the new scene", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const scene = fixtureScene();
  let pending: Route | undefined;
  await fixture(page, scene, { onGraph: async (route, url) => {
    if (url.searchParams.get("limit") !== "100") return false;
    pending = route;
    return true;
  } });
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  await selectFromSearch(page, scene.nodes[0].name);
  await page.getByRole("button", { name: "Load connections", exact: true }).click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  await selectFromSearch(page, scene.nodes[1].name);
  await page.getByRole("button", { name: "Explore connections", exact: true }).click();
  await graphReady(page, 2);
  await expect(page).toHaveURL(new RegExp("entry=" + encodeURIComponent(scene.nodes[1].uri)));
  const lateUri = "akb://fixture/doc/late-response.md";
  await pending!.fulfill({ json: {
    nodes: [scene.nodes[0], { uri: lateUri, name: "Late response", resource_type: "document" }],
    edges: [{ source: scene.nodes[0].uri, target: lateUri, relation: "references", kind: "explicit" }],
  } });
  await frames(page);
  await graphReady(page, 2);
  await page.getByRole("button", { name: "List", exact: true }).click();
  const list = page.getByRole("region", { name: "Relationship index", exact: true });
  await expect(list.locator("li > button")).toHaveCount(2);
  await expect(list.getByText("Service metrics", { exact: true })).toBeVisible();
  await expect(list.getByText("Service diagram.png", { exact: true })).toBeVisible();
  await expect(list).not.toContainText("Late response");
});

test("List stays selected when exploring connections and changing traversal depth", async ({ page }) => {
  await fixture(page);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  await page.getByRole("button", { name: "List", exact: true }).click();
  await selectFromSearch(page, "Service metrics");
  await page.getByRole("button", { name: "Explore connections", exact: true }).click();
  await expect(page).toHaveURL(/entry=/);
  await expect(page.getByRole("button", { name: "List", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "2 hop neighborhood", exact: true }).click();
  await expect(page).toHaveURL(/hops=2/);
  await expect(page.getByRole("button", { name: "List", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("region", { name: "Relationship index", exact: true })).toBeVisible();
});

test("following a relationship restores its hidden target to the scene", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await fixture(page);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  await selectFromSearch(page, "Service metrics");
  await page.getByRole("button", { name: "Resource actions", exact: true }).click();
  await page.getByRole("menuitem", { name: "Hide from this view", exact: true }).click();
  await graphReady(page, 29);
  await expect(page.getByLabel("Visible graph labels", { exact: true }).getByRole("button", { name: "Inspect Service metrics", exact: true })).toHaveCount(0);
  await selectFromSearch(page, "API Gateway");
  const inspector = page.getByRole("complementary", { name: "Inspector for API Gateway", exact: true });
  await inspector.getByRole("button", { name: "Service metrics", exact: true }).first().click();
  await expect(page.getByRole("complementary", { name: "Inspector for Service metrics", exact: true })).toBeVisible();
  await graphReady(page, 30);
  await expect(page.getByLabel("Visible graph labels", { exact: true }).getByRole("button", { name: "Inspect Service metrics", exact: true })).toBeVisible();
});

for (const direction of ["outgoing", "incoming"] as const) {
  test(`following ${direction} relations preserves provenance without duplicating loaded connections`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const scene = fixtureScene(2, 2);
    scene.edges = scene.edges.filter(edge => edge.relation === "references");
    scene.edges_total = 2;
    scene.nodes.forEach(node => { node.degree = 2; });
    const start = scene.nodes[direction === "outgoing" ? 0 : 1];
    const target = scene.nodes[direction === "outgoing" ? 1 : 0];
    const { errors } = await fixture(page, scene, { onGraph: async route => {
      await route.fulfill({ json: scene });
      return true;
    } });
    // A partial map can omit a neighbor that the inspector already knows about.
    await page.route("**/graph/overview**", route => route.fulfill({ json: {
      ...scene, nodes: [start], edges: [], returned: 1, truncated: true,
    } }));
    await page.goto("/vault/fixture/graph");
    await graphReady(page, 1);
    const canvas = page.getByTestId("graph-canvas");
    await selectFromSearch(page, start.name);
    const inspector = page.getByRole("complementary", { name: `Inspector for ${start.name}`, exact: true });
    const labels = direction === "outgoing" ? ["Explicit relation", "Body link"] : ["Body link", "Explicit relation"];
    await inspector.getByRole("button", { name: target.name, exact: true }).filter({ hasText: labels[0] }).click();
    await expect(canvas).toHaveAccessibleName(/2 resources, 1 relationships/);

    // An explicit relation and a body link with the same endpoints are distinct.
    await page.getByRole("complementary", { name: `Inspector for ${target.name}`, exact: true })
      .getByRole("button", { name: start.name, exact: true }).filter({ hasText: labels[0] }).click();
    await inspector.getByRole("button", { name: target.name, exact: true }).filter({ hasText: labels[1] }).click();
    await expect.soft(canvas).toHaveAccessibleName(/2 resources, 2 relationships/);
    // Loading the server versions (including a repeated load) must not add copies.
    for (let attempt = 0; attempt < 2; attempt++) {
      await page.getByRole("button", { name: "Load connections", exact: true }).click();
      await expect(page.getByRole("button", { name: "Load connections", exact: true })).toBeEnabled();
      await expect(canvas).toHaveAccessibleName(/2 resources, 2 relationships/);
    }
    expect(errors).toEqual([]);
  });
}

test("pointer picking, closing and filtering keep 3D relationship details in sync", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const scene = fixtureScene(2, 2);
  scene.edges = scene.edges.slice(0, 1);
  scene.edges_total = 1;
  scene.nodes.forEach(node => { node.degree = 1; });
  await fixture(page, scene);
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 2);
  const before = await stablePixels(page);
  const details = page.getByRole("complementary", { name: "Relationship details", exact: true });
  async function inspectEdge() {
    const labels = await labelPositions(page);
    expect(labels).toHaveLength(2);
    const midpoint = { x: (labels[0].x + labels[1].x) / 2, y: (labels[0].y - labels[0].height / 2 + labels[1].y - labels[1].height / 2) / 2 };
    // Labels sit below differently sized projected meshes. Probe the narrow
    // band above their midpoint with real pointer clicks, never engine events.
    for (const yOffset of [-12, -24, -36, -48, -60, -72, -84, -96, -108, 0]) {
      for (const xOffset of [0, -18, 18]) {
        await page.mouse.click(midpoint.x + xOffset, midpoint.y + yOffset);
        await frames(page);
        if (await details.isVisible()) break;
      }
      if (await details.isVisible()) break;
    }
    await expect(details).toBeVisible();
  }
  await inspectEdge();
  await expect(details).toContainText("API Gateway");
  await expect(details).toContainText("Service metrics");
  await expect.poll(async () => before.equals(await canvasPixels(page))).toBe(false);
  await page.getByRole("button", { name: "Close relationship details", exact: true }).click();
  await expect(details).toBeHidden();
  await expect.poll(async () => before.equals(await canvasPixels(page))).toBe(true);
  await inspectEdge();
  await page.getByRole("button", { name: /^Filters/ }).click();
  await page.getByRole("menuitemcheckbox", { name: "table", exact: true }).click();
  await expect(details).toBeHidden();
  await page.keyboard.press("Escape");
  await graphReady(page, 1);
  await expect(page.getByTestId("graph-canvas")).toHaveAccessibleName(/0 relationships/);
});

test("a failed connection load retains the 3D map and can be retried", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const scene = fixtureScene();
  const extraUri = "akb://fixture/doc/retried-connection.md";
  let attempts = 0;
  const { errors } = await fixture(page, scene, { onGraph: async (route, url) => {
    if (url.searchParams.get("limit") !== "100") return false;
    attempts += 1;
    if (attempts === 1) await route.fulfill({ status: 503, json: { detail: "Connections temporarily unavailable" } });
    else {
      const recovered: FixtureNode = { uri: extraUri, name: "Recovered connection", resource_type: "document", degree: 1 };
      // The document endpoint must describe the same resource introduced by
      // expansion, otherwise its authoritative title overrides the scene name.
      scene.nodes.push(recovered);
      await route.fulfill({ json: {
        nodes: [scene.nodes[0], recovered],
        edges: [{ source: scene.nodes[0].uri, target: extraUri, relation: "references", kind: "explicit" }],
      } });
    }
    return true;
  } });
  await page.goto("/vault/fixture/graph");
  await graphReady(page, 30);
  await selectFromSearch(page, "API Gateway");
  const before = await stablePixels(page);
  const labelsBefore = await labelPositions(page);
  await page.getByRole("button", { name: "Load connections", exact: true }).click();
  const inspector = page.getByRole("complementary", { name: "Inspector for API Gateway", exact: true });
  await expect(inspector.getByRole("alert")).toContainText("Connections temporarily unavailable");
  expect(await canvasPixels(page)).toEqual(before);
  expect(await labelPositions(page)).toEqual(labelsBefore);
  await inspector.getByRole("button", { name: "Retry loading connections", exact: true }).click();
  await graphReady(page, 31);
  await expect(inspector.getByRole("alert")).toHaveCount(0);
  await selectFromSearch(page, "Recovered connection");
  expect(attempts).toBe(2);
  expect(errors).toEqual([]);
});

for (const count of [700, 1500]) {
  test("stress fixture " + count + ": real 3D frame and responsive zoom", async ({ page }, testInfo) => {
    test.skip(process.env.AKB_GRAPH_STRESS !== "1", "Opt-in renderer measurement; no production performance claim.");
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 1000 });
    const { errors } = await fixture(page, fixtureScene(count, count === 700 ? 200 : 1000));
    const started = performance.now();
    await page.goto("/vault/fixture/graph");
    await graphReady(page, count);
    const loadAndFirstFrameMs = performance.now() - started;
    const before = await stablePixels(page);
    const inputStarted = performance.now();
    await page.getByRole("button", { name: "Zoom in", exact: true }).click();
    await expect.poll(async () => before.equals(await canvasPixels(page))).toBe(false);
    const zoomResponseWithCaptureMs = performance.now() - inputStarted;
    expect(errors).toEqual([]);
    const measurement = {
      fixtureNodes: count, loadAndFirstFrameMs, zoomResponseWithCaptureMs,
      backend: "intercepted HTTP fixture", renderer: "real Three.js WebGL2, SwiftShader", repetitions: 1,
    };
    console.info("Graph fixture measurement:", JSON.stringify(measurement));
    await testInfo.attach("fixture-measurement", { body: JSON.stringify(measurement), contentType: "application/json" });
  });
}
