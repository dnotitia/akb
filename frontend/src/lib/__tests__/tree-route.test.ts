import { describe, expect, it } from "vitest";
import { activePathFromRoute, filterTree, findDoc, flattenVisible, leafHref } from "@/lib/tree-route";
import type { TreeNode } from "@/hooks/use-vault-tree";

const t: TreeNode[] = [
  {
    kind: "collection",
    name: "architecture",
    path: "architecture",
    children: [
      { kind: "document", name: "Schema", path: "architecture/schema.md", raw: { uri: "akb://v/doc/architecture/schema.md" } },
      { kind: "document", name: "System", path: "architecture/system.md", raw: {} },
    ],
  },
  { kind: "table", name: "audit_log", path: "audit_log" },
];

describe("findDoc", () => {
  it("matches by path", () => {
    expect(findDoc(t, "architecture/schema.md")?.name).toBe("Schema");
  });
  it("matches by raw.uri", () => {
    expect(findDoc(t, "akb://v/doc/architecture/schema.md")?.name).toBe("Schema");
  });
  it("returns null on miss", () => {
    expect(findDoc(t, "nope")).toBeNull();
  });
  it("does NOT match partial path (dead fallback removed)", () => {
    expect(findDoc(t, "schema.md")).toBeNull();
  });
});

describe("activePathFromRoute", () => {
  it("resolves doc by canonical path", () => {
    expect(activePathFromRoute("/vault/v/doc/architecture%2Fschema.md", t)).toBe(
      "document:architecture/schema.md",
    );
  });
  it("resolves table", () => {
    expect(activePathFromRoute("/vault/v/table/audit_log", t)).toBe("table:audit_log");
  });
  it("returns null on landing route", () => {
    expect(activePathFromRoute("/vault/v", t)).toBeNull();
  });
});

describe("leafHref", () => {
  it("encodes document path", () => {
    const node = t[0].children![0];
    expect(leafHref("myvault", node)).toBe("/vault/myvault/doc/architecture%2Fschema.md");
  });
});

describe("filterTree", () => {
  it("keeps collections whose descendants match", () => {
    const out = filterTree(t, "schema");
    expect(out).toHaveLength(1);
    expect(out[0].children?.map((c) => c.name)).toEqual(["Schema"]);
  });
  it("keeps leaves matched directly", () => {
    const out = filterTree(t, "audit");
    expect(out.map((n) => n.name)).toEqual(["audit_log"]);
  });
  it("drops non-matching branches", () => {
    const out = filterTree(t, "xxx");
    expect(out).toEqual([]);
  });
});

describe("flattenVisible — kind-group headers", () => {
  it("labels each leaf-kind group only when a parent mixes ≥2 kinds", () => {
    const tree: TreeNode[] = [
      {
        kind: "collection", name: "mixed", path: "mixed", children: [
          { kind: "document", name: "a", path: "mixed/a.md" },
          { kind: "document", name: "b", path: "mixed/b.md" },
          { kind: "table", name: "t1", path: "t1" },
          { kind: "file", name: "f1", path: "f1" },
        ],
      },
      {
        kind: "collection", name: "docsonly", path: "docsonly", children: [
          { kind: "document", name: "c", path: "docsonly/c.md" },
          { kind: "document", name: "d", path: "docsonly/d.md" },
        ],
      },
    ];
    const rows = flattenVisible(tree, new Set(["mixed", "docsonly"]), false);
    expect(rows.map((r) => [r.node.name, r.kindHeader])).toEqual([
      ["mixed", undefined],
      ["a", "document"], // first doc of a mixed parent → header
      ["b", undefined],
      ["t1", "table"], // kind transition → header
      ["f1", "file"],
      ["docsonly", undefined],
      ["c", undefined], // single-kind collection → no headers
      ["d", undefined],
    ]);
  });

  it("groups loose leaf kinds at the vault root too; collections never get a header", () => {
    const tree: TreeNode[] = [
      { kind: "collection", name: "col", path: "col", children: [] },
      { kind: "document", name: "rdoc", path: "rdoc.md" },
      { kind: "table", name: "rtab", path: "rtab" },
    ];
    const rows = flattenVisible(tree, new Set(), false);
    expect(rows.map((r) => [r.node.name, r.kindHeader])).toEqual([
      ["col", undefined],
      ["rdoc", "document"],
      ["rtab", "table"],
    ]);
  });
});
