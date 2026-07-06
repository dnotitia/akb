import { useState } from "react";
import type { Meta, StoryObj } from "@storybook/react-vite";
import { expect, userEvent, within } from "storybook/test";
import { Database, FileText, Search, ShieldAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { Segmented } from "@/components/ui/segmented";
import { SelectMenu, type SelectOption } from "@/components/ui/select-menu";
import { TagInput } from "@/components/ui/tag-input";
import { Textarea } from "@/components/ui/textarea";

const meta = {
  title: "UI/Forms and controls",
  parameters: {
    layout: "padded",
  },
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

const vaultOptions: SelectOption[] = [
  { value: "akb", label: "akb", hint: "Production knowledge base" },
  { value: "research", label: "research", hint: "Papers, eval notes, findings" },
  { value: "ops", label: "ops", hint: "Runbooks and incident logs" },
  { value: "archive", label: "archive", hint: "Read-only vault", disabled: true },
];

const manyOptions: SelectOption[] = Array.from({ length: 14 }, (_, index) => ({
  value: `collection-${index + 1}`,
  label: `engineering/collection-${index + 1}`,
  hint: index % 3 === 0 ? "Frequently used" : "Nested collection path",
}));

function SelectHarness({ options = vaultOptions, searchable = false }: { options?: SelectOption[]; searchable?: boolean }) {
  const [value, setValue] = useState(options[0]?.value || "");
  return (
    <SelectMenu
      value={value}
      onValueChange={setValue}
      options={options}
      searchable={searchable}
      placeholder="Choose vault"
      searchPlaceholder="Filter collections..."
      aria-label="Vault"
    />
  );
}

function SegmentedHarness({ disabled = false }: { disabled?: boolean }) {
  const [value, setValue] = useState("dense");
  return (
    <Segmented
      value={value}
      onChange={setValue}
      disabled={disabled}
      className="grid-cols-3"
      aria-label="Search mode"
      options={[
        { value: "dense", label: "Semantic", icon: <Search className="h-3.5 w-3.5" aria-hidden /> },
        { value: "literal", label: "Literal", icon: <FileText className="h-3.5 w-3.5" aria-hidden /> },
        { value: "danger", label: "Reset", danger: true, icon: <ShieldAlert className="h-3.5 w-3.5" aria-hidden /> },
      ]}
    />
  );
}

function TagHarness({ limit = false }: { limit?: boolean }) {
  const [tags, setTags] = useState(limit ? ["design", "search"] : ["akb", "vault"]);
  return (
    <TagInput
      value={tags}
      onChange={setTags}
      maxTags={limit ? 2 : 8}
      maxTagLength={16}
      placeholder={limit ? "Limit reached" : "Add tag"}
    />
  );
}

export const TextFields: Story = {
  render: () => (
    <main className="mx-auto max-w-4xl p-6">
      <Panel>
        <PanelHeader label="Text fields" />
        <div className="grid gap-5 p-5 md:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="vault-name">Vault name</Label>
            <Input id="vault-name" defaultValue="akb-platform" />
          </div>
          <div className="space-y-2">
            <Label htmlFor="path">Collection path</Label>
            <Input id="path" placeholder="engineering/specs" />
          </div>
          <div className="space-y-2">
            <Label htmlFor="invalid">Invalid value</Label>
            <Input id="invalid" aria-invalid defaultValue="Uppercase Path" />
            <p className="text-xs text-destructive">Use lowercase paths only.</p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="disabled">Disabled token</Label>
            <Input id="disabled" disabled value="pat_••••••••••••" readOnly />
          </div>
          <div className="space-y-2 md:col-span-2">
            <Label htmlFor="summary">Summary</Label>
            <Textarea id="summary" defaultValue="A calm, token-governed textarea for document metadata." />
          </div>
          <div className="space-y-2 md:col-span-2">
            <Label htmlFor="readonly">Read-only body</Label>
            <Textarea id="readonly" readOnly value="Rendered historical versions keep contrast while signaling that editing is unavailable." />
          </div>
        </div>
      </Panel>
    </main>
  ),
};

export const SelectMenus: Story = {
  render: () => (
    <main className="mx-auto grid max-w-4xl gap-5 p-6 md:grid-cols-2">
      <Panel className="p-5">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium">
          <Database className="h-4 w-4 text-primary" aria-hidden />
          Vault menu
        </div>
        <SelectHarness />
      </Panel>
      <Panel className="p-5">
        <div className="mb-2 text-sm font-medium">Searchable collection menu</div>
        <SelectHarness options={manyOptions} searchable />
      </Panel>
    </main>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(canvas.getAllByLabelText("Vault")[0]);
    await expect(await within(document.body).findByText("research")).toBeVisible();
    await userEvent.keyboard("{Escape}");
  },
};

export const SegmentedControl: Story = {
  render: () => (
    <main className="mx-auto grid max-w-4xl gap-5 p-6 md:grid-cols-2">
      <Panel className="p-5">
        <div className="mb-3 text-sm font-medium">Active selection</div>
        <SegmentedHarness />
      </Panel>
      <Panel className="p-5">
        <div className="mb-3 text-sm font-medium">Disabled selection</div>
        <SegmentedHarness disabled />
      </Panel>
    </main>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.click(canvas.getAllByRole("radio", { name: "Literal" })[0]);
    await expect(canvas.getAllByRole("radio", { name: "Literal" })[0]).toHaveAttribute("aria-checked", "true");
  },
};

export const TagInputStates: Story = {
  render: () => (
    <main className="mx-auto grid max-w-4xl gap-5 p-6 md:grid-cols-2">
      <Panel className="p-5">
        <div className="mb-3 text-sm font-medium">Editable tags</div>
        <TagHarness />
      </Panel>
      <Panel className="p-5">
        <div className="mb-3 text-sm font-medium">Limit reached</div>
        <TagHarness limit />
      </Panel>
    </main>
  ),
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const input = canvas.getAllByPlaceholderText("Add tag")[0];
    await userEvent.type(input, "agent{Enter}");
    await expect(canvas.getByText("#agent")).toBeVisible();
  },
};

export const FormFooter: Story = {
  render: () => (
    <form className="mx-auto max-w-2xl rounded-[var(--radius-lg)] border border-border bg-surface p-6 shadow-sm">
      <div className="space-y-2">
        <Label htmlFor="doc-title">Document title</Label>
        <Input id="doc-title" defaultValue="Storybook rollout plan" />
      </div>
      <div className="mt-4 space-y-2">
        <Label htmlFor="doc-body">Body</Label>
        <Textarea id="doc-body" defaultValue="Stories capture every meaningful UI state before the backend is needed." />
      </div>
      <div className="mt-6 flex justify-end gap-2">
        <Button type="button" variant="outline">Cancel</Button>
        <Button type="submit" variant="accent">Create document</Button>
      </div>
    </form>
  ),
};
