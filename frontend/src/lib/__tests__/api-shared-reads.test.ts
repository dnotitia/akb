import { afterEach, expect, it, vi } from "vitest";
import { getVaultInfo, listVaults } from "@/lib/api";

afterEach(() => vi.unstubAllGlobals());

it("shares concurrent identical shell reads and refetches once they settle", async () => {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify({ vaults: [] }), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);

  await Promise.all([listVaults(), listVaults(), getVaultInfo("v"), getVaultInfo("v")]);
  expect(fetchMock).toHaveBeenCalledTimes(2);

  await listVaults();
  expect(fetchMock).toHaveBeenCalledTimes(3);
});
