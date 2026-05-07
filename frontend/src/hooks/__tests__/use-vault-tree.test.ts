import { describe, expect, it } from "vitest";
import { buildTree } from "@/hooks/use-vault-tree";

const doc = (path: string, title = path.split("/").pop()!) => ({
  type: "document" as const,
  name: title,
  path,
});
const coll = (path: string, name = path.split("/").pop()!) => ({
  type: "collection" as const,
  name,
  path,
});

describe("buildTree", () => {
  it("nests collections by path segments", () => {
    const items = [coll("features"), coll("features/overview"), coll("prd"), coll("prd/overview")];
    const tree = buildTree(items);
    expect(tree.map((n) => n.name)).toEqual(["features", "prd"]);
    expect(tree[0].children?.map((c) => c.name)).toEqual(["overview"]);
    expect(tree[1].children?.map((c) => c.name)).toEqual(["overview"]);
  });

  it("attaches documents to their declared collection", () => {
    const items = [coll("architecture"), doc("architecture/schema.md", "Schema")];
    const tree = buildTree(items);
    expect(tree).toHaveLength(1);
    expect(tree[0].children).toHaveLength(1);
    expect(tree[0].children![0].kind).toBe("document");
    expect(tree[0].children![0].name).toBe("Schema");
  });

  it("fabricates ancestors when a document lands in an undeclared collection", () => {
    const items = [doc("orphan/path/leaf.md")];
    const tree = buildTree(items);
    // orphan → path → leaf.md
    expect(tree).toHaveLength(1);
    expect(tree[0].kind).toBe("collection");
    expect(tree[0].name).toBe("orphan");
    expect(tree[0].children![0].name).toBe("path");
    expect(tree[0].children![0].children![0].kind).toBe("document");
  });

  it("places root-level docs without a collection at the top", () => {
    const tree = buildTree([doc("readme.md")]);
    expect(tree).toHaveLength(1);
    expect(tree[0].kind).toBe("document");
  });

  it("sorts collections before documents, then tables, then files — each alphabetical", () => {
    const items = [
      { type: "file", name: "zzz.pdf", file_id: "f1", path: "zzz.pdf" },
      { type: "table", name: "bbb", path: "bbb" },
      doc("readme.md", "readme"),
      coll("alpha"),
    ];
    const tree = buildTree(items as any);
    expect(tree.map((n) => n.kind)).toEqual(["collection", "document", "table", "file"]);
  });

  it("still shows `overview` nested under each parent (the bug that motivated the refactor)", () => {
    const items = [
      coll("features"),
      coll("features/overview"),
      coll("prd"),
      coll("prd/overview"),
      coll("roadmap"),
      coll("roadmap/overview"),
    ];
    const tree = buildTree(items);
    const overviewsAtRoot = tree.filter((n) => n.name === "overview");
    expect(overviewsAtRoot).toHaveLength(0); // not 3, not 14 — it's nested
    expect(tree.map((n) => n.name).sort()).toEqual(["features", "prd", "roadmap"]);
  });
});
