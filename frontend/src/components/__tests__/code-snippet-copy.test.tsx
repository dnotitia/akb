import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { CodeSnippet } from "@/components/ui/code-snippet";

const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
afterEach(() => {
  if (descriptor) Object.defineProperty(navigator, "clipboard", descriptor);
  else Reflect.deleteProperty(navigator, "clipboard");
});

describe("snippet copy feedback", () => {
  it.each([undefined, { writeText: vi.fn().mockRejectedValue(new Error("Blocked")) }])("does not report success when copying is unavailable", async clipboard => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: clipboard });
    render(<CodeSnippet code="example configuration" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy snippet" }));
    expect(await screen.findByRole("status")).toHaveTextContent("copy it manually");
    expect(screen.queryByRole("button", { name: "Snippet copied" })).not.toBeInTheDocument();
  });

  it("reports success only after writing the exact value", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<CodeSnippet code="example configuration" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy snippet" }));
    expect(await screen.findByRole("button", { name: "Snippet copied" })).toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith("example configuration");
  });
});
