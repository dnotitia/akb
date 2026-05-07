import { describe, expect, it } from "vitest";
import { parseHeadings, slugify } from "@/lib/markdown";

describe("slugify", () => {
  it("lowercases and hyphenates", () => {
    expect(slugify("Hello World")).toBe("hello-world");
  });
  it("preserves Hangul", () => {
    expect(slugify("기술 스택")).toBe("기술-스택");
  });
  it("strips markdown emphasis", () => {
    expect(slugify("**Bold** _emphasis_")).toBe("bold-emphasis");
  });
  it("collapses runs of dashes/spaces", () => {
    expect(slugify("A  --  B")).toBe("a-b");
  });
  it("strips punctuation", () => {
    expect(slugify("Progressive Disclosure (L1/L2/L3)")).toBe("progressive-disclosure-l1l2l3");
  });
});

describe("parseHeadings — ATX", () => {
  it("extracts level and text", () => {
    const h = parseHeadings("# One\n## Two\n### Three");
    expect(h.map((x) => [x.level, x.text])).toEqual([
      [1, "One"], [2, "Two"], [3, "Three"],
    ]);
  });
  it("ignores headings inside fenced code blocks", () => {
    const h = parseHeadings("# Real\n```\n# Fake\n```\n## Also real");
    expect(h.map((x) => x.text)).toEqual(["Real", "Also real"]);
  });
  it("handles tilde fences", () => {
    const h = parseHeadings("~~~\n# Fake\n~~~\n# Real");
    expect(h.map((x) => x.text)).toEqual(["Real"]);
  });
  it("strips trailing closing hashes", () => {
    const h = parseHeadings("## Title ##");
    expect(h[0].text).toBe("Title");
  });
  it("disambiguates duplicate slugs", () => {
    const h = parseHeadings("## Intro\n## Intro\n## Intro");
    expect(h.map((x) => x.slug)).toEqual(["intro", "intro-2", "intro-3"]);
  });
});

describe("parseHeadings — setext", () => {
  it("recognizes === as H1", () => {
    const h = parseHeadings("Title\n=====\n");
    expect(h).toEqual([{ level: 1, text: "Title", slug: "title" }]);
  });
  it("recognizes --- as H2", () => {
    const h = parseHeadings("Subtitle\n---\n");
    expect(h[0].level).toBe(2);
  });
  it("doesn't treat --- after an empty line as setext", () => {
    const h = parseHeadings("\n---\n");
    expect(h).toEqual([]);
  });
});
