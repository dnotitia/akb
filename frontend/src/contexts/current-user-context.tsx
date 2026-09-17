import { createContext, useContext, type ReactNode } from "react";
import type { CurrentUser } from "@/lib/api";

const CurrentUserContext = createContext<CurrentUser | null>(null);
const AccessVerificationContext = createContext({ checking: false, revision: 0 });

export function CurrentUserProvider({
  user,
  children,
  checking = false,
  revision = 0,
}: {
  user: CurrentUser | null;
  children: ReactNode;
  checking?: boolean;
  revision?: number;
}) {
  return (
    <CurrentUserContext.Provider value={user}>
      <AccessVerificationContext.Provider value={{ checking, revision }}>{children}</AccessVerificationContext.Provider>
    </CurrentUserContext.Provider>
  );
}

// The colocated hook keeps this small provider ergonomic across route modules.
// eslint-disable-next-line react-refresh/only-export-components
export function useCurrentUser(): CurrentUser | null {
  return useContext(CurrentUserContext);
}

// Same authenticated shell proof; consumers must not infer ACL freshness from
// an unchanged account ID (local sessions can gain/lose access too).
// eslint-disable-next-line react-refresh/only-export-components
export function useAccessVerification() { return useContext(AccessVerificationContext); }
