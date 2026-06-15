import { useState } from "react";
import { X } from "lucide-react";
import { Input } from "@/components/ui/input";

export interface TagInputProps {
  value: string[];
  onChange: (tags: string[]) => void;
  id?: string;
  placeholder?: string;
  /** Per-tag character cap. Defaults to 50. */
  maxTagLength?: number;
  /** Maximum number of tags accepted. Defaults to 50. */
  maxTags?: number;
}

export function TagInput({
  value,
  onChange,
  id,
  placeholder = "Add tag and press Enter or comma",
  maxTagLength = 50,
  maxTags = 50,
}: TagInputProps) {
  const [draft, setDraft] = useState("");
  const [live, setLive] = useState("");

  function commit() {
    const v = draft.trim().replace(/^#/, "").slice(0, maxTagLength);
    if (!v) return;
    // Keep the draft text on rejection so the user sees what didn't take.
    if (value.length >= maxTags) {
      setLive(`Tag limit reached (${maxTags})`);
      return;
    }
    if (value.includes(v)) {
      setLive(`"${v}" is already added`);
      return;
    }
    onChange([...value, v]);
    setLive(`Added tag ${v}`);
    setDraft("");
  }

  function remove(t: string) {
    onChange(value.filter((x) => x !== t));
    setLive(`Removed tag ${t}`);
  }

  return (
    <>
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2">
          {value.map((t) => (
            <span
              key={t}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full border border-border bg-surface-muted text-xs"
            >
              #{t}
              <button
                type="button"
                onClick={() => remove(t)}
                aria-label={`Remove tag ${t}`}
                className="text-foreground-muted hover:text-destructive cursor-pointer rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-muted"
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
        onChange={(e) => setDraft(e.target.value.slice(0, maxTagLength))}
        maxLength={maxTagLength}
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
      <span className="sr-only" role="status" aria-live="polite">{live}</span>
    </>
  );
}
