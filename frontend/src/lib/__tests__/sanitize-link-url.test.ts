import { describe, expect, it } from "vitest";
import { sanitizeLinkUrl } from "@/lib/utils";

// sanitizeLinkUrl is the only thing standing between user-written markdown
// links and the user's clipboard / clicks. The actual XSS vectors we care
// about — `javascript:`, `data:`, `vbscript:`, and protocol-relative
// `//host` redirects — must round-trip to `#`, while every reasonable
// navigation scheme must pass through unchanged. Lock both halves down so
// a well-meaning refactor can't reintroduce the hole.
describe("sanitizeLinkUrl", () => {
  it("strips javascript: links", () => {
    expect(sanitizeLinkUrl("javascript:alert(1)")).toBe("#");
    expect(sanitizeLinkUrl("JavaScript:alert(1)")).toBe("#");
    expect(sanitizeLinkUrl("  javascript:alert(1)  ")).toBe("#");
  });

  it("strips data: links", () => {
    expect(sanitizeLinkUrl("data:text/html,<script>alert(1)</script>")).toBe("#");
  });

  it("strips vbscript: and other unknown schemes", () => {
    expect(sanitizeLinkUrl("vbscript:msgbox(1)")).toBe("#");
    expect(sanitizeLinkUrl("file:///etc/passwd")).toBe("#");
    expect(sanitizeLinkUrl("intent://evil")).toBe("#");
  });

  it("blocks protocol-relative URLs that inherit the page scheme", () => {
    // Browsers expand `//evil.example/x` to `https://evil.example/x` —
    // treat the implicit scheme as untrusted.
    expect(sanitizeLinkUrl("//evil.example/x")).toBe("#");
    expect(sanitizeLinkUrl("  //evil.example/x  ")).toBe("#");
  });

  it("passes through http / https / mailto / tel", () => {
    expect(sanitizeLinkUrl("https://example.com")).toBe("https://example.com");
    expect(sanitizeLinkUrl("http://example.com")).toBe("http://example.com");
    expect(sanitizeLinkUrl("HTTPS://Example.COM")).toBe("HTTPS://Example.COM");
    expect(sanitizeLinkUrl("mailto:a@example.com")).toBe("mailto:a@example.com");
    expect(sanitizeLinkUrl("tel:+1-555-0100")).toBe("tel:+1-555-0100");
  });

  it("passes through same-origin paths, fragments, queries", () => {
    expect(sanitizeLinkUrl("/local/path")).toBe("/local/path");
    expect(sanitizeLinkUrl("#anchor")).toBe("#anchor");
    expect(sanitizeLinkUrl("?q=x")).toBe("?q=x");
  });

  it("returns `#` for empty, whitespace, null, or undefined input", () => {
    expect(sanitizeLinkUrl("")).toBe("#");
    expect(sanitizeLinkUrl("   ")).toBe("#");
    expect(sanitizeLinkUrl(null)).toBe("#");
    expect(sanitizeLinkUrl(undefined)).toBe("#");
  });
});
