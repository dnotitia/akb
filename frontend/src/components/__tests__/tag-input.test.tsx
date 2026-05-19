import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TagInput } from "@/components/ui/tag-input";

describe("TagInput", () => {
  it("commits a tag on Enter and clears the draft", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<TagInput value={[]} onChange={onChange} />);
    const input = screen.getByRole("textbox");
    await user.type(input, "release{Enter}");
    expect(onChange).toHaveBeenCalledWith(["release"]);
    expect(input).toHaveValue("");
  });

  it("commits a tag on comma", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<TagInput value={[]} onChange={onChange} />);
    await user.type(screen.getByRole("textbox"), "alpha,");
    expect(onChange).toHaveBeenCalledWith(["alpha"]);
  });

  it("strips a leading # and de-dupes existing tags", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<TagInput value={["alpha"]} onChange={onChange} />);
    await user.type(screen.getByRole("textbox"), "#alpha{Enter}");
    // Already in value — onChange should not be called with a duplicate.
    expect(onChange).not.toHaveBeenCalled();
  });

  it("backspace on an empty draft pops the last tag", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<TagInput value={["alpha", "beta"]} onChange={onChange} />);
    await user.click(screen.getByRole("textbox"));
    await user.keyboard("{Backspace}");
    expect(onChange).toHaveBeenCalledWith(["alpha"]);
  });

  it("caps individual tags at maxTagLength", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<TagInput value={[]} onChange={onChange} maxTagLength={5} />);
    await user.type(screen.getByRole("textbox"), "supercalifragilistic{Enter}");
    expect(onChange).toHaveBeenCalledWith(["super"]);
  });

  it("refuses to add tags once maxTags is reached", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<TagInput value={["a", "b"]} onChange={onChange} maxTags={2} />);
    await user.type(screen.getByRole("textbox"), "c{Enter}");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("renders an X button per existing tag and removes via click", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<TagInput value={["alpha", "beta"]} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: /Remove tag alpha/ }));
    expect(onChange).toHaveBeenCalledWith(["beta"]);
  });
});
