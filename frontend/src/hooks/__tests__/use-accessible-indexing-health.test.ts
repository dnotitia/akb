import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch, listVaults } from "@/lib/api";
import { loadAccessibleIndexingHealth } from "../use-accessible-indexing-health";

vi.mock("@/lib/api", () => ({
  authenticatedFetch: vi.fn(),
  listVaults: vi.fn(),
}));

const listVaultsMock = vi.mocked(listVaults);
const authenticatedFetchMock = vi.mocked(authenticatedFetch);

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("loadAccessibleIndexingHealth", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("aggregates only reader-visible Vault health and includes metadata work", async () => {
    listVaultsMock.mockResolvedValue({
      vaults: [
        { name: "platform" },
        { name: "shared research" },
        { name: "platform" },
      ],
    });
    authenticatedFetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("platform")) {
        return jsonResponse({
          vector_store: {
            backfill: { upsert: { pending: 3, abandoned: 1, indexed: 20 } },
          },
          metadata_backfill: { pending: 2, abandoned: 0 },
        });
      }
      return jsonResponse({
        vector_store: {
          backfill: { upsert: { pending: 4, abandoned: 0, indexed: 10 } },
        },
        metadata_backfill: { pending: 1, abandoned: 2 },
      });
    });

    await expect(loadAccessibleIndexingHealth()).resolves.toEqual({
      vaultCount: 2,
      checkedVaultCount: 2,
      pending: 10,
      abandoned: 3,
      indexed: 30,
      incomplete: false,
    });
    expect(authenticatedFetchMock).toHaveBeenCalledWith(
      "/health/vault/shared%20research",
    );
  });

  it("marks totals as a lower bound when one Vault status is unavailable", async () => {
    listVaultsMock.mockResolvedValue({
      vaults: [{ name: "available" }, { name: "temporarily-offline" }],
    });
    authenticatedFetchMock.mockImplementation(async (input) => {
      if (String(input).endsWith("temporarily-offline")) {
        return jsonResponse({ detail: "Unavailable" }, 503);
      }
      return jsonResponse({
        vector_store: {
          backfill: { upsert: { pending: 5, abandoned: 0, indexed: 12 } },
        },
      });
    });

    await expect(loadAccessibleIndexingHealth()).resolves.toEqual({
      vaultCount: 2,
      checkedVaultCount: 1,
      pending: 5,
      abandoned: 0,
      indexed: 12,
      incomplete: true,
    });
  });

  it("returns a complete quiet snapshot for an account with no Vaults", async () => {
    listVaultsMock.mockResolvedValue({ vaults: [] });

    await expect(loadAccessibleIndexingHealth()).resolves.toEqual({
      vaultCount: 0,
      checkedVaultCount: 0,
      pending: 0,
      abandoned: 0,
      indexed: 0,
      incomplete: false,
    });
    expect(authenticatedFetchMock).not.toHaveBeenCalled();
  });
});
