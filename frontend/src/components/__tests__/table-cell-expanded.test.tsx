import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TableCellExpanded } from "@/components/table-cell-expanded";
import { formatCellValue } from "@/lib/table-cell-value";

describe("formatCellValue", () => {
  it.each([
    [{ service: "gateway", tags: ["api"] }, '{\n  "service": "gateway",\n  "tags": [\n    "api"\n  ]\n}', "json"],
    [[1, 2], "[\n  1,\n  2\n]", "json"],
    ["first line\nsecond line", "first line\nsecond line", "text"],
    ["", "", "text"],
    [42.5, "42.5", "number"],
    [Number.NaN, "—", "number"],
    [Number.POSITIVE_INFINITY, "—", "number"],
    [false, "false", "boolean"],
    [null, "null", "null"],
    [undefined, "null", "null"],
  ])("renders %j as its full text", (value, text, kind) => {
    expect(formatCellValue(value)).toEqual({ text, kind });
  });
});

function renderExpanded(value: unknown) {
  const onCollapse = vi.fn();
  render(<TableCellExpanded value={value} columnName="payload" rowNumber={12} onCollapse={onCollapse} />);
  return { onCollapse, region: screen.getByRole("region", { name: "payload, row 12" }) };
}

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");

afterEach(() => {
  if (clipboardDescriptor) Object.defineProperty(navigator, "clipboard", clipboardDescriptor);
  else Reflect.deleteProperty(navigator, "clipboard");
});

describe("TableCellExpanded", () => {
  it("names its value region after the column and row and keeps it keyboard-scrollable", () => {
    const { region } = renderExpanded("Ready");
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toHaveClass("overflow-auto", "max-h-[50vh]", "select-text");
    expect(region).toHaveTextContent("Ready");
  });

  it("shows an object as two-space indented JSON in a monospace block", () => {
    const value = { service: "gateway", tags: ["api"] };
    const { region } = renderExpanded(value);
    const pre = region.querySelector("pre");
    expect(pre?.textContent).toBe('{\n  "service": "gateway",\n  "tags": [\n    "api"\n  ]\n}');
    expect(pre).toHaveClass("font-mono", "whitespace-pre-wrap");
  });

  it("keeps a string's line breaks and wraps long words inside the cell", () => {
    const { region } = renderExpanded("first line\nsecond line");
    const text = region.firstElementChild;
    expect(text?.textContent).toBe("first line\nsecond line");
    expect(text).toHaveClass("whitespace-pre-wrap", "[overflow-wrap:anywhere]");
  });

  it.each([
    [null, "null"],
    ["", "Empty string"],
  ])("labels %j instead of showing an empty box", (value, label) => {
    const { region } = renderExpanded(value);
    expect(region).toHaveTextContent(label);
    expect(region.firstElementChild).toHaveClass("italic", "text-foreground-muted");
  });

  it("shows a number with tabular figures", () => {
    const { region } = renderExpanded(42.5);
    expect(region.firstElementChild).toHaveTextContent("42.5");
    expect(region.firstElementChild).toHaveClass("tabular-nums");
  });

  it.each([
    [{ ok: true }, '{\n  "ok": true\n}'],
    ["Line one\nLine two", "Line one\nLine two"],
    ["", ""],
  ])("copies %j as exactly the text it shows", async (value, copied) => {
    const user = userEvent.setup();
    // After setup(): user-event installs its own clipboard stub there.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const { onCollapse } = renderExpanded(value);

    await user.click(screen.getByRole("button", { name: "Copy value" }));

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(copied);
    expect(await screen.findByRole("button", { name: "Copy value: copied" })).toBeInTheDocument();
    expect(onCollapse).not.toHaveBeenCalled();
  });

  it("describes Copy value and Collapse by the value region they act on", () => {
    render(
      <>
        <TableCellExpanded value="Ready" columnName="payload" rowNumber={12} onCollapse={vi.fn()} />
        <TableCellExpanded value="Done" columnName="payload" rowNumber={13} onCollapse={vi.fn()} />
      </>,
    );
    // Open cells repeat the same two buttons. Each keeps its short visible
    // name and points its description at its own value region, whose name
    // ("payload, row 12") browsers then read as the description. jsdom's
    // accessible-description helper reads the region's text instead, so the
    // reference itself is what this asserts.
    const regions = [12, 13].map((row) => screen.getByRole("region", { name: `payload, row ${row}` }));
    expect(regions[0].id).not.toBe("");
    expect(regions[0].id).not.toBe(regions[1].id);
    for (const name of ["Copy value", "Collapse"]) {
      const buttons = screen.getAllByRole("button", { name });
      expect(buttons).toHaveLength(2);
      buttons.forEach((button, index) => {
        expect(button).toHaveAccessibleName(name);
        expect(button).toHaveAttribute("aria-describedby", regions[index].id);
      });
    }
  });

  it("collapses from its Collapse button, which says the cell is expanded", async () => {
    const user = userEvent.setup();
    const { onCollapse } = renderExpanded("Ready");
    const collapse = screen.getByRole("button", { name: "Collapse" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");

    await user.click(collapse);
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });

  it("collapses on Escape from anywhere inside, without passing the key on", async () => {
    const user = userEvent.setup();
    const pageEscape = vi.fn();
    window.addEventListener("keydown", pageEscape);
    try {
      const { onCollapse, region } = renderExpanded("Ready");
      region.focus();
      await user.keyboard("{Escape}");
      expect(onCollapse).toHaveBeenCalledTimes(1);

      screen.getByRole("button", { name: "Copy value" }).focus();
      await user.keyboard("{Escape}");
      expect(onCollapse).toHaveBeenCalledTimes(2);
      expect(pageEscape).not.toHaveBeenCalled();

      // Other keys are left alone.
      region.focus();
      await user.keyboard("{Enter}");
      expect(onCollapse).toHaveBeenCalledTimes(2);
    } finally {
      window.removeEventListener("keydown", pageEscape);
    }
  });

  it("stays open when its value is clicked, so the text can be selected", async () => {
    const user = userEvent.setup();
    const { onCollapse, region } = renderExpanded("Select me");
    await user.click(region);
    await user.dblClick(region.firstElementChild!);
    expect(onCollapse).not.toHaveBeenCalled();
  });
});
