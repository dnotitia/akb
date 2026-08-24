import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { RelationsPanel } from "@/components/relations/relations-panel";
import type { RelationRow } from "@/lib/api";


vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    createRelation: vi.fn(),
    deleteRelation: vi.fn(),
  };
});

const explicit: RelationRow = {
  direction: "outgoing",
  relation: "related_to",
  uri: "akb://alpha/coll/specs/doc/b.md",
  resource_type: "doc",
  kind: "explicit",
  name: "Visible relation",
};

const implicit: RelationRow = {
  direction: "outgoing",
  relation: "links_to",
  uri: "akb://alpha/coll/specs/doc/c.md",
  resource_type: "doc",
  kind: "implicit",
  name: "Managed relation",
};

function renderPanel(relations: RelationRow[], canWrite: boolean) {
  return render(
    <MemoryRouter>
      <RelationsPanel
        vault="alpha"
        sourceUri="akb://alpha/coll/specs/doc/a.md"
        relations={relations}
        relationsError={false}
        canWrite={canWrite}
        graphHref="/vault/alpha/graph"
        onReload={vi.fn()}
      />
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("RelationsPanel authority and source ownership", () => {
  it("does not render mutation controls for a reader", () => {
    renderPanel([explicit], false);

    expect(screen.getByText("Visible relation", { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove relation/ })).not.toBeInTheDocument();
  });

  it("allows a writer to remove only explicit relations", () => {
    renderPanel([explicit, implicit], true);

    expect(screen.getByRole("button", { name: "Add" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Remove relation/ })).toHaveLength(1);
    expect(screen.getByLabelText("Managed by document content")).toBeInTheDocument();
  });

  it("omits a foreign endpoint from labels and relation counts", () => {
    renderPanel(
      [{ ...explicit, uri: "akb://beta/coll/private/doc/hidden.md", name: "Hidden" }],
      true,
    );

    expect(screen.getByText("0 relations")).toBeInTheDocument();
    expect(screen.queryByText("Hidden")).not.toBeInTheDocument();
  });
});
