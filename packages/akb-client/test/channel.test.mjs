import assert from "node:assert/strict";
import { test, vi } from "vitest";

import { AkbError, createClient } from "../dist/index.js";
import { createClient as createLiteClient } from "../dist/lite.js";

const textEncoder = new TextEncoder();
const CHECKPOINT_READY = "event: checkpoint\nid: ec1.ready\ndata: {\"version\":1,\"cursor\":\"ec1.ready\"}\n\n";

test("channel is vault-scoped and exposes the same auth boundary from main and lite", async () => {
  let fetchCalls = 0;
  const fetch = async () => {
    fetchCalls += 1;
    return new Response(null, { status: 204 });
  };
  const root = createClient({ baseUrl: "https://akb.test/api/v1", fetch });
  const lite = createLiteClient({ baseUrl: "https://akb.test/api/v1", fetch });

  assert.throws(() => root.channel(), /Select a vault/);
  assert.throws(() => lite.channel(), /Select a vault/);
  assert.throws(() => root.vault("vault/one").channel().on("message", () => undefined), TypeError);
  assert.equal(fetchCalls, 0);
  assert.equal(typeof root.vault("vault/one").channel, "function");
  assert.equal(typeof lite.vault("vault/one").channel, "function");
});

test("channel parses split SSE frames, filters listeners, advances through checkpoints, and unsubscribes", async () => {
  const calls = [];
  const tokens = ["token-one"];
  const order = [];
  const changes = [];
  const checkpoints = [];
  const fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    calls.push({ url, headers: Object.fromEntries(new Headers(init.headers)) });
    return sseResponse([
      ": heartbeat\r\n\r\n",
      "retry: 1\r\n\r\n",
      "event: checkpoint\r\nid: ec1.checkpoint\r\ndata: {\"version\":1,\"cursor\":\"ec1.checkpoint\"}\r\n\r\n",
      "event: change\r\nid: ec1.a\r\ndata: {\"version\":1,\"cursor\":\"ec1.a\",\"occurred_at\":\"2026-01-01T00:00:00Z\",\"vault\":\"vault/one\",\"kind\":\"document.put\",\"payload\":{\"id\":\"a\"}}\r\n\r\n",
      "event: change\r\nid: ec1.b\r\ndata: {\"version\":1,\"cursor\":\"ec1.b\",\"occurred_at\":\"2026-01-01T00:00:01Z\",\"vault\":\"vault/one\",\"kind\":\"table.rows_changed\",\"payload\":{\"id\":\"b\"}}\r\n\r\n",
      "event: change\r\nid: ec1.z\r\ndata: {\"version\":1,\"cursor\":\"ec1.z\",\"occurred_at\":\"2026-01-01T00:00:02Z\",\"vault\":\"vault/one\",\"kind\":\"document.put\",\"payload\":{\"id\":\"z\"}}\r\n\r\n",
    ], 200, init.signal, false);
  };
  const claims = { sub: "user-1", app_metadata: { org_id: "org-1", role: "reader" } };
  const client = createClient({
    baseUrl: "https://akb.test/api/v1",
    token: () => tokens.at(-1),
    fetch,
  }).vault("vault/one").actingAs(claims);

  const subscription = await client.channel()
    .on("change", { kinds: ["document.put", "table.rows_changed", "document.put"] }, async (event) => {
      order.push(`first:${event.kind}`);
      await Promise.resolve();
      changes.push(["first", event.kind]);
    })
    .on("change", { kinds: ["table.rows_changed"] }, (event) => {
      order.push(`second:${event.kind}`);
      changes.push(["second", event.kind]);
    })
    .on("checkpoint", (checkpoint) => {
      checkpoints.push(checkpoint.cursor);
    })
    .subscribe({ start: "earliest" });

  await waitFor(() => changes.length >= 4);
  assert.deepEqual(calls[0].url.pathname, "/api/v1/events/vault%2Fone");
  assert.deepEqual(calls[0].url.searchParams.getAll("kind"), ["document.put", "table.rows_changed"]);
  assert.equal(calls[0].url.searchParams.get("start"), "earliest");
  assert.equal(calls[0].headers.authorization, "Bearer token-one");
  assert.equal(calls[0].headers["x-akb-claims"], JSON.stringify(claims));
  assert.deepEqual(checkpoints, ["ec1.checkpoint"]);
  assert.deepEqual(order, ["first:document.put", "first:table.rows_changed", "second:table.rows_changed", "first:document.put"]);
  assert.equal(subscription.cursor, "ec1.z");

  await subscription.unsubscribe();
  await subscription.closed;
});

test("reconnects after a retryable handshake and re-evaluates the token provider", async () => {
  const calls = [];
  let tokenCalls = 0;
  let attempt = 0;
  const fetch = async (input, init = {}) => {
    const url = new URL(String(input));
    const headers = Object.fromEntries(new Headers(init.headers));
    calls.push({ url, headers });
    attempt += 1;
    if (attempt === 1) {
      return json({ message: "temporary", code: "temporary_failure" }, 503, "Unavailable");
    }
    return sseResponse([
      "event: checkpoint\nid: ec1.ready\ndata: {\"version\":1,\"cursor\":\"ec1.ready\"}\n\n",
    ]);
  };
  const client = createClient({
    baseUrl: "https://akb.test/api/v1",
    token: () => `token-${++tokenCalls}`,
    fetch,
  }).vault("retry vault");

  const subscription = await client.channel().subscribe({ cursor: "ec1.start" });
  await waitFor(() => subscription.cursor === "ec1.ready");
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url.searchParams.get("cursor"), "ec1.start");
  assert.equal(calls[0].headers.authorization, "Bearer token-1");
  assert.equal(calls[1].headers["last-event-id"], "ec1.start");
  assert.equal(calls[1].headers.authorization, "Bearer token-2");
  await subscription.unsubscribe();
});

test("listener failure rejects closed and preserves the last safe cursor", async () => {
  const original = new Error("handler failed");
  const fetch = async () => sseResponse([
    "event: checkpoint\nid: ec1.safe\ndata: {\"version\":1,\"cursor\":\"ec1.safe\"}\n\n",
    "event: change\nid: ec1.failed\ndata: {\"version\":1,\"cursor\":\"ec1.failed\",\"occurred_at\":\"2026-01-01T00:00:00Z\",\"vault\":\"vault\",\"kind\":\"document.put\",\"payload\":{}}\n\n",
  ]);
  const subscription = await createClient({
    baseUrl: "https://akb.test/api/v1",
    fetch,
  }).vault("vault").channel().on("change", () => {
    throw original;
  }).subscribe();

  await assert.rejects(subscription.closed, (error) => error === original);
  assert.equal(subscription.cursor, "ec1.safe");
});

test("event gap and invalid cursor remain AkbError responses", async () => {
  const fetch = async (_input, init = {}) => {
    assert.equal(init.headers instanceof Headers, true);
    return json({
      message: "Event cursor is outside the retained Vault tail",
      code: "event_gap",
      details: { earliest_cursor: "ec1.earliest", latest_cursor: "ec1.latest" },
    }, 410, "Gone");
  };
  const client = createClient({ baseUrl: "https://akb.test/api/v1", fetch }).vault("vault");
  await assert.rejects(
    client.channel().subscribe({ cursor: "ec1.old" }),
    (error) => error instanceof AkbError
      && error.code === "event_gap"
      && error.details.earliest_cursor === "ec1.earliest"
      && error.details.latest_cursor === "ec1.latest",
  );
});

test("a refused open waits at least its Retry-After, within the jitter bound", async () => {
  // Retry-After: 5 allows less than min(20% of 5 s, 1 s) of jitter on top. Math.random() = 0
  // takes the bottom of that range and the largest value below 1 its top.
  for (const [random, expected] of [[0, 5_000], [1 - 2 ** -53, 5_999]]) {
    await withFakeClock(async () => {
      vi.spyOn(Math, "random").mockReturnValue(random);
      const attempts = [];
      const fetch = async (_input, init = {}) => {
        attempts.push(Date.now());
        if (attempts.length === 1) {
          return json(
            { message: "Event tail capacity is exhausted on this server", code: "event_tail_capacity" },
            503,
            "Service Unavailable",
            { "retry-after": "5" },
          );
        }
        return sseResponse([CHECKPOINT_READY], 200, init.signal, false);
      };
      const opening = createClient({ baseUrl: "https://akb.test/api/v1", fetch })
        .vault("vault")
        .channel()
        .subscribe();

      await until(() => vi.getTimerCount() > 0, "the wait after the refusal");
      await vi.advanceTimersByTimeAsync(expected - 1);
      await settle();
      assert.equal(attempts.length, 1, `retried before ${expected} ms`);
      await vi.advanceTimersByTimeAsync(1);
      await until(() => attempts.length === 2, "the second attempt");
      assert.equal(attempts[1] - attempts[0], expected);
      const subscription = await opening;
      await subscription.unsubscribe();
    });
  }
});

test("without Retry-After, refused opens back off exponentially with jitter up to 30 s", async () => {
  // Math.random() = 0 takes the bottom of each jittered range, the largest value the top.
  for (const [random, expected] of [
    [0, [250, 500, 1_000, 2_000, 4_000, 8_000, 15_000, 15_000]],
    [1 - 2 ** -53, [500, 1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000]],
  ]) {
    await withFakeClock(async () => {
      vi.spyOn(Math, "random").mockReturnValue(random);
      const refusals = expected.length;
      const attempts = [];
      const fetch = async (_input, init = {}) => {
        attempts.push(Date.now());
        if (attempts.length <= refusals) {
          return json({ message: "temporary", code: "temporary_failure" }, 503, "Service Unavailable");
        }
        return sseResponse([CHECKPOINT_READY], 200, init.signal, false);
      };
      const opening = createClient({ baseUrl: "https://akb.test/api/v1", fetch })
        .vault("vault")
        .channel()
        .subscribe();

      const delays = await attemptDelays(attempts, refusals + 1);
      assert.deepEqual(delays, expected);
      const subscription = await opening;
      await subscription.unsubscribe();
    });
  }
});

test("the first event of a stream resets the backoff", async () => {
  await withFakeClock(async () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    const attempts = [];
    const fetch = async (_input, init = {}) => {
      attempts.push(Date.now());
      switch (attempts.length) {
        case 4:
          // Opens, sends its start checkpoint and ends: the channel reconnects.
          return sseResponse([CHECKPOINT_READY]);
        case 6:
          return sseResponse([CHECKPOINT_READY], 200, init.signal, false);
        default:
          return json({ message: "temporary", code: "temporary_failure" }, 503, "Service Unavailable");
      }
    };
    const opening = createClient({ baseUrl: "https://akb.test/api/v1", fetch })
      .vault("vault")
      .channel()
      .subscribe();

    const delays = await attemptDelays(attempts, 6);
    // Three refusals back off; the stream that sent its checkpoint ends and is
    // reopened after the reconnection time; the next refusal starts the backoff again.
    assert.deepEqual(delays, [250, 500, 1_000, 250, 250]);
    const subscription = await opening;
    await subscription.unsubscribe();
  });
});

test("a stream that ends before its first event or comment keeps the backoff growing", async () => {
  await withFakeClock(async () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    const attempts = [];
    const fetch = async (_input, init = {}) => {
      attempts.push(Date.now());
      switch (attempts.length) {
        case 2:
          // Accepted, then closed before a single line, as a proxy that drops it.
          return sseResponse([]);
        case 3:
          // Accepted, then failed before a single line.
          return sseResponse([], 200, null, false, new TypeError("terminated"));
        case 4:
          // A comment line is enough to show the stream is live.
          return sseResponse([": heartbeat\n\n"]);
        case 6:
          return sseResponse([CHECKPOINT_READY], 200, init.signal, false);
        default:
          return json({ message: "temporary", code: "temporary_failure" }, 503, "Service Unavailable");
      }
    };
    const opening = createClient({ baseUrl: "https://akb.test/api/v1", fetch })
      .vault("vault")
      .channel()
      .subscribe();

    const delays = await attemptDelays(attempts, 6);
    // The refusal and the two empty streams back off in a row; the stream that
    // sent a comment is reopened after the reconnection time, and the next
    // refusal starts the backoff again.
    assert.deepEqual(delays, [250, 500, 1_000, 250, 250]);
    const subscription = await opening;
    await subscription.unsubscribe();
  });
});

test("Retry-After may be an HTTP-date, is waited in full past 30 s, and never skips the backoff", async () => {
  await withFakeClock(async () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    vi.setSystemTime(Date.UTC(2026, 10, 13, 0, 0, 0));
    const refusals = [
      // Measured from the response's own Date header, not the local clock.
      { date: "Fri, 13 Nov 2026 00:00:30 GMT", "retry-after": "Fri, 13 Nov 2026 00:00:37 GMT" },
      // Without a Date header, measured from the local clock (00:00:07 by now).
      { "retry-after": "Fri, 13 Nov 2026 00:00:10 GMT" },
      { "retry-after": "120" },
      // A zero wait does not bypass the backoff of the fourth refusal in a row.
      { "retry-after": "0" },
    ];
    const attempts = [];
    const fetch = async (_input, init = {}) => {
      attempts.push(Date.now());
      const headers = refusals[attempts.length - 1];
      if (headers) {
        return json({ message: "temporary", code: "event_tail_unavailable" }, 503, "Service Unavailable", headers);
      }
      return sseResponse([CHECKPOINT_READY], 200, init.signal, false);
    };
    const opening = createClient({ baseUrl: "https://akb.test/api/v1", fetch })
      .vault("vault")
      .channel()
      .subscribe();

    const delays = await attemptDelays(attempts, refusals.length + 1);
    assert.deepEqual(delays, [7_000, 3_000, 120_000, 2_000]);
    const subscription = await opening;
    await subscription.unsubscribe();
  });
});

test("a Retry-After longer than one timer allows is waited in full, and stops with the channel", async () => {
  // 3,000,000 s is more than the 2^31 - 1 ms a single timer can wait.
  const longRefusal = () => json({ message: "down for maintenance", code: "unavailable" }, 503, "Service Unavailable", {
    "retry-after": "3000000",
  });
  const channelOf = (fetch) => createClient({ baseUrl: "https://akb.test/api/v1", fetch }).vault("vault").channel();

  await withFakeClock(async () => {
    vi.spyOn(Math, "random").mockReturnValue(0);
    const attempts = [];
    const opening = channelOf(async (_input, init = {}) => {
      attempts.push(Date.now());
      return attempts.length === 1 ? longRefusal() : sseResponse([CHECKPOINT_READY], 200, init.signal, false);
    }).subscribe();

    assert.deepEqual(await attemptDelays(attempts, 2), [3_000_000_000]);
    await (await opening).unsubscribe();
  });

  // Aborting the initial open in its first timer, or in a later one, leaves no
  // timer and opens nothing more.
  for (const timersRun of [0, 1]) {
    await withFakeClock(async () => {
      const attempts = [];
      const controller = new AbortController();
      const opening = channelOf(async () => {
        attempts.push(Date.now());
        return longRefusal();
      }).subscribe({ signal: controller.signal });

      await until(() => vi.getTimerCount() > 0, "the first timer");
      for (let run = 0; run < timersRun; run += 1) {
        await vi.advanceTimersToNextTimerAsync();
        await until(() => vi.getTimerCount() > 0, "the next timer of the chain");
      }
      controller.abort();
      await (await opening).closed;
      assert.equal(vi.getTimerCount(), 0, `a timer is left after ${timersRun} ran`);
      await vi.advanceTimersByTimeAsync(4_000_000_000);
      await settle();
      assert.equal(attempts.length, 1, `opened again after ${timersRun} ran`);
    });
  }

  // So does unsubscribe() during a later timer of a reconnect after an opened stream.
  await withFakeClock(async () => {
    const attempts = [];
    const subscription = await channelOf(async () => {
      attempts.push(Date.now());
      return attempts.length === 1 ? sseResponse([CHECKPOINT_READY]) : longRefusal();
    }).subscribe();

    await until(() => vi.getTimerCount() > 0, "the reconnection time");
    await vi.advanceTimersToNextTimerAsync();
    await until(() => attempts.length === 2 && vi.getTimerCount() > 0, "the first timer of the long wait");
    await vi.advanceTimersToNextTimerAsync();
    await until(() => vi.getTimerCount() > 0, "the next timer of the chain");
    await subscription.unsubscribe();
    assert.equal(vi.getTimerCount(), 0, "a timer is left after unsubscribe()");
    await vi.advanceTimersByTimeAsync(4_000_000_000);
    await settle();
    assert.equal(attempts.length, 2, "opened again after unsubscribe()");
  });
});

test("refusals after a stream has opened honour Retry-After and keep backing off", async () => {
  // A stream sends its checkpoint and ends; six refusals with Retry-After: 5 follow.
  for (const [random, expected] of [
    [0, [250, 5_000, 5_000, 5_000, 5_000, 5_000, 8_000]],
    [1 - 2 ** -53, [250, 5_999, 5_999, 5_999, 5_999, 8_999, 16_999]],
  ]) {
    await withFakeClock(async () => {
      vi.spyOn(Math, "random").mockReturnValue(random);
      const attempts = [];
      const fetch = async (_input, init = {}) => {
        attempts.push(Date.now());
        if (attempts.length === 1) return sseResponse([CHECKPOINT_READY]);
        if (attempts.length <= 7) {
          return json({ message: "busy", code: "event_tail_capacity" }, 503, "Service Unavailable", {
            "retry-after": "5",
          });
        }
        return sseResponse([CHECKPOINT_READY], 200, init.signal, false);
      };
      const subscription = await createClient({ baseUrl: "https://akb.test/api/v1", fetch })
        .vault("vault")
        .channel()
        .subscribe();

      assert.deepEqual(await attemptDelays(attempts, 8), expected);
      await subscription.unsubscribe();
    });
  }
});

test("the Retry-After jitter goes on top of a longer SSE retry", async () => {
  // retry: 30000 makes the channel's own wait 30 s; two refusals with Retry-After: 5 follow.
  for (const [random, expected] of [
    [0, [30_000, 30_000, 30_000]],
    [1 - 2 ** -53, [30_000, 30_999, 30_999]],
  ]) {
    await withFakeClock(async () => {
      vi.spyOn(Math, "random").mockReturnValue(random);
      const attempts = [];
      const fetch = async (_input, init = {}) => {
        attempts.push(Date.now());
        if (attempts.length === 1) return sseResponse(["retry: 30000\n\n", CHECKPOINT_READY]);
        if (attempts.length <= 3) {
          return json({ message: "busy", code: "event_tail_capacity" }, 503, "Service Unavailable", {
            "retry-after": "5",
          });
        }
        return sseResponse([CHECKPOINT_READY], 200, init.signal, false);
      };
      const subscription = await createClient({ baseUrl: "https://akb.test/api/v1", fetch })
        .vault("vault")
        .channel()
        .subscribe();

      assert.deepEqual(await attemptDelays(attempts, 4), expected);
      await subscription.unsubscribe();
    });
  }
});

test("the Retry-After jitter never exceeds 20% of a Retry-After", async () => {
  await withFakeClock(async () => {
    vi.spyOn(Math, "random").mockReturnValue(1 - 2 ** -53);
    // Without a Date header, this HTTP-date is 1003 ms after the local clock.
    vi.setSystemTime(Date.parse("2026-11-13T00:00:00.997Z"));
    const attempts = [];
    const fetch = async (_input, init = {}) => {
      attempts.push(Date.now());
      if (attempts.length === 1) {
        return json({ message: "busy", code: "event_tail_unavailable" }, 503, "Service Unavailable", {
          "retry-after": "Fri, 13 Nov 2026 00:00:02 GMT",
        });
      }
      return sseResponse([CHECKPOINT_READY], 200, init.signal, false);
    };
    const opening = createClient({ baseUrl: "https://akb.test/api/v1", fetch })
      .vault("vault")
      .channel()
      .subscribe();

    const [delay] = await attemptDelays(attempts, 2);
    // At most 1003 + 20% of 1003 = 1203.6 ms, so 1203 ms at the top of the range.
    assert.equal(delay, 1_203);
    const subscription = await opening;
    await subscription.unsubscribe();
  });
});

test("after an SSE retry of 1000 ms, refusals in a row back off from it", async () => {
  for (const [random, expected] of [
    [0, [1_000, 1_000, 2_000]],
    [1 - 2 ** -53, [1_000, 2_000, 4_000]],
  ]) {
    await withFakeClock(async () => {
      vi.spyOn(Math, "random").mockReturnValue(random);
      const attempts = [];
      const fetch = async (_input, init = {}) => {
        attempts.push(Date.now());
        if (attempts.length === 1) return sseResponse(["retry: 1000\n\n", CHECKPOINT_READY]);
        if (attempts.length <= 3) {
          return json({ message: "temporary", code: "temporary_failure" }, 503, "Service Unavailable");
        }
        return sseResponse([CHECKPOINT_READY], 200, init.signal, false);
      };
      const subscription = await createClient({ baseUrl: "https://akb.test/api/v1", fetch })
        .vault("vault")
        .channel()
        .subscribe();

      // The stream that set it is reopened after 1000 ms; the two refusals that
      // follow wait in [1000, 2000] and [2000, 4000] ms, not from 250 ms.
      assert.deepEqual(await attemptDelays(attempts, 4), expected);
      await subscription.unsubscribe();
    });
  }
});

test("a stream cut mid-event is reopened from the last cursor instead of failing", async () => {
  const checkpointStart = "event: checkpoint\nid: ec1.start\ndata: {\"version\":1,\"cursor\":\"ec1.start\"}\n\n";
  const changeA = "event: change\nid: ec1.a\ndata: {\"version\":1,\"cursor\":\"ec1.a\",\"occurred_at\":\"2026-01-01T00:00:00Z\",\"vault\":\"vault\",\"kind\":\"document.put\",\"payload\":{}}\n\n";
  for (const [cut, tail] of [
    ["inside its JSON", changeA.slice(0, changeA.indexOf("\"occurred_at\""))],
    ["before its terminating blank line", changeA.slice(0, -1)],
  ]) {
    const calls = [];
    const changes = [];
    const fetch = async (_input, init = {}) => {
      calls.push(Object.fromEntries(new Headers(init.headers)));
      if (calls.length === 1) return sseResponse([checkpointStart, tail]);
      return sseResponse([changeA], 200, init.signal, false);
    };
    const subscription = await createClient({ baseUrl: "https://akb.test/api/v1", fetch })
      .vault("vault")
      .channel()
      .on("change", (event) => changes.push({ cursor: event.cursor, stream: calls.length }))
      .subscribe();
    let failure = null;
    subscription.closed.catch((error) => {
      failure = error;
    });

    await waitFor(() => failure !== null || changes.some((change) => change.stream === 2));
    assert.equal(failure, null, `a stream cut ${cut} ended the channel`);
    // The cut event is discarded, so the second stream resumes after the checkpoint.
    assert.equal(calls.length, 2, cut);
    assert.equal(calls[1]["last-event-id"], "ec1.start", cut);
    assert.deepEqual(changes, [{ cursor: "ec1.a", stream: 2 }], cut);
    assert.equal(subscription.cursor, "ec1.a", cut);
    await subscription.unsubscribe();
    await subscription.closed;
  }
});

test("a CRLF split across chunks is one line ending, and a lone CR still ends a line", async () => {
  const data = "data: {\"version\":1,\"cursor\":\"ec1.x\"}";
  for (const [shape, chunks] of [
    // Every CR of the frame is the last byte of a chunk and its LF the first of the next.
    ["CRLF split after each CR", ["event: checkpoint\r", "\nid: ec1.x\r", `\n${data}\r`, "\n\r", "", "\n"]],
    ["CR-only line endings split after each CR", ["event: checkpoint\r", "id: ec1.x\r", `${data}\r`, "\r"]],
    ["an empty chunk between the CR and LF of a field line", ["event: checkpoint\r", "", "\nid: ec1.x\r\n", `${data}\r\n`, "\r\n"]],
    ["a full CRLF, then a chunk that starts with the blank line's LF", [`event: checkpoint\r\nid: ec1.x\r\n${data}\r\n`, "\n"]],
  ]) {
    const checkpoints = [];
    const fetch = async (_input, init = {}) => sseResponse(chunks, 200, init.signal, false);
    const subscription = await createClient({ baseUrl: "https://akb.test/api/v1", fetch })
      .vault("vault")
      .channel()
      .on("checkpoint", (checkpoint) => checkpoints.push(checkpoint.cursor))
      .subscribe();
    let failure = null;
    subscription.closed.catch((error) => {
      failure = error;
    });

    await waitFor(() => failure !== null || checkpoints.length > 0);
    assert.equal(failure, null, `${shape} ended the channel`);
    assert.deepEqual(checkpoints, ["ec1.x"], shape);
    assert.equal(subscription.cursor, "ec1.x", shape);
    await subscription.unsubscribe();
    await subscription.closed;
  }
});

test("a 429 user quota refusal still ends the channel and carries its code and Retry-After", async () => {
  const quota = () => json(
    {
      message: "This account has too many open event streams on this server",
      code: "event_tail_user_quota",
      hint: "Close event streams this account no longer reads.",
    },
    429,
    "Too Many Requests",
    { "retry-after": "5" },
  );
  const isQuotaError = (error) => error instanceof AkbError
    && error.status === 429
    && error.code === "event_tail_user_quota"
    && error.hint === "Close event streams this account no longer reads."
    && error.retryAfterMs === 5_000;

  let openCalls = 0;
  const refusedOpen = createClient({
    baseUrl: "https://akb.test/api/v1",
    fetch: async () => {
      openCalls += 1;
      return quota();
    },
  }).vault("vault");
  await assert.rejects(refusedOpen.channel().subscribe(), isQuotaError);
  assert.equal(openCalls, 1);

  let reconnectCalls = 0;
  const refusedReconnect = createClient({
    baseUrl: "https://akb.test/api/v1",
    fetch: async () => {
      reconnectCalls += 1;
      return reconnectCalls === 1 ? sseResponse([CHECKPOINT_READY]) : quota();
    },
  }).vault("vault");
  const subscription = await refusedReconnect.channel().subscribe();
  await assert.rejects(subscription.closed, isQuotaError);
  assert.equal(reconnectCalls, 2);
  assert.equal(subscription.cursor, "ec1.ready");
});

function sseResponse(chunks, status = 200, signal = null, close = true, failure = null) {
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(textEncoder.encode(chunk));
      if (failure) {
        controller.error(failure);
      } else if (close) {
        controller.close();
      } else {
        signal?.addEventListener("abort", () => {
          controller.error(new DOMException("aborted", "AbortError"));
        }, { once: true });
      }
    },
  });
  return new Response(body, {
    status,
    headers: { "content-type": "text/event-stream" },
  });
}

function json(body, status, statusText, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { "content-type": "application/json", ...headers },
  });
}

async function waitFor(predicate) {
  const deadline = Date.now() + 2_000;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("timed out waiting for channel state");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

// Only the channel's reconnect timers and the clock are faked; response bodies
// are still read on the real event loop.
async function withFakeClock(run) {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
  try {
    await run();
  } finally {
    vi.useRealTimers();
    vi.restoreAllMocks();
  }
}

// Turns the real event loop, on which response bodies are read, until `done()`.
async function until(done, what) {
  for (let turn = 0; turn < 1_000; turn += 1) {
    if (done()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error(`timed out waiting for ${what}`);
}

// Gives anything the channel would do next a chance to happen.
async function settle() {
  for (let turn = 0; turn < 20; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

// Runs each reconnect wait to its end and returns the waits between the
// recorded attempts, in fake milliseconds.
async function attemptDelays(attempts, count) {
  await until(() => attempts.length > 0, "the first attempt");
  while (attempts.length < count) {
    const before = attempts.length;
    // A wait longer than one timer allows is a chain of timers; run each in turn.
    while (attempts.length === before) {
      await until(() => vi.getTimerCount() > 0 || attempts.length > before, `the wait after attempt ${before}`);
      if (attempts.length > before) break;
      await vi.advanceTimersToNextTimerAsync();
      await until(() => attempts.length > before || vi.getTimerCount() > 0, `attempt ${before + 1}`);
    }
  }
  return attempts.slice(1).map((at, index) => at - attempts[index]);
}
