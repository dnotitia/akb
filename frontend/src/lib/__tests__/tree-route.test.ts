import { describe, expect, it } from "vitest";
import { activePathFromRoute, filterTree, findDoc, leafHref } from "@/lib/tree-route";
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
