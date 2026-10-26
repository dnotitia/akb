import type { QueryClient, QueryKey } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";

/** A denied snapshot must not reappear when a later retry fails transiently. */
export async function readWithAccessBoundary<T>(
  client: QueryClient,
  key: QueryKey,
  read: () => Promise<T>,
  signal?: AbortSignal,
): Promise<T | null> {
  try {
    return await read();
  } catch (error) {
    if (!signal?.aborted && error instanceof ApiError && [401, 403, 404].includes(error.status)) {
      // undefined is a no-op in QueryClient. null explicitly clears the prior
      // success while retaining the observer and its normal retry lifecycle.
      client.setQueryData(key, null);
    }
    throw error;
  }
}
