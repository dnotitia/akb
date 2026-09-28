import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { makeServer, apiUrl } from "@/test-msw";

vi.mock("msw/browser", () => ({
  setupWorker: vi.fn(() => ({ start: vi.fn() })),
}));

import { handlers } from "@/mocks/browser";

const server = makeServer(...handlers);

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("browser mock handlers", () => {
  it("serves the guide template used by the vault overview", async () => {
    const response = await fetch(
      new URL(apiUrl("/help/skill-template"), window.location.origin),
    );

    expect(response.status).toBe(200);
    expect(await response.text()).toBe(
      "# {vault} Guide\n\n(Describe what this vault is for.)\n",
    );
  });
});
