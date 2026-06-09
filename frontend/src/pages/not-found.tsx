import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";

/**
 * Catch-all for unmatched routes. Without this, an old/typo'd/stale-deploy URL
 * matched no route and React Router rendered nothing — a fully blank page with
 * no recovery path. This gives a clear message + a way back.
 */
export default function NotFoundPage() {
  return (
    <div className="mx-auto flex max-w-[640px] flex-col items-center px-6 py-24 text-center">
      <p className="coord mb-3">404 · not found</p>
      <h1 className="font-display text-[40px] leading-[1.1] text-foreground mb-3">
        Page not found
      </h1>
      <p className="text-foreground-muted mb-8">
        The page you're looking for doesn't exist or may have moved. Check the
        address, or head back to your workspace.
      </p>
      <Button asChild>
        <Link to="/">Back to workspace</Link>
      </Button>
    </div>
  );
}
