import {
  createClient,
  type AkbCollectionCreateEnvelope,
  type AkbCollectionDeleteEnvelope,
  type AkbCollectionSummary,
  type AkbCreateCollectionInput,
  type AkbDeleteCollectionOptions,
  type AkbOperationResponse,
  type AkbSuccessEnvelope,
  type operations,
} from "@akb/client";

const client = createClient({ baseUrl: "https://proof.invalid/api/v1" }).vault("proof");
const createInput: AkbCreateCollectionInput = { path: "guides/api", summary: null };
const deleteOptions: AkbDeleteCollectionOptions = { recursive: true };
const created: AkbCollectionCreateEnvelope = (await client.docs.createCollection(createInput)).throwOnError().data;
const deleted: AkbCollectionDeleteEnvelope = (await client.docs.deleteCollection("guides/api", deleteOptions)).throwOnError().data;
const summary: AkbCollectionSummary = created.collection;
created.kind satisfies "collection_create";
created.created satisfies boolean;
summary.path satisfies string;
summary.summary satisfies string | null;
summary.doc_count satisfies number;
deleted.kind satisfies "collection_delete";
deleted.collection satisfies string;
deleted.deleted_docs satisfies number;
deleted.deleted_files satisfies number;
deleted.deleted_sub_collections satisfies number;
deleted.deleted_tables satisfies number;

type CreateResponse = AkbOperationResponse<operations["collectionsCreateCollection"]>;
type DeleteResponse = AkbOperationResponse<operations["collectionsDeleteCollection"]>;
created satisfies CreateResponse;
deleted satisfies DeleteResponse;

declare const success: AkbSuccessEnvelope;
if (success.kind === "collection_create") {
  success.collection.doc_count satisfies number;
} else if (success.kind === "collection_delete") {
  success.deleted_tables satisfies number;
}

// @ts-expect-error path is required.
client.docs.createCollection({ summary: "missing path" });
// @ts-expect-error recursive is boolean when provided.
client.docs.deleteCollection("guides/api", { recursive: "true" });
