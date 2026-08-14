import {
  createClient,
  type AkbOperationResponse,
  type CreateCollectionRequest,
  type LinkRequest,
  type operations,
} from "@akb/client";
import {
  createClient as createLiteClient,
  type AkbClient as LiteClient,
  type CreateCollectionRequest as LiteCreateCollectionRequest,
  type LinkRequest as LiteLinkRequest,
} from "@akb/client/lite";
import {
  createControlPlaneAdminClient,
  createControlPlaneAppClient,
  exchangeAppCredential,
  type ControlPlaneOperations,
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

const main = createClient({
  baseUrl: "https://packed.invalid/api/v1",
  defaultVault: "packed",
});
const lite: LiteClient = createLiteClient({
  baseUrl: "https://packed.invalid/api/v1",
  defaultVault: "packed",
});

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
