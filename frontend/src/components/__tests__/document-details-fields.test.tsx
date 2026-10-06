import { createRef, useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { DocumentDetailsFields } from "@/components/document-details-fields";
import type { DocumentDetailsValues } from "@/lib/document-details";

const initial: DocumentDetailsValues = {
  summary: "Original summary",
  type: "note",
  domain: "research",
  tags: ["source"],
  status: "active",
};

function ControlledFields({
  value = initial,
  disabled = false,
}: {
  value?: DocumentDetailsValues;
  disabled?: boolean;
}) {
  const [details, setDetails] = useState(value);
  return (
    <>
      <DocumentDetailsFields
        value={details}
        onChange={setDetails}
        idPrefix="test-details"
        disabled={disabled}
        showStatus
      />
      <output aria-label="Current details">{JSON.stringify(details)}</output>
    </>
  );
}

function currentDetails() {
  return JSON.parse(screen.getByLabelText("Current details").textContent ?? "null");
}

describe("DocumentDetailsFields", () => {
  it("labels every control, caps the summary, and exposes its first field for focus", () => {
    const firstFieldRef = createRef<HTMLTextAreaElement>();
    render(
      <DocumentDetailsFields
        value={initial}
        onChange={() => {}}
        idPrefix="create-details"
        firstFieldRef={firstFieldRef}
      />,
    );

    const summary = screen.getByRole("textbox", { name: "Summary" });
    expect(summary).toHaveValue("Original summary");
    expect(summary).toHaveAttribute("maxLength", "500");
    expect(screen.getByLabelText("Document type")).toHaveTextContent("note");
    expect(screen.getByRole("textbox", { name: "Domain" })).toHaveValue("research");
    expect(screen.getByRole("textbox", { name: "Tags" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Status" })).not.toBeInTheDocument();

    firstFieldRef.current?.focus();
    expect(summary).toHaveFocus();
  });

  it("updates each selected detail while keeping the other values", async () => {
    const user = userEvent.setup();
    render(<ControlledFields />);

    await user.clear(screen.getByLabelText("Summary"));
    await user.type(screen.getByLabelText("Summary"), "A useful summary");
    await user.clear(screen.getByLabelText("Domain"));
    await user.type(screen.getByLabelText("Domain"), "engineering");
    await user.click(screen.getByRole("button", { name: "Document type" }));
    await user.click(screen.getByRole("menuitemradio", { name: "decision" }));
    await user.type(screen.getByLabelText("Tags"), "release{Enter}");
    await user.click(screen.getByRole("button", { name: "Remove tag source" }));
    await user.click(screen.getByRole("button", { name: "Status" }));
    await user.click(screen.getByRole("menuitemradio", { name: "draft" }));

    expect(currentDetails()).toEqual({
      summary: "A useful summary",
      type: "decision",
      domain: "engineering",
      tags: ["release"],
      status: "draft",
    });
  });

  it("displays unknown type and status values and preserves them on unrelated edits", async () => {
    const user = userEvent.setup();
    render(<ControlledFields value={{ ...initial, type: "custom-report", status: "review" }} />);

    expect(screen.getByRole("button", { name: "Document type" })).toHaveTextContent("custom-report");
    expect(screen.getByRole("button", { name: "Status" })).toHaveTextContent("review");
    await user.click(screen.getByRole("button", { name: "Document type" }));
    expect(screen.getByRole("menuitemradio", { name: "custom-report" })).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Status" }));
    expect(screen.getByRole("menuitemradio", { name: "review" })).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{Escape}");
    await user.clear(screen.getByLabelText("Summary"));
    await user.type(screen.getByLabelText("Summary"), "Revised summary");

    expect(currentDetails()).toEqual({
      summary: "Revised summary",
      type: "custom-report",
      domain: "research",
      tags: ["source"],
      status: "review",
    });
  });

  it("disables all controls and tag removal while a save is pending", async () => {
    const user = userEvent.setup();
    render(<ControlledFields disabled />);

    for (const name of ["Summary", "Domain", "Tags"]) {
      expect(screen.getByRole("textbox", { name })).toBeDisabled();
    }
    for (const name of ["Document type", "Status", "Remove tag source"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    await user.type(screen.getByLabelText("Summary"), "Changed");
    await user.type(screen.getByLabelText("Tags"), "new{Enter}");
    await user.click(screen.getByRole("button", { name: "Remove tag source" }));
    expect(currentDetails()).toEqual(initial);
  });
});
