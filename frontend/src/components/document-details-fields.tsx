import type { Ref } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SelectMenu } from "@/components/ui/select-menu";
import { TagInput } from "@/components/ui/tag-input";
import { Textarea } from "@/components/ui/textarea";
import { DOC_STATUSES, DOC_TYPES } from "@/lib/doc-constants";
import type { DocumentDetailsValues } from "@/lib/document-details";

export interface DocumentDetailsFieldsProps {
  value: DocumentDetailsValues;
  onChange: (next: DocumentDetailsValues) => void;
  disabled?: boolean;
  idPrefix: string;
  showStatus?: boolean;
  firstFieldRef?: Ref<HTMLTextAreaElement>;
}

function optionsWithCurrentValue(options: readonly string[], current: string) {
  const values = options.includes(current) ? options : [current, ...options];
  return values.map((value) => ({ value, label: value || "Not set" }));
}

export function DocumentDetailsFields({
  value,
  onChange,
  disabled = false,
  idPrefix,
  showStatus = false,
  firstFieldRef,
}: DocumentDetailsFieldsProps) {
  function change(patch: Partial<DocumentDetailsValues>) {
    if (!disabled) onChange({ ...value, ...patch });
  }

  return (
    <fieldset disabled={disabled} className="min-w-0 space-y-4">
      <legend className="sr-only">Document details</legend>
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-summary`}>Summary</Label>
        <Textarea
          id={`${idPrefix}-summary`}
          ref={firstFieldRef}
          value={value.summary}
          onChange={(event) => change({ summary: event.target.value })}
          rows={4}
          maxLength={500}
          placeholder="What does this document cover?"
          disabled={disabled}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-type`}>Document type</Label>
        <SelectMenu
          id={`${idPrefix}-type`}
          aria-label="Document type"
          value={value.type}
          onValueChange={(type) => change({ type })}
          options={optionsWithCurrentValue(DOC_TYPES, value.type)}
          disabled={disabled}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-domain`}>Domain</Label>
        <Input
          id={`${idPrefix}-domain`}
          value={value.domain}
          onChange={(event) => change({ domain: event.target.value })}
          placeholder="e.g. engineering"
          disabled={disabled}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-tags`}>Tags</Label>
        <TagInput
          id={`${idPrefix}-tags`}
          value={value.tags}
          onChange={(tags) => change({ tags })}
        />
      </div>
      {showStatus && (
        <div className="space-y-1.5">
          <Label htmlFor={`${idPrefix}-status`}>Status</Label>
          <SelectMenu
            id={`${idPrefix}-status`}
            aria-label="Status"
            value={value.status}
            onValueChange={(status) => change({ status })}
            options={optionsWithCurrentValue(DOC_STATUSES, value.status)}
            disabled={disabled}
          />
        </div>
      )}
    </fieldset>
  );
}
