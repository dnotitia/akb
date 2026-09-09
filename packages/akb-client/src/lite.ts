export {
  AKB_ERROR_CODES,
  AkbError,
  akbFetch,
  createClient,
  unwrapAkbResponse,
} from "./index.js";

export type {
  ChangeEventEnvelopeV1,
  EventCursor,
  EventKind,
  TailCheckpointV1,
  AkbClaims,
  AkbClient,
  AkbClientConfig,
  AkbClientOptions,
  AkbErrorPayload,
  AkbLocalErrorCode,
  AkbJsonValue,
  AkbNamespaceStub,
  AkbResult,
  AkbSqlTag,
  AkbSuccessEnvelope,
  AkbTableMigrationOptions,
  AkbTableStub,
  AkbTablesFacade,
  AkbThrowingResult,
  AkbVaultSqlResult,
  CreateCollectionRequest,
  LinkRequest,
} from "./index.js";

export type {
  AkbChangeEventChannel,
  AkbChangeEventListener,
  AkbChangeEventListenerOptions,
  AkbChangeEventSubscribeOptions,
  AkbChangeEventSubscription,
  AkbEventGapDetails,
  AkbTailCheckpointListener,
} from "./index.js";
