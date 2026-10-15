import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TooltipText } from "@/components/ui/tooltip-text";

const originals = ["scrollWidth", "clientWidth", "scrollHeight", "clientHeight"].map(
  (name) => [name, Object.getOwnPropertyDescriptor(HTMLElement.prototype, name)] as const,
);

/** jsdom has no layout: report the element whose text is `text` with these sizes. */
function stubMeasuredText(text: string, sizes: Record<string, number>) {
  for (const [name, size] of Object.entries(sizes)) {
    Object.defineProperty(HTMLElement.prototype, name, {
      configurable: true,
      get(this: HTMLElement) {
        return this.textContent === text ? size : 0;
      },
    });
  }
}

/** Wider than its box, as a truncated single line. */
function stubOverflowingText(text: string) {
  stubMeasuredText(text, { scrollWidth: 320, clientWidth: 120 });
}

/** Taller than its box, as text clamped to a number of lines. */
function stubClampedText(text: string) {
  stubMeasuredText(text, { scrollHeight: 120, clientHeight: 40 });
}

afterEach(() => {
  for (const [name, descriptor] of originals) {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
    else Reflect.deleteProperty(HTMLElement.prototype, name);
  }
});

describe("TooltipText overflow reporting", () => {
  it("reports when the measured text overflows its box", async () => {
    stubOverflowingText("A label wider than its column");
    const onOverflowChange = vi.fn();
    render(
      <TooltipText className="truncate" onOverflowChange={onOverflowChange}>
        A label wider than its column
      </TooltipText>,
    );
    await waitFor(() => expect(onOverflowChange).toHaveBeenLastCalledWith(true));
  });

  it.each([
    [false, "beside its trigger"],
    [true, "on document.body"],
  ])("with portal=%s renders the open tooltip %s", async (portal, _where) => {
    stubOverflowingText("A label wider than its column");
    const user = userEvent.setup();
    render(
      <div data-testid="host">
        <TooltipText className="truncate" portal={portal}>
          A label wider than its column
        </TooltipText>
      </div>,
    );
    await user.hover(screen.getByText("A label wider than its column"));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("A label wider than its column");
    if (portal) expect(screen.getByTestId("host")).not.toContainElement(tooltip);
    else expect(screen.getByTestId("host")).toContainElement(tooltip);
  });

  it("reports only measured values, also from a newly mounted instance", () => {
    stubOverflowingText("A label wider than its column");
    const onOverflowChange = vi.fn();
    const label = (
      <TooltipText className="truncate" onOverflowChange={onOverflowChange}>
        A label wider than its column
      </TooltipText>
    );
    const first = render(label);
    expect(onOverflowChange.mock.calls).toEqual([[true]]);

    // A caller that swaps the element around the text remounts it. Reporting
    // the initial, unmeasured false first would make that caller swap back.
    first.unmount();
    render(label);
    expect(onOverflowChange.mock.calls).toEqual([[true], [true]]);
  });

  it("reports false for text that fits and keeps the callback off the DOM", () => {
    const onOverflowChange = vi.fn();
    render(
      <TooltipText className="truncate" onOverflowChange={onOverflowChange}>
        Fits
      </TooltipText>,
    );
    expect(onOverflowChange).toHaveBeenLastCalledWith(false);
    expect(onOverflowChange).not.toHaveBeenCalledWith(true);
    expect(screen.getByText("Fits").getAttributeNames().sort()).toEqual(["class", "data-state"]);
  });
});

describe("TooltipText overflow axis", () => {
  const clamped = "A note clamped to six lines";

  it("ignores vertical overflow by default", () => {
    stubClampedText(clamped);
    const onOverflowChange = vi.fn();
    render(
      <TooltipText className="line-clamp-6" onOverflowChange={onOverflowChange}>
        {clamped}
      </TooltipText>,
    );
    expect(onOverflowChange).toHaveBeenLastCalledWith(false);
    expect(onOverflowChange).not.toHaveBeenCalledWith(true);
  });

  it('reports vertical overflow with overflowAxis="both" and shows the tooltip', async () => {
    stubClampedText(clamped);
    const user = userEvent.setup();
    const onOverflowChange = vi.fn();
    render(
      <TooltipText overflowAxis="both" className="line-clamp-6" onOverflowChange={onOverflowChange}>
        {clamped}
      </TooltipText>,
    );
    await waitFor(() => expect(onOverflowChange).toHaveBeenLastCalledWith(true));
    await user.hover(screen.getByText(clamped));
    expect(await screen.findByRole("tooltip")).toHaveTextContent(clamped);
  });

  it('still reports horizontal overflow with overflowAxis="both"', async () => {
    stubOverflowingText(clamped);
    const onOverflowChange = vi.fn();
    render(
      <TooltipText overflowAxis="both" onOverflowChange={onOverflowChange}>
        {clamped}
      </TooltipText>,
    );
    await waitFor(() => expect(onOverflowChange).toHaveBeenLastCalledWith(true));
  });

  it("measures again when the axis changes, even if the size does not", async () => {
    stubClampedText(clamped);
    const onOverflowChange = vi.fn();
    const { rerender } = render(
      <TooltipText onOverflowChange={onOverflowChange}>{clamped}</TooltipText>,
    );
    expect(onOverflowChange).toHaveBeenLastCalledWith(false);

    rerender(
      <TooltipText overflowAxis="both" onOverflowChange={onOverflowChange}>
        {clamped}
      </TooltipText>,
    );
    await waitFor(() => expect(onOverflowChange).toHaveBeenLastCalledWith(true));

    rerender(<TooltipText onOverflowChange={onOverflowChange}>{clamped}</TooltipText>);
    await waitFor(() => expect(onOverflowChange).toHaveBeenLastCalledWith(false));
  });
});
