import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";

export interface ResourceLocation {
  vault: string;
  title: string;
  kind: "Document" | "File" | "Table";
  collectionPath?: string;
  /** Canonical ref from the authorized resource response, not a guessed title. */
  resourceRef?: string;
  status?: string;
}

interface Publication {
  owner: symbol;
  scope: string;
  location: ResourceLocation | null;
}

const ResourceLocationContext = createContext<{
  location: ResourceLocation | null;
  publish: (owner: symbol, location: ResourceLocation | null) => () => void;
} | null>(null);

/** A route owns its title only for the identity and access proof that loaded it. */
export function ResourceLocationProvider({
  identity,
  revision = 0,
  checking = false,
  children,
}: {
  identity: string;
  revision?: number;
  checking?: boolean;
  children: ReactNode;
}) {
  const route = useLocation();
  const scope = JSON.stringify([identity, revision, route.pathname]);
  const [publication, setPublication] = useState<Publication | null>(null);
  const publish = useCallback((owner: symbol, location: ResourceLocation | null) => {
    if (!checking) setPublication({ owner, scope, location });
    return () => setPublication(current => current?.owner === owner ? null : current);
  }, [checking, scope]);
  const location = !checking && publication?.scope === scope ? publication.location : null;
  const value = useMemo(() => ({ location, publish }), [location, publish]);
  return <ResourceLocationContext.Provider value={value}>{children}</ResourceLocationContext.Provider>;
}

// Page callers publish resolved data; previews deliberately opt out.
// eslint-disable-next-line react-refresh/only-export-components
export function usePublishResourceLocation(location: ResourceLocation | null, enabled = true) {
  const publish = useContext(ResourceLocationContext)?.publish;
  const vault = location?.vault;
  const title = location?.title;
  const kind = location?.kind;
  const collectionPath = location?.collectionPath;
  const resourceRef = location?.resourceRef;
  const status = location?.status;
  useLayoutEffect(() => {
    if (!enabled || !publish) return;
    const owner = Symbol("resource-location");
    return publish(owner, vault && title && kind ? { vault, title, kind, collectionPath, resourceRef, status } : null);
  }, [publish, enabled, vault, title, kind, collectionPath, resourceRef, status]);
}

// eslint-disable-next-line react-refresh/only-export-components
export function useResourceLocation(): ResourceLocation | null {
  return useContext(ResourceLocationContext)?.location ?? null;
}
