import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { MarkdownRender } from "@/components/markdown-render";
import { DocumentFindBar, DocumentReaderToolsActions, RawDocumentText, ReaderImageZoom } from "@/components/document-reader-tools";
import { ReaderToolsProvider } from "@/contexts/reader-tools-context";
import { readReaderPreferences } from "@/lib/reader-preferences";
import { findReadingTextMatches } from "@akb/markdown-editor/react";

const markdown = '# Reader\n\nA literal [value] and another [value]. 한글 검색.\n\n```ts\nconst value = "한글";\nconsole.log(value);\n```';
function Harness({ raw = false }: { raw?: boolean }) {
  return <MemoryRouter><ReaderToolsProvider><DocumentReaderToolsActions /><DocumentFindBar />{raw ? <pre><RawDocumentText text={markdown} searchable /></pre> : <MarkdownRender markdown={markdown} className="document-reading-flow" searchable />}</ReaderToolsProvider></MemoryRouter>;
}

beforeEach(() => localStorage.removeItem("akb:reader-preferences:v1"));

describe("reader tools", () => {
  it("matches literal punctuation and Unicode without expanding regex, with a bounded result set", () => {
    expect(findReadingTextMatches("[a] [a] 한글 한글", "[a]").matches).toEqual([{ from: 0, to: 3 }, { from: 4, to: 7 }]);
    expect(findReadingTextMatches("İ A", "a").matches).toEqual([{ from: 2, to: 3 }]);
    expect(findReadingTextMatches("한글 한글", "한글", 1)).toEqual({ matches: [{ from: 0, to: 2 }], limited: true });
    expect(findReadingTextMatches("anything", "").matches).toEqual([]);
  });

  it("discards invalid or unavailable preference storage", () => {
    localStorage.setItem("akb:reader-preferences:v1", '{"fontSize":999,"width":"overflow","tableFit":"true"}');
    expect(readReaderPreferences()).toEqual({ fontSize: 16, width: "comfortable", tableFit: false, foldCode: false });
    localStorage.setItem("akb:reader-preferences:v1", "broken");
    expect(readReaderPreferences().fontSize).toBe(16);
  });

  it("collapses code without changing source and copies just the code", async () => {
    const user = userEvent.setup(); const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(<Harness />);
    const toolbar = await screen.findByRole("group", { name: "ts code tools" });
    expect(within(toolbar).getByText("2 lines")).toBeInTheDocument();
    await user.click(within(toolbar).getByRole("button", { name: "Copy code block 1" }));
    expect(copy).toHaveBeenCalledWith('const value = "한글";\nconsole.log(value);');
    await user.click(within(toolbar).getByRole("button", { name: "Collapse code block 1" }));
    expect(document.querySelector("pre[data-reading-folded=true]")).toHaveTextContent('const value = "한글";');
    await user.click(screen.getByRole("button", { name: "Expand code block 1" }));
    expect(document.querySelector("pre[data-reading-folded=true]")).toBeNull();
  });

  it("finds rendered text, wraps matches, reveals collapsed code and clears marks on close", async () => {
    const user = userEvent.setup(); render(<Harness />);
    await user.click(await screen.findByRole("button", { name: "Collapse code block 1" }));
    await user.click(screen.getByRole("button", { name: "Find in document" }));
    await user.type(screen.getByRole("searchbox", { name: "Find text in document" }), "한글");
    await waitFor(() => expect(screen.getByRole("search")).toHaveTextContent("1/2"));
    await user.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByRole("search")).toHaveTextContent("2/2");
    expect(document.querySelector("pre[data-reading-folded=true]")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByRole("search")).toHaveTextContent("1/2");
    await user.click(screen.getByRole("button", { name: "Close document find" }));
    expect(document.querySelector(".markdown-reading-match")).toBeNull();
    expect(screen.getByRole("button", { name: "Find in document" })).toHaveFocus();
  });

  it("finds literal raw Markdown and keeps reader settings small and persistent", async () => {
    const user = userEvent.setup(); render(<Harness raw />);
    await user.click(screen.getByRole("button", { name: "Find in document" }));
    await user.paste("[value]");
    expect(screen.getByRole("search")).toHaveTextContent("1/2");
    expect(document.querySelectorAll("mark")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Reading settings" }));
    await user.click(screen.getByRole("button", { name: "Increase text size" }));
    await user.click(screen.getByRole("radio", { name: "Wide" }));
    expect(readReaderPreferences()).toMatchObject({ fontSize: 18, width: "wide" });
    await user.click(screen.getByRole("button", { name: "Reset reading settings" }));
    expect(readReaderPreferences().width).toBe("comfortable");
  });

  it("gives images an explicit enlarge button without changing image semantics", async () => {
    render(<MarkdownRender markdown={'![Diagram](https://example.invalid/a.png)'} />);
    const image = await screen.findByRole("img", { name: "Diagram" });
    Object.defineProperties(image, { complete: { configurable: true, value: true }, naturalWidth: { configurable: true, value: 100 } });
    vi.spyOn(image, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, top: 0, left: 0, right: 100, bottom: 100, width: 100, height: 100, toJSON: () => ({}) });
    fireEvent.load(image);
    expect(await screen.findByRole("button", { name: "Enlarge image: Diagram" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Diagram" })).toBeInTheDocument();
  });

  it("closes the image dialog with Escape", async () => {
    const user = userEvent.setup(); const close = vi.fn();
    render(<ReaderImageZoom image={{ src: "https://example.invalid/a.png", alt: "Diagram" }} onClose={close} />);
    expect(screen.getByRole("dialog", { name: "Diagram" })).toBeInTheDocument();
    await user.keyboard("{Escape}"); expect(close).toHaveBeenCalled();
  });
});
