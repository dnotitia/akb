import {
  createClient,
  type AkbOperationResponse,
  type AkbSuccessEnvelope,
  type CreateCollectionRequest,
  type LinkRequest,
  type operations,
} from "../src/index.js";
import { createClient as createLiteClient } from "../src/lite.js";

type ContractOperationId =
  | "graphNeighbors"
  | "graphOverview"
  | "graphHealth"
  | "graphRelations"
  | "graphLink"
  | "graphUnlink"
  | "graphProvenance"
  | "activityList"
  | "activityRecent"
  | "documentsHistory"
  | "documentsDiff"
  | "collectionsCreateCollection"
  | "collectionsDeleteCollection"
  | "tablesGetVault"
  | "tablesGetVaultSchema"
  | "tablesGetTableSchema"
  | "tablesPostVault"
  | "tablesAlterTable"
  | "tablesApplyMigration"
  | "tablesDeleteTableName";

type Assert<T extends true> = T;
type IsAny<T> = 0 extends (1 & T) ? true : false;
type IsUnknown<T> =
  IsAny<T> extends true
    ? false
    : unknown extends T
      ? [keyof T] extends [never]
        ? true
        : false
      : false;
type IsExactly<Left, Right> =
  (<T>() => T extends Left ? 1 : 2) extends
  (<T>() => T extends Right ? 1 : 2)
    ? (<T>() => T extends Right ? 1 : 2) extends
      (<T>() => T extends Left ? 1 : 2)
      ? true
      : false
    : false;
type OperationRequestBody<Operation> =
  Operation extends { requestBody: infer RequestBody } ? RequestBody : never;
type IsConcreteRequest<Id extends ContractOperationId> =
  IsAny<OperationRequestBody<operations[Id]>> extends true
    ? false
    : IsUnknown<OperationRequestBody<operations[Id]>> extends true
      ? false
      : IsExactly<OperationRequestBody<operations[Id]>, Record<string, unknown>> extends true
        ? false
        : true;
type IsConcreteResponse<Id extends ContractOperationId> =
  IsAny<AkbOperationResponse<operations[Id]>> extends true
    ? false
    : IsUnknown<AkbOperationResponse<operations[Id]>> extends true
      ? false
      : IsExactly<AkbOperationResponse<operations[Id]>, Record<string, unknown>> extends true
        ? false
        : IsExactly<AkbOperationResponse<operations[Id]>, AkbSuccessEnvelope> extends true
          ? false
          : true;
type AllTrue<Value> = Exclude<Value, true> extends never ? true : false;

type _ContractRequestsAreConcrete = Assert<
  AllTrue<{ [Id in ContractOperationId]: IsConcreteRequest<Id> }[ContractOperationId]>
>;
type _ContractResponsesAreConcrete = Assert<
  AllTrue<{ [Id in ContractOperationId]: IsConcreteResponse<Id> }[ContractOperationId]>
>;
type _GraphLinkBodyIsExact = Assert<
  IsExactly<OperationRequestBody<operations["graphLink"]>, LinkRequest>
>;
type _CollectionCreateBodyIsExact = Assert<
  IsExactly<
    OperationRequestBody<operations["collectionsCreateCollection"]>,
    CreateCollectionRequest
  >
>;

const main = createClient({ baseUrl: "https://contract.invalid/api/v1" });
const lite = createLiteClient({ baseUrl: "https://contract.invalid/api/v1" });

main.graph.link satisfies typeof lite.graph.link;
main.docs.createCollection satisfies typeof lite.docs.createCollection;
main.activity.list satisfies typeof lite.activity.list;
main.docs.history satisfies typeof lite.docs.history;
main.tables.migrate satisfies typeof lite.tables.migrate;
