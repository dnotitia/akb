import { describe, expect, it } from "vitest";
import {
  RESERVED_COLLECTION,
  VAULT_SKILL_PATH,
  isReservedCollection,
} from "@/lib/skill";
import { DOC_TYPES } from "@/lib/doc-constants";

describe("reserved namespace", () => {
  it("exposes the canonical constants", () => {
    expect(RESERVED_COLLECTION).toBe("overview");
    expect(VAULT_SKILL_PATH).toBe("overview/vault-skill.md");
  });
  it("matches overview and its subtree only", () => {
    expect(isReservedCollection("overview")).toBe(true);
    expect(isReservedCollection("overview/sub")).toBe(true);
    expect(isReservedCollection("overview-notes")).toBe(false);
    expect(isReservedCollection("")).toBe(false);
    expect(isReservedCollection("notes")).toBe(false);
  });
  it("drops skill from the user-selectable type vocabulary", () => {
    expect(DOC_TYPES).not.toContain("skill");
  });
});
