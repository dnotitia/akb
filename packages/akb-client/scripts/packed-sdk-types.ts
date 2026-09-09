import {
  createClient,
  type AkbChangeEventChannel,
  type AkbEventGapDetails,
  type AkbOperationResponse,
  type ChangeEventEnvelopeV1,
  type CreateCollectionRequest,
  type EventCursor,
  type EventKind,
  type LinkRequest,
  type TailCheckpointV1,
  type operations,
} from "@akb/client";
import {
  createClient as createLiteClient,
  type AkbClient as LiteClient,
  type AkbChangeEventChannel as LiteChangeEventChannel,
  type CreateCollectionRequest as LiteCreateCollectionRequest,
  type LinkRequest as LiteLinkRequest,
} from "@akb/client/lite";
import {
  createControlPlaneAdminClient,
  createControlPlaneAppClient,
  exchangeAppCredential,
  type ControlPlaneOperations,
  type DesiredSchemaProjection,
  type ReleaseManifest,
  type RolloutRequest,
} from "@akb/client/control-plane";

type Body<Operation> = Operation extends { requestBody: infer RequestBody }
  ? RequestBody
  : never;
type Equal<Left, Right> =
  (<T>() => T extends Left ? 1 : 2) extends
  (<T>() => T extends Right ? 1 : 2)
    ? (<T>() => T extends Right ? 1 : 2) extends
      (<T>() => T extends Left ? 1 : 2)
      ? true
      : false
    : false;
type Assert<Value extends true> = Value;

type _GraphLinkBody = Assert<Equal<Body<operations["graphLink"]>, LinkRequest>>;
type _CollectionCreateBody = Assert<
  Equal<Body<operations["collectionsCreateCollection"]>, CreateCollectionRequest>
>;
type _DropResponse = Assert<
  Equal<
    AkbOperationResponse<operations["tablesDeleteTableName"]>["kind"],
    "table"
  >
>;
type _LiteLink = Assert<Equal<LinkRequest, LiteLinkRequest>>;
type _LiteCollection = Assert<
  Equal<CreateCollectionRequest, LiteCreateCollectionRequest>
>;
type _ControlPlaneOperation = Assert<
  Equal<ControlPlaneOperations["appsCreate"]["requestBody"]["app_key"], string>
>;
type _ControlPlaneRollout = Assert<
  Equal<RolloutRequest["manifest_checksum"], string>
>;
type _ManifestVersion = Assert<Equal<ReleaseManifest["manifest_version"], 2>>;
type _ManifestSchema = Assert<
  Equal<ReleaseManifest["schema"], DesiredSchemaProjection>
>;

const main = createClient({
  baseUrl: "https://packed.invalid/api/v1",
  defaultVault: "packed",
});
const lite: LiteClient = createLiteClient({
  baseUrl: "https://packed.invalid/api/v1",
  defaultVault: "packed",
});

type _ChannelParity = Assert<Equal<AkbChangeEventChannel, LiteChangeEventChannel>>;
const mainChannel: AkbChangeEventChannel = main.channel();
const liteChannel: LiteChangeEventChannel = lite.channel();
mainChannel satisfies LiteChangeEventChannel;
liteChannel satisfies AkbChangeEventChannel;

const packedCursor: EventCursor = "ec1.packed";
const packedKind: EventKind = "future.kind";
const packedEvent: ChangeEventEnvelopeV1 = {
  version: 1,
  cursor: packedCursor,
  occurred_at: "2026-01-01T00:00:00Z",
  vault: "packed",
  kind: packedKind,
  payload: {},
};
const packedCheckpoint: TailCheckpointV1 = { version: 1, cursor: packedCursor };
const packedGap: AkbEventGapDetails = {
  earliest_cursor: "ec1.earliest",
  latest_cursor: "ec1.latest",
};
const packedSubscription = mainChannel
  .on("change", { kinds: [packedKind] }, (event) => {
    event satisfies ChangeEventEnvelopeV1;
    // @ts-expect-error Delivery infrastructure is not part of the public envelope.
    event.stream_id;
  })
  .on("checkpoint", (checkpoint) => {
    checkpoint satisfies TailCheckpointV1;
  })
  .subscribe({ cursor: packedCursor });
packedSubscription.then((subscription) => subscription.cursor satisfies EventCursor | null);

// @ts-expect-error The Tail only accepts the retained replay sentinel.
mainChannel.subscribe({ start: "current" });
// @ts-expect-error Only change and checkpoint are public stream event names.
mainChannel.on("message", () => undefined);
// @ts-expect-error Event Kind remains an open string contract, not a number.
mainChannel.on("change", { kinds: [123] }, () => undefined);

void packedEvent;
void packedCheckpoint;
void packedGap;

main.graph.link satisfies typeof lite.graph.link;
main.activity.list satisfies typeof lite.activity.list;
main.docs.history satisfies typeof lite.docs.history;
main.docs.createCollection satisfies typeof lite.docs.createCollection;
main.tables.migrate satisfies typeof lite.tables.migrate;

const admin = createControlPlaneAdminClient({
  baseUrl: "https://packed.invalid/api/v1",
  adminToken: "admin-token",
});
const app = createControlPlaneAppClient({
  baseUrl: "https://packed.invalid/api/v1",
  appToken: "app-token",
});
admin.apps.get("app-id");
app.rollouts.get("rollout-id");
exchangeAppCredential({
  baseUrl: "https://packed.invalid/api/v1",
  credential: "deployment-credential",
});
