// Mock Service Worker setup for vitest. Imported per-test (not from
// the global setup) so unit tests that mock @/lib/api at the module
// boundary keep their fast path — MSW is for tests that exercise the
// real fetch shape against handler stubs.
import { setupServer } from "msw/node";
import { http, HttpResponse } from "msw";

const API = "/api/v1";

export { http, HttpResponse };

export function makeServer(...handlers: Parameters<typeof setupServer>) {
  const server = setupServer(...handlers);
  return server;
}

// Re-export the URL prefix so individual tests don't repeat it.
export const apiUrl = (path: string) => API + path;
