import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import GuidePreview from "../guide-preview";

const CONTENT = "---\ntitle: System metadata\n---\n# Writing guide\n\nRead **before editing**.\n\n## Checklist\n\n- [ ] Keep document IDs\n- [x] Use canonical paths\n\n## Examples\n\n```md\nakb://my-v/overview/guide.md\n```";

describe("GuidePreview", () => {
  it("shows real Markdown in a read-only preview with isolated section anchors", async () => {
    render(<GuidePreview vault="my-v" content={CONTENT} />);
    const preview = within(screen.getByRole("region", { name: "Vault guide preview" }));
    const heading = await preview.findByRole("heading", { name: "Writing guide" });
    expect(heading.tagName).toBe("H4");
    expect(heading.id).toMatch(/^guide-preview-.+-writing-guide$/);
    expect(preview.getByText("before editing").tagName).toBe("STRONG");
    expect(preview.queryByText("System metadata")).not.toBeInTheDocument();
    const tasks = preview.getAllByRole("checkbox");
    expect(tasks).toHaveLength(2);
    expect(tasks[0]).toBeDisabled();
    expect(tasks[1]).toBeChecked();
    expect(tasks[1]).toBeDisabled();
  });

  it("expands in place and keeps the same labelled toggle and document region", async () => {
    const user = userEvent.setup();
    render(<GuidePreview vault="my-v" content={CONTENT} />);
    const region = screen.getByRole("region", { name: "Vault guide preview" });
    const toggle = screen.getByRole("button", { name: "Show more" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", region.id);
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Show less" })).toBe(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveFocus();
    expect(screen.getByRole("region", { name: "Vault guide preview" })).toBe(region);
  });

  it("expands and focuses an internal heading without jumping to a Settings section", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    render(<><h3 id="access">Settings access</h3><GuidePreview vault="my-v"
      content={"# Guide\n\nRead this.\n\n[Jump to access](#access)\n\nAnother paragraph.\n\n## Access\n\nGuide-specific instructions."}
      onNavigate={onNavigate} /></>);
    const link = await screen.findByRole("link", { name: "Jump to access" });
    expect(link).toHaveAttribute("href", "/vault/my-v/doc/overview%2Fvault-skill.md#access");
    await user.click(link);
    await waitFor(() => expect(screen.getByRole("heading", { name: /^Access$/ })).toHaveFocus());
    expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("heading", { name: "Settings access" })).not.toHaveFocus();
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("preserves historical revision queries instead of following the current inline heading", async () => {
    const onNavigate = vi.fn((event) => event.preventDefault());
    render(<GuidePreview vault="my-v"
      content={"[Earlier guide](/vault/my-v/doc/overview%2Fvault-skill.md?commit=old#access)\n\n## Access\n\nCurrent instructions."}
      onNavigate={onNavigate} />);
    await userEvent.click(await screen.findByRole("link", { name: "Earlier guide" }));
    expect(onNavigate).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ search: "?commit=old", hash: "#access" }));
    expect(screen.getByRole("heading", { name: "Access" })).not.toHaveFocus();
  });

  it("hands other resource links to the containing Settings navigation guard", async () => {
    const onNavigate = vi.fn((event) => event.preventDefault());
    render(<GuidePreview vault="my-v" content="[Members](/vault/my-v/members)" onNavigate={onNavigate} />);
    await userEvent.click(await screen.findByRole("link", { name: "Members" }));
    expect(onNavigate).toHaveBeenCalledWith(expect.anything(), expect.objectContaining({ pathname: "/vault/my-v/members" }));
  });

  it("does not offer expansion for a short guide ending with a code block", async () => {
    render(<GuidePreview vault="my-v" content={'# Guide\n\nKeep IDs.\n\n```md\nakb://my-v\n```'} />);
    expect(await screen.findByRole("heading", { name: "Guide" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });
});
