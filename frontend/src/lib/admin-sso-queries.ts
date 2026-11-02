import { useQuery } from "@tanstack/react-query";

import { getAdminSsoOwnership, type AdminSsoOwnership } from "@/lib/api";

export const OWNERSHIP_QUERY_ROOT = ["admin-sso-ownership", 1] as const;
export const OPERATION_REQUESTS_QUERY_KEY = ["admin-sso-operation-requests", 1] as const;

export function ownershipQueryKey(alias: string) {
  return [...OWNERSHIP_QUERY_ROOT, alias] as const;
}

export function useProviderOwnership(alias: string) {
  return useQuery<AdminSsoOwnership>({
    queryKey: ownershipQueryKey(alias),
    queryFn: () => getAdminSsoOwnership(alias),
    staleTime: 10_000,
  });
}
