import { useState } from "react";
import { ChevronRight, ChevronDown } from "lucide-react";

interface Props {
  data: any;
  name?: string;
  depth?: number;
}

// Render at most this many children per container before collapsing the rest
// behind a "+ N more" toggle, so a 100k-element array can't mount all at once.
const CHILD_CAP = 100;
// Stop recursing on pathologically deep / cyclic structures.
const MAX_DEPTH = 64;

const TOGGLE_CLS =
  "inline-flex items-center text-foreground-muted hover:text-foreground rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background";

export function JsonTree({ data, name, depth = 0 }: Props) {
  // Open only the top level by default — deep auto-expansion mounts the whole
  // payload up front.
  const [open, setOpen] = useState(depth < 1);
  const [showAll, setShowAll] = useState(false);

  // Type-coloring uses the brand-neutral categorical scale, NOT the semantic
  // success/warning/info tokens (those mean ok/warn/error, not syntax).
  if (data === null) return <span className="text-subtle">null</span>;
  if (typeof data === "boolean")
    return <span className="text-[var(--color-cat-4)]">{String(data)}</span>;
  if (typeof data === "number")
    return <span className="text-[var(--color-cat-1)]">{data}</span>;
  if (typeof data === "string")
    return <span className="text-[var(--color-cat-3)] whitespace-pre-wrap break-words">"{data}"</span>;

  if (depth >= MAX_DEPTH) return <span className="text-subtle">…</span>;

  const isArray = Array.isArray(data);
  if (isArray || typeof data === "object") {
    const keys = isArray ? null : Object.keys(data);
    const len = isArray ? data.length : keys!.length;
    if (len === 0) return <span className="text-foreground-muted">{isArray ? "[]" : "{}"}</span>;

    const shownCount = showAll ? len : Math.min(len, CHILD_CAP);
    const label = isArray ? `[${len}]` : `{${len}}`;

    return (
      <div>
        <button
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          aria-label={`${name ? name + " " : ""}${isArray ? "array" : "object"}, ${len} ${len === 1 ? "item" : "items"}`}
          className={TOGGLE_CLS}
        >
          {open ? <ChevronDown className="h-3 w-3" aria-hidden /> : <ChevronRight className="h-3 w-3" aria-hidden />}
          <span className="ml-1 text-xs">{label}</span>
        </button>
        {open && (
          <div className="ml-4 border-l border-border pl-3">
            {isArray
              ? data.slice(0, shownCount).map((v: any, i: number) => (
                  <div key={i} className="text-sm font-mono">
                    <span className="text-subtle mr-2">{i}:</span>
                    <JsonTree data={v} depth={depth + 1} />
                  </div>
                ))
              : keys!.slice(0, shownCount).map((k) => (
                  <div key={k} className="text-sm font-mono">
                    <span className="text-primary mr-2">{k}:</span>
                    <JsonTree data={data[k]} name={k} depth={depth + 1} />
                  </div>
                ))}
            {len > shownCount && (
              <button
                onClick={() => setShowAll(true)}
                className="coord mt-1 hover:text-primary rounded-[var(--radius-sm)] focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
              >
                + {len - shownCount} more
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  return <span>{String(data)}</span>;
}
