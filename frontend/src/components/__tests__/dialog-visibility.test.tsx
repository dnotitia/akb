import { StrictMode, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Dialog, DialogClose, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { isModalOpen, useModalOpen } from "@/lib/modal-visibility";

function Probe() { return <output data-testid="modal-state">{String(useModalOpen())}</output>; }
const content = <DialogContent aria-describedby={undefined}><DialogTitle>Example</DialogTitle><DialogClose>Dismiss</DialogClose></DialogContent>;

afterEach(() => { cleanup(); expect(isModalOpen()).toBe(false); });

describe("Dialog modal ownership", () => {
  it("tracks uncontrolled trigger/close and defaultOpen without leaking under StrictMode", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<StrictMode><Probe /><Dialog defaultOpen><DialogTrigger>Launch</DialogTrigger>{content}</Dialog></StrictMode>);
    expect(screen.getByTestId("modal-state")).toHaveTextContent("true");
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.getByTestId("modal-state")).toHaveTextContent("false");
    await user.click(screen.getByRole("button", { name: "Launch" }));
    expect(screen.getByTestId("modal-state")).toHaveTextContent("true");
    unmount();
    expect(isModalOpen()).toBe(false);
  });

  it("does not release a controlled modal when its parent rejects dismissal", async () => {
    const changed = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<><Probe /><Dialog open onOpenChange={changed}>{content}</Dialog></>);
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(changed).toHaveBeenCalledWith(false);
    expect(isModalOpen()).toBe(true);
    rerender(<><Probe /><Dialog open={false} onOpenChange={changed}>{content}</Dialog></>);
    expect(isModalOpen()).toBe(false);
  });

  it("counts nested modal roots independently and excludes nonmodal dialogs", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [nested, setNested] = useState(true);
      const [parent, setParent] = useState(true);
      return <><Probe /><button onClick={() => setParent(false)}>Close parent</button>
        <Dialog modal={false} open><span>Nonmodal content</span></Dialog>
        <Dialog open={parent}><DialogContent aria-describedby={undefined}><DialogTitle>Parent</DialogTitle>
          <Dialog open={nested} onOpenChange={setNested}><DialogContent aria-describedby={undefined}><DialogTitle>Child</DialogTitle><DialogClose>Close child</DialogClose></DialogContent></Dialog>
        </DialogContent></Dialog>
      </>;
    }
    const { unmount } = render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Close child" }));
    expect(isModalOpen()).toBe(true);
    unmount();
    render(<><Probe /><Dialog modal={false} defaultOpen>{content}</Dialog></>);
    expect(screen.getByTestId("modal-state")).toHaveTextContent("false");
  });
});
