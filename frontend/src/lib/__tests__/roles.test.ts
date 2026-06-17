import { describe, it, expect } from "vitest";
import { Crown, Eye, HelpCircle, Pencil, ShieldCheck } from "lucide-react";
import { roleIcon, canEdit } from "@/lib/roles";

describe("roleIcon", () => {
  it("maps each known role to its glyph + label", () => {
    expect(roleIcon("owner")).toEqual({ Icon: Crown, label: "owner" });
    expect(roleIcon("admin")).toEqual({ Icon: ShieldCheck, label: "admin" });
    expect(roleIcon("writer")).toEqual({ Icon: Pencil, label: "writer" });
    expect(roleIcon("reader")).toEqual({ Icon: Eye, label: "reader" });
  });

  it("falls back to a neutral glyph for an unknown role (never undefined → no React #130)", () => {
    const r = roleIcon("maintainer");
    expect(r.Icon).toBe(HelpCircle);
    expect(r.label).toBe("maintainer");
    expect(roleIcon("").Icon).toBe(HelpCircle);
  });
});

describe("canEdit", () => {
  it("is true for write-authority roles", () => {
    expect(canEdit("owner")).toBe(true);
    expect(canEdit("admin")).toBe(true);
    expect(canEdit("writer")).toBe(true);
  });

  it("is false for reader, unknown, and undefined", () => {
    expect(canEdit("reader")).toBe(false);
    expect(canEdit("maintainer")).toBe(false);
    expect(canEdit(undefined)).toBe(false);
  });
});
