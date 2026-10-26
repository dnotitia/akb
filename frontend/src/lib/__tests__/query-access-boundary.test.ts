import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import { readWithAccessBoundary } from "@/lib/query-access-boundary";

describe("readWithAccessBoundary", () => {
  it.each([401, 403, 404])("clears a denied %i snapshot until another read succeeds", async status => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const key = ["private", "alice", "resource"];
    client.setQueryData(key, { content: "Private snapshot" });
    const fail = (code: number) => readWithAccessBoundary(client, key, () => Promise.reject(new ApiError("Unavailable", code, {})));
    await expect(fail(status)).rejects.toMatchObject({ status });
    expect(client.getQueryData(key)).toBeNull();
    await expect(fail(503)).rejects.toMatchObject({ status: 503 });
    expect(client.getQueryData(key)).toBeNull();
    const fresh = await client.fetchQuery({ queryKey: key, queryFn: () => readWithAccessBoundary(client, key, async () => ({ content: "Fresh" })) });
    expect(fresh).toEqual({ content: "Fresh" });
    expect(client.getQueryData(key)).toEqual(fresh);
  });
  it("preserves a verified snapshot on a transient failure", async () => {
    const client = new QueryClient();
    const key = ["private", "alice", "resource"];
    client.setQueryData(key, { content: "Prior" });
    await expect(readWithAccessBoundary(client, key, () => Promise.reject(new ApiError("Offline", 503, {})))).rejects.toMatchObject({ status: 503 });
    expect(client.getQueryData(key)).toEqual({ content: "Prior" });
  });
});

it("does not purge a newer success when an aborted old request is denied", async () => {
  const client = new QueryClient();
  const key = ["private", "alice", "resource"];
  const controller = new AbortController();
  let rejectOld!: (error: Error) => void;
  const oldRead = readWithAccessBoundary(client, key, () => new Promise((_resolve, reject) => { rejectOld = reject; }), controller.signal);
  controller.abort();
  client.setQueryData(key, { content: "New authorized snapshot" });
  rejectOld(new ApiError("Old denial", 403, {}));
  await expect(oldRead).rejects.toMatchObject({ status: 403 });
  expect(client.getQueryData(key)).toEqual({ content: "New authorized snapshot" });
});
