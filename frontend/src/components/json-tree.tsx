import { useState } from "react";
import { ChevronRight, ChevronDown } from "lucide-react";

interface Props {
  data: any;
  name?: string;
  depth?: number;
}

export function JsonTree({ data, name, depth = 0 }: Props) {
  const [open, setOpen] = useState(depth < 2);

  if (data === null) return <span className="text-purple-500">null</span>;
  if (typeof data === "boolean")
    return <span className="text-orange-500">{String(data)}</span>;
  if (typeof data === "number")
    return <span className="text-blue-500">{data}</span>;
  if (typeof data === "string")
    return <span className="text-green-700 dark:text-green-400">"{data}"</span>;

  if (Array.isArray(data)) {
    if (data.length === 0) return <span className="text-muted-foreground">[]</span>;
    return (
      <div>
        <button
          onClick={() => setOpen(!open)}
          className="inline-flex items-center text-muted-foreground hover:text-foreground"
        >
          {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          <span className="ml-1 text-xs">[{data.length}]</span>
        </button>
        {open && (
          <div className="ml-4 border-l border-border pl-3">
            {data.map((v, i) => (
              <div key={i} className="text-sm font-mono">
                <span className="text-muted-foreground mr-2">{i}:</span>
                <JsonTree data={v} depth={depth + 1} />
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  if (typeof data === "object") {
    const keys = Object.keys(data);
    if (keys.length === 0) return <span className="text-muted-foreground">{"{}"}</span>;
    return (
      <div>
        <button
          onClick={() => setOpen(!open)}
          className="inline-flex items-center text-muted-foreground hover:text-foreground"
        >
          {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          <span className="ml-1 text-xs">{`{${keys.length}}`}</span>
        </button>
        {open && (
          <div className="ml-4 border-l border-border pl-3">
            {keys.map((k) => (
              <div key={k} className="text-sm font-mono">
                <span className="text-blue-600 dark:text-blue-400 mr-2">{k}:</span>
                <JsonTree data={data[k]} name={k} depth={depth + 1} />
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  return <span>{String(data)}</span>;
}
