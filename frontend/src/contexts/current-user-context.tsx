import { createContext, useContext, type ReactNode } from "react";
import type { CurrentUser } from "@/lib/api";

const CurrentUserContext = createContext<CurrentUser | null>(null);

export function CurrentUserProvider({
  user,
  children,
}: {
  user: CurrentUser;
  children: ReactNode;
}) {
  return (
    <CurrentUserContext.Provider value={user}>
      {children}
    </CurrentUserContext.Provider>
  );
}

// The colocated hook keeps this small provider ergonomic across route modules.
// eslint-disable-next-line react-refresh/only-export-components
export function useCurrentUser(): CurrentUser | null {
  return useContext(CurrentUserContext);
}
