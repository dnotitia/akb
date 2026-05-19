import { useState } from "react";
import { X } from "lucide-react";
import { Input } from "@/components/ui/input";

export interface TagInputProps {
  value: string[];
  onChange: (tags: string[]) => void;
  id?: string;
  placeholder?: string;
}

export function TagInput({
  value,
  onChange,
  id,
  placeholder = "Add tag and press Enter or comma",
}: TagInputProps) {
  const [draft, setDraft] = useState("");

  function commit() {
    const v = draft.trim().replace(/^#/, "");
    if (!v) return;
    if (!value.includes(v)) onChange([...value, v]);
    setDraft("");
  }

  function remove(t: string) {
    onChange(value.filter((x) => x !== t));
  }

  return (
    <>
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2">
          {value.map((t) => (
            <span
              key={t}
              className="inline-flex items-center gap-1 px-2 py-0.5 border border-border bg-surface-muted text-xs font-mono"
            >
              #{t}
              <button
                type="button"
                onClick={() => remove(t)}
                aria-label={`Remove tag ${t}`}
                className="text-foreground-muted hover:text-destructive cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <X className="h-3 w-3" aria-hidden />
              </button>
            </span>
          ))}
        </div>
      )}
      <Input
        id={id}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            commit();
          } else if (e.key === "Backspace" && !draft && value.length > 0) {
            onChange(value.slice(0, -1));
          }
        }}
        onBlur={commit}
        placeholder={placeholder}
      />
    </>
  );
}
