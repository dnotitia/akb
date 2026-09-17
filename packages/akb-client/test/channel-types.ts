import {
  createClient,
  type AkbChangeEventChannel,
  type AkbEventGapDetails,
  type ChangeEventEnvelopeV1,
  type EventCursor,
  type TailCheckpointV1,
} from "../src/index.js";
import { createClient as createLiteClient } from "../src/lite.js";

const main = createClient({ baseUrl: "https://akb.test/api/v1" });
const lite = createLiteClient({ baseUrl: "https://akb.test/api/v1" });
const mainChannel: AkbChangeEventChannel = main.vault("eng").channel();
const liteChannel = lite.vault("eng").channel();

const event: ChangeEventEnvelopeV1 = {
  version: 1,
  cursor: "ec1.event",
  occurred_at: "2026-01-01T00:00:00Z",
  vault: "eng",
  kind: "document.put",
  resource_uri: null,
  actor: null,
  payload: { id: "doc-1" },
};
const checkpoint: TailCheckpointV1 = { version: 1, cursor: "ec1.checkpoint" };
const gap: AkbEventGapDetails = {
  earliest_cursor: "ec1.earliest",
  latest_cursor: "ec1.latest",
};
const cursor: EventCursor = event.cursor;

const configured = mainChannel
  .on("change", { kinds: ["document.put", "future.kind"] }, (value) => {
    value satisfies ChangeEventEnvelopeV1;
  })
  .on("checkpoint", (value) => {
    value satisfies TailCheckpointV1;
  });
const subscription = await configured.subscribe({ cursor });
subscription.cursor satisfies EventCursor | null;
await subscription.unsubscribe();
await subscription.closed;

liteChannel.on("change", (value) => {
  value satisfies ChangeEventEnvelopeV1;
});

// @ts-expect-error The Tail only accepts the retained replay sentinel.
mainChannel.subscribe({ start: "current" });
// @ts-expect-error Change listener event names are closed over the public protocol events.
mainChannel.on("event", () => undefined);
// @ts-expect-error Event Kind values are strings, not numbers.
mainChannel.on("change", { kinds: [42] }, () => undefined);
// @ts-expect-error Checkpoint listeners receive checkpoints, not change envelopes.
mainChannel.on("checkpoint", (_value: ChangeEventEnvelopeV1) => undefined);

void checkpoint;
void gap;
