import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { createCollection } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface CreateCollectionDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vault: string;
  onCreated: (path: string) => void;
}

/** Normalize + validate a collection path client-side so we can short-circuit
 *  obvious garbage before round-tripping the server. Server enforces the same
 *  rules — this is purely UX. Returns `{path}` on success, `{error}` on
 *  failure. */
function normalize(raw: string): { path: string } | { error: string } {
  const trimmed = raw.trim().replace(/^\/+|\/+$/g, "");
  if (!trimmed) return { error: "Path is empty." };
  if (trimmed.length > 1024) return { error: "Path is too long (max 1024 chars)." };
  for (const seg of trimmed.split("/")) {
    if (seg === "" || seg === "." || seg === "..") {
      return { error: `Invalid path segment: "${seg}"` };
    }
  }
  return { path: trimmed };
}

export function CreateCollectionDialog({
  open,
  onOpenChange,
  vault,
  onCreated,
}: CreateCollectionDialogProps) {
  const [path, setPath] = useState("");
  const [summary, setSummary] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");

  useEffect(() => {
    if (!open) {
      setPath("");
      setSummary("");
      setError("");
      setInfo("");
    }
  }, [open]);

  async function handleSubmit() {
    setError("");
    setInfo("");
    const result = normalize(path);
    if ("error" in result) {
      setError(result.error);
      return;
    }
    setWorking(true);
    try {
      const res = await createCollection(vault, result.path, summary.trim() || undefined);
      if (res.created) {
        onCreated(res.collection.path);
        onOpenChange(false);
      } else {
        setInfo("Collection already exists.");
      }
    } catch (e) {
      // ApiError extends Error, so this branch covers both. We surface the
      // server-provided message verbatim; structured `detail` fields stay on
      // the ApiError instance if a future caller wants them.
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg || "Create failed");
    } finally {
      setWorking(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !working && onOpenChange(o)}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>New collection</DialogTitle>
          <DialogDescription>
            Create a new collection in{" "}
            <span className="font-mono font-semibold text-foreground">{vault}</span>.
            Paths may be nested (e.g. <span className="font-mono">specs/api</span>).
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div>
            <Label htmlFor="collection-path" className="coord-ink mb-1.5 block">
              Path
            </Label>
            <Input
              id="collection-path"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="api-specs"
              autoComplete="off"
              autoFocus
              className="font-mono"
              disabled={working}
            />
          </div>

          <div>
            <Label htmlFor="collection-summary" className="coord-ink mb-1.5 block">
              Summary (optional)
            </Label>
            <Input
              id="collection-summary"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              placeholder="What lives in this collection?"
              autoComplete="off"
              disabled={working}
            />
          </div>

          {error && (
            <div role="alert" className="border border-destructive p-2 text-xs text-destructive">
              {error}
            </div>
          )}
          {info && (
            <div className="border border-border p-2 text-xs text-foreground-muted">
              {info}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={working}
          >
            Cancel
          </Button>
          <Button type="button" onClick={handleSubmit} disabled={working}>
            {working ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Creating…
              </>
            ) : (
              "Create"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
