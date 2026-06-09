import { Component, useEffect, useRef, type ErrorInfo, type ReactNode } from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

interface Props {
  children: ReactNode;
  /** Fallback node. If a function, receives the error + a reset callback. */
  fallback?:
    | ReactNode
    | ((err: Error, reset: () => void) => ReactNode);
  /** Reset the boundary when any value in this array changes. */
  resetKeys?: unknown[];
}

interface State {
  error: Error | null;
}

/**
 * Catches render-time and lifecycle errors in descendants so a single
 * broken component (e.g. a lazy chunk that fails to load, or a third
 * party that hits a hooks-null issue in prod) doesn't unmount the whole
 * app and leave a blank page.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  componentDidUpdate(prev: Props) {
    const prevKeys = prev.resetKeys ?? [];
    const nextKeys = this.props.resetKeys ?? [];
    if (
      this.state.error &&
      (prevKeys.length !== nextKeys.length ||
        prevKeys.some((k, i) => !Object.is(k, nextKeys[i])))
    ) {
      this.reset();
    }
  }

  reset = () => this.setState({ error: null });

  render() {
    if (!this.state.error) return this.props.children;
    const { fallback } = this.props;
    if (typeof fallback === "function") {
      return fallback(this.state.error, this.reset);
    }
    return fallback ?? <DefaultFallback error={this.state.error} reset={this.reset} />;
  }
}

function DefaultFallback({ error, reset }: { error: Error; reset: () => void }) {
  // Move focus to the recovery action when the fallback mounts — otherwise a
  // keyboard user's focus is stranded on a control that just unmounted, and
  // role=alert only announces, it doesn't move focus.
  const retryRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    retryRef.current?.focus();
  }, []);
  return (
    <div className="flex items-center justify-center p-12" role="alert">
      <div className="max-w-md text-center">
        <div className="coord-spark mb-3">§ Something went wrong</div>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground mb-3">
          This view couldn&rsquo;t render
        </h1>
        <p className="text-sm text-foreground-muted leading-relaxed mb-6">
          {error.message || "An unexpected error occurred."}
        </p>
        <Button ref={retryRef} variant="outline" size="sm" onClick={reset}>
          <RefreshCw className="h-4 w-4" aria-hidden />
          Try again
        </Button>
      </div>
    </div>
  );
}
