import { describe, expect, it } from "vitest";
import {
  activePathFromRoute,
  filterTree,
  filterTreeByKind,
  findDoc,
  flattenVisible,
  kindGroupKey,
  leafHref,
} from "@/lib/tree-route";
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

describe("filterTreeByKind", () => {
  it("keeps collection ancestry while removing unrelated resource kinds", () => {
    const mixed: TreeNode[] = [
      {
        kind: "collection",
        name: "mixed",
        path: "mixed",
        children: [
          { kind: "document", name: "Doc", path: "mixed/doc.md" },
          { kind: "table", name: "Data", path: "data" },
        ],
      },
    ];
    const tables = filterTreeByKind(mixed, "table");
    expect(tables).toHaveLength(1);
    expect(tables[0].children?.map((node) => node.kind)).toEqual(["table"]);
  });
});

describe("flattenVisible — resource-kind groups", () => {
  it("promotes mixed resource kinds into independent disclosure rows", () => {
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
    expect(rows.map((row) =>
      row.type === "node"
        ? [row.type, row.node.name]
        : [row.type, row.kind],
    )).toEqual([
      ["node", "mixed"],
      ["kind-group", "document"],
      ["node", "a"],
      ["node", "b"],
      ["kind-group", "table"],
      ["node", "t1"],
      ["kind-group", "file"],
      ["node", "f1"],
      ["node", "docsonly"],
      ["node", "c"],
      ["node", "d"],
    ]);
  });

  it("groups loose mixed resource kinds at the vault root too", () => {
    const tree: TreeNode[] = [
      { kind: "collection", name: "col", path: "col", children: [] },
      { kind: "document", name: "rdoc", path: "rdoc.md" },
      { kind: "table", name: "rtab", path: "rtab" },
    ];
    const rows = flattenVisible(tree, new Set(), false);
    expect(rows.map((row) =>
      row.type === "node"
        ? [row.type, row.node.name]
        : [row.type, row.kind],
    )).toEqual([
      ["node", "col"],
      ["kind-group", "document"],
      ["node", "rdoc"],
      ["kind-group", "table"],
      ["node", "rtab"],
    ]);
  });

  it("limits a large kind preview and exposes an incremental more row", () => {
    const documents: TreeNode[] = Array.from({ length: 25 }, (_, index) => ({
      kind: "document",
      name: `Document ${index + 1}`,
      path: `large/${index + 1}.md`,
    }));
    const tree: TreeNode[] = [
      {
        kind: "collection",
        name: "large",
        path: "large",
        children: documents,
      },
    ];
    const rows = flattenVisible(tree, new Set(["large"]), false);
    expect(rows.filter((row) => row.type === "node")).toHaveLength(21);
    expect(rows.find((row) => row.type === "kind-group")).toMatchObject({
      kind: "document",
      count: 25,
      isOpen: true,
    });
    expect(rows.find((row) => row.type === "more")).toMatchObject({
      visibleCount: 20,
      totalCount: 25,
    });
  });

  it("keeps a collapsed kind visible while removing its leaves", () => {
    const tree: TreeNode[] = [
      {
        kind: "collection",
        name: "mixed",
        path: "mixed",
        children: [
          { kind: "document", name: "Doc", path: "mixed/doc.md" },
          { kind: "table", name: "Table", path: "table" },
        ],
      },
    ];
    const rows = flattenVisible(tree, new Set(["mixed"]), false, {
      collapsedKindGroups: new Set([kindGroupKey("mixed", "document")]),
    });
    const documentGroup = rows.find(
      (row) => row.type === "kind-group" && row.kind === "document",
    );
    expect(documentGroup).toMatchObject({ isOpen: false });
    expect(
      rows.some((row) => row.type === "node" && row.node.name === "Doc"),
    ).toBe(false);
    expect(
      rows.some((row) => row.type === "node" && row.node.name === "Table"),
    ).toBe(true);
  });

  it("pins the current route into a limited preview without rendering the gap", () => {
    const documents: TreeNode[] = Array.from({ length: 80 }, (_, index) => ({
      kind: "document",
      name: `Document ${index + 1}`,
      path: `large/${index + 1}.md`,
    }));
    const rows = flattenVisible(
      [
        {
          kind: "collection",
          name: "large",
          path: "large",
          children: documents,
        },
      ],
      new Set(["large"]),
      false,
      { activeSig: "document:large/80.md" },
    );
    expect(
      rows.some(
        (row) => row.type === "node" && row.node.path === "large/80.md",
      ),
    ).toBe(true);
    expect(
      rows.some(
        (row) => row.type === "node" && row.node.path === "large/21.md",
      ),
    ).toBe(false);
  });
});
