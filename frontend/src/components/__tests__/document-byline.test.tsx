import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DocumentByline } from "@/components/document-byline";
import { documentAuthorName } from "@/lib/document-author";

describe("document identity metadata", () => {
  it("keeps author, exact update time and draft state together", () => {
    render(<DocumentByline author="Ada Lovelace" updatedAt="2026-10-27T03:00:00Z" status="draft" />);
    const byline = screen.getByRole("group", { name: "Document metadata" });
    expect(within(byline).getByText("Author")).toBeInTheDocument();
    expect(within(byline).getByText("Ada Lovelace")).toBeInTheDocument();
    expect(within(byline).getByText("Draft")).toBeInTheDocument();
    expect(byline.querySelector("time")).toHaveAttribute("datetime", "2026-10-27T03:00:00.000Z");
  });
  it("distinguishes a pinned version from the current lifecycle state", () => {
    render(<DocumentByline author="Ada" status="draft" historical version="1111111122222222" timestampLabel="Version saved" updatedAt="2026-10-21T03:00:00Z" />);
    expect(screen.getByText("Version 11111111")).toBeInTheDocument();
    expect(screen.queryByText("Draft")).toBeNull();
    expect(screen.getByText(/Version saved:/)).toBeInTheDocument();
  });
  it("keeps archived status and omits invalid timestamps", () => {
    render(<DocumentByline author="Ada" status="archived" updatedAt="invalid" />);
    expect(screen.getByText("Archived")).toBeInTheDocument();
    expect(document.querySelector("time")).toBeNull();
  });
  it("does not display account IDs or unknown placeholder values as author names", () => {
    expect(documentAuthorName({ created_by_name: " unknown author ", created_by: "00000000-0000-0000-0000-000000000001" })).toBeUndefined();
    expect(documentAuthorName({ created_by_name: null, created_by: "reader" })).toBe("reader");
    expect(documentAuthorName({ created_by_name: " Ada ", created_by: "reader" })).toBe("Ada");
  });
});
