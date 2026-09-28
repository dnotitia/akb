import {
  createContext,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import type { NavigateOptions } from "react-router-dom";

type ResourceNavigationGuard = (href: string, options?: NavigateOptions) => boolean;

interface ResourceNavigationContextValue {
  /** False means the resource is handling confirmation; prevent the Link's default navigation. */
  requestNavigation: (href: string, options?: NavigateOptions) => boolean;
  registerGuard: (guard: ResourceNavigationGuard) => () => void;
}

const ResourceNavigationContext = createContext<ResourceNavigationContextValue>({
  requestNavigation: () => true,
  registerGuard: () => () => {},
});

/** Explicitly participating resource links consult the current editor only. */
export function ResourceNavigationProvider({ children }: { children: ReactNode }) {
  const guardRef = useRef<{ owner: symbol; guard: ResourceNavigationGuard } | null>(null);
  const requestNavigation = useCallback((href: string, options?: NavigateOptions) => {
    const guard = guardRef.current?.guard;
    if (!guard) return true;
    return options ? guard(href, options) : guard(href);
  }, []);
  const registerGuard = useCallback((guard: ResourceNavigationGuard) => {
    const owner = Symbol("resource-navigation");
    guardRef.current = { owner, guard };
    return () => {
      if (guardRef.current?.owner === owner) guardRef.current = null;
    };
  }, []);
  const value = useMemo(() => ({ requestNavigation, registerGuard }), [requestNavigation, registerGuard]);
  return <ResourceNavigationContext.Provider value={value}>{children}</ResourceNavigationContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useResourceNavigation() {
  return useContext(ResourceNavigationContext);
}

// eslint-disable-next-line react-refresh/only-export-components
export function useResourceNavigationGuard(guard: ResourceNavigationGuard | null) {
  const { registerGuard } = useContext(ResourceNavigationContext);
  useLayoutEffect(() => {
    if (guard) return registerGuard(guard);
  }, [guard, registerGuard]);
}
