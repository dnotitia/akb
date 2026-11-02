import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Columns2, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";

export function ReaderPanelWidthMenu({ split, onPreset, onReset }: { split: boolean; onPreset: (value: number) => void; onReset: () => void }) {
  return <DropdownMenu.Root><DropdownMenu.Trigger asChild><Button type="button" variant="ghost" size="icon" className="reading-icon-button" aria-label="Panel width" title="Panel width"><Columns2 size={14} aria-hidden /></Button></DropdownMenu.Trigger>
    <DropdownMenu.Portal><DropdownMenu.Content align="end" sideOffset={6} className="reader-panel-width-menu">
      <DropdownMenu.Label className="reading-caption">{split ? "Side document width" : "Context panel width"}</DropdownMenu.Label>
      {(split ? [{ label: "Main wider", value: .35 }, { label: "Equal columns", value: .5 }, { label: "Side wider", value: .65 }] : [{ label: "Compact", value: 288 }, { label: "Standard", value: 352 }, { label: "Wide", value: 416 }]).map(option => <DropdownMenu.Item key={option.label} className="document-reader-menu-item" onSelect={() => onPreset(option.value)}>{option.label}</DropdownMenu.Item>)}
      <DropdownMenu.Separator className="my-1 h-px bg-border" /><DropdownMenu.Item className="document-reader-menu-item" onSelect={onReset}><RotateCcw size={13} aria-hidden />Reset width</DropdownMenu.Item>
    </DropdownMenu.Content></DropdownMenu.Portal>
  </DropdownMenu.Root>;
}
