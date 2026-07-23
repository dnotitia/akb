import {
  createClient,
  createTypedFetch,
  unwrapAkbResponse,
  type AkbResult,
  type AkbSuccessEnvelope,
  type AkbClient,
  type AkbActivityEnvelope,
  type AkbActivityListOptions,
  type AkbActivityRecentOptions,
  type AkbCollectionCreateEnvelope,
  type AkbCollectionDeleteEnvelope,
  type AkbCollectionSummary,
  type AkbCreateCollectionInput,
  type AkbDeleteCollectionOptions,
  type AkbDocumentEnvelope,
  type AkbDocumentDiffEnvelope,
  type AkbDocumentDiffOptions,
  type AkbDocumentHistoryEnvelope,
  type AkbDocumentHistoryOptions,
  type AkbDocumentPutInput,
  type AkbDocumentWriteEnvelope,
  type AkbGraphEnvelope,
  type AkbGraphHealthEnvelope,
  type AkbGraphNeighborsEnvelope,
  type AkbGraphOverviewEnvelope,
  type AkbGraphRelationsOptions,
  type AkbProvenanceEnvelope,
  type AkbRelation,
  type AkbRelationLinkEnvelope,
  type AkbRelationType,
  type AkbRelationUnlinkEnvelope,
  type AkbRelationsEnvelope,
  type AkbWritableRelationType,
  type AkbOperationResponse,
  type AkbStorageUploadOptions,
  type operations,
} from "../src/index.js";
import { createClient as createLiteClient } from "../src/lite.js";
import type { AkbSchema } from "./fixtures/akb.types.ts";

type TableQueryEnvelope = {
  kind: "table_query";
  columns: string[];
  items: Array<{ id: string }>;
  total: number;
};

const result: AkbResult<TableQueryEnvelope> = unwrapAkbResponse<TableQueryEnvelope>(
  { ok: true, status: 200, statusText: "OK" },
  { kind: "table_query", columns: ["id"], items: [{ id: "r1" }], total: 1 },
);

if (result.error) {
  result.error.code.toUpperCase();
} else {
  result.data?.items.at(0)?.id.toUpperCase();
}

const checked = result.throwOnError();
checked.data.items.at(0)?.id.toUpperCase();

const client = createClient({ baseUrl: "https://akb.test/api/v1" });
const requestResult = await client.request<TableQueryEnvelope>("/tables/reef/sql", {
  method: "POST",
  body: JSON.stringify({ sql: "SELECT id FROM incidents" }),
});
requestResult.throwOnError().data.kind satisfies "table_query";
const docEnvelope: AkbDocumentEnvelope = { kind: "document", uri: "akb://eng/doc/readme.md" };
const docWriteEnvelope: AkbDocumentWriteEnvelope = {
  kind: "document_write",
  uri: "akb://eng/doc/readme.md",
  vault: "eng",
  path: "readme.md",
  commit_hash: "abc1234",
  chunks_indexed: 1,
  entities_found: 0,
};
docEnvelope.kind satisfies "document";
docWriteEnvelope.kind satisfies "document_write";
await client.vault("eng").from("tasks").select("id");

const typedClient = createClient<AkbSchema>({ baseUrl: "https://akb.test/api/v1" });
typedClient satisfies AkbClient<AkbSchema>;
typedClient.vault("eng").actingAs({ sub: "u1", app_metadata: { org_id: "o1", role: "member" } });
// @ts-expect-error org_id and role are required by the BFF X-Akb-Claims parser.
typedClient.vault("eng").actingAs({ sub: "u1", app_metadata: {} });
const rawSqlResult = await typedClient.vault("eng").sql<{ title: string }>`SELECT title FROM tasks WHERE status = ${"todo"}`;
const rawSqlData = rawSqlResult.throwOnError().data;
if (rawSqlData.kind === "table_query") {
  rawSqlData.items.at(0)?.title satisfies string | undefined;
}
const searchResult = await typedClient.vault("eng").search("postgres", {
  rerank: false,
  tags: ["sdk"],
  limit: 3,
});
searchResult.throwOnError().data.kind satisfies "search";
searchResult.throwOnError().data.results.at(0)?.uri satisfies string | undefined;
const graphNeighbors = await typedClient.graph.neighbors("akb://eng/doc/readme.md", { hops: 5, limit: 17 });
graphNeighbors.throwOnError().data.kind satisfies "graph_neighbors";
graphNeighbors.throwOnError().data.nodes.at(0)?.resource_type satisfies "doc" | "table" | "file" | undefined;
graphNeighbors.throwOnError().data.edges.at(0)?.relation satisfies "depends_on" | "related_to" | "implements" | "references" | "attached_to" | "derived_from" | "links_to" | undefined;
const graphOverview = await typedClient.vault("eng").graph.overview({ topK: 37 });
graphOverview.throwOnError().data.kind satisfies "graph_overview";
graphOverview.throwOnError().data.nodes_total satisfies number;
const graphHealth = await typedClient.vault("eng").graph.health({ hubThreshold: 7, limit: 11 });
graphHealth.throwOnError().data.kind satisfies "graph_health";
graphHealth.throwOnError().data.orphans.sample.at(0)?.degree satisfies number | null | undefined;
// @ts-expect-error graph hops are constrained to the backend-supported 1..5 range.
typedClient.graph.neighbors("akb://eng/doc/readme.md", { hops: 0 });
// @ts-expect-error graph hops are constrained to the backend-supported 1..5 range.
typedClient.graph.neighbors("akb://eng/doc/readme.md", { hops: 6 });
// @ts-expect-error graph traversal uses hops, not browse depth.
typedClient.graph.neighbors("akb://eng/doc/readme.md", { depth: 2 });
const graphLeaf: AkbGraphNeighborsEnvelope = graphNeighbors.throwOnError().data;
const overviewLeaf: AkbGraphOverviewEnvelope = graphOverview.throwOnError().data;
const healthLeaf: AkbGraphHealthEnvelope = graphHealth.throwOnError().data;
const rawGraph = await typedClient.graph.request<AkbGraphEnvelope>("?vault=eng");
const rawGraphData = rawGraph.throwOnError().data;
if (rawGraphData.kind === "graph_neighbors") {
  rawGraphData.nodes satisfies AkbGraphNeighborsEnvelope["nodes"];
} else {
  rawGraphData.nodes_total satisfies number;
}
graphLeaf.kind satisfies "graph_neighbors";
overviewLeaf.kind satisfies "graph_overview";
healthLeaf.kind satisfies "graph_health";
const relationFilter: AkbRelationType = "links_to";
const writableRelation: AkbWritableRelationType = "derived_from";
const relationOptions: AkbGraphRelationsOptions = { direction: "incoming", type: relationFilter };
const graphRelations = await typedClient.graph.relations("akb://eng/doc/readme.md", relationOptions);
const relationsLeaf: AkbRelationsEnvelope = graphRelations.throwOnError().data;
relationsLeaf.kind satisfies "relations";
const relationRow: AkbRelation | undefined = relationsLeaf.relations.at(0);
relationRow?.direction satisfies "incoming" | "outgoing" | undefined;
relationRow?.relation satisfies AkbRelationType | undefined;
relationRow?.resource_type satisfies "doc" | "table" | "file" | undefined;
relationRow?.name satisfies string | null | undefined;
const graphLink = await typedClient.graph.link({
  source: "akb://eng/doc/readme.md",
  target: "akb://eng/table/tasks",
  relation: writableRelation,
  metadata: { confidence: 1 },
});
const linkLeaf: AkbRelationLinkEnvelope = graphLink.throwOnError().data;
linkLeaf.kind satisfies "relation_link";
linkLeaf.relation satisfies AkbWritableRelationType;
const graphUnlink = await typedClient.graph.unlink({ source: "akb://eng/doc/readme.md", target: "akb://eng/table/tasks" });
const unlinkLeaf: AkbRelationUnlinkEnvelope = graphUnlink.throwOnError().data;
unlinkLeaf.kind satisfies "relation_unlink";
unlinkLeaf.unlinked satisfies number;
const graphProvenance = await typedClient.graph.provenance("akb://eng/doc/readme.md");
const provenanceLeaf: AkbProvenanceEnvelope = graphProvenance.throwOnError().data;
provenanceLeaf.kind satisfies "provenance";
provenanceLeaf.created_by satisfies string | null;
provenanceLeaf.created_at satisfies string | null;
provenanceLeaf.updated_at satisfies string | null;
provenanceLeaf.current_commit satisfies string | null;
provenanceLeaf.relations satisfies AkbRelation[];
// @ts-expect-error links_to is implicit/read-only and cannot be created explicitly.
typedClient.graph.link({ source: "akb://eng/doc/a", target: "akb://eng/doc/b", relation: "links_to" });
// @ts-expect-error links_to cannot be used for a named unlink; omit relation to remove all matching edges.
typedClient.graph.unlink({ source: "akb://eng/doc/a", target: "akb://eng/doc/b", relation: "links_to" });
const drillDownResult = await typedClient.vault("eng").search.drillDown("akb://eng/doc/readme.md", {
  section: "Intro",
});
drillDownResult.throwOnError().data.kind satisfies "drill_down";
drillDownResult.throwOnError().data.sections.at(0)?.content satisfies string | null | undefined;
const grepResult = await typedClient.vault("eng").search.grep("needle", {
  regex: true,
  filesWithMatches: true,
});
grepResult.throwOnError().data.kind satisfies "grep";
grepResult.throwOnError().data.files?.at(0) satisfies string | undefined;
const docs = typedClient.vault("eng").docs;
const createCollectionInput: AkbCreateCollectionInput = { path: "guides/api", summary: null };
const deleteCollectionOptions: AkbDeleteCollectionOptions = { recursive: true };
const createdCollection = await docs.createCollection(createCollectionInput);
const collectionCreateLeaf: AkbCollectionCreateEnvelope = createdCollection.throwOnError().data;
collectionCreateLeaf.kind satisfies "collection_create";
collectionCreateLeaf.created satisfies boolean;
const collectionSummary: AkbCollectionSummary = collectionCreateLeaf.collection;
collectionSummary.summary satisfies string | null;
collectionSummary.doc_count satisfies number;
const deletedCollection = await docs.deleteCollection("guides/api", deleteCollectionOptions);
const collectionDeleteLeaf: AkbCollectionDeleteEnvelope = deletedCollection.throwOnError().data;
collectionDeleteLeaf.kind satisfies "collection_delete";
collectionDeleteLeaf.deleted_docs satisfies number;
collectionDeleteLeaf.deleted_files satisfies number;
collectionDeleteLeaf.deleted_sub_collections satisfies number;
collectionDeleteLeaf.deleted_tables satisfies number;
type CollectionCreateOperation = AkbOperationResponse<operations["collectionsCreateCollection"]>;
type CollectionDeleteOperation = AkbOperationResponse<operations["collectionsDeleteCollection"]>;
collectionCreateLeaf satisfies CollectionCreateOperation;
collectionDeleteLeaf satisfies CollectionDeleteOperation;
declare const collectionSuccess: import("../src/core/schema.gen.js").AkbSuccessEnvelope;
if (collectionSuccess.kind === "collection_create") {
  collectionSuccess.collection.doc_count satisfies number;
} else if (collectionSuccess.kind === "collection_delete") {
  collectionSuccess.deleted_tables satisfies number;
}
// @ts-expect-error collection path is required.
docs.createCollection({ summary: "missing path" });
// @ts-expect-error recursive is boolean when provided.
docs.deleteCollection("guides/api", { recursive: "true" });
const documentPutInput: AkbDocumentPutInput = {
  collection: "guides",
  title: "Readme",
  content: "# Readme",
  status: "active",
  tags: ["sdk"],
  dependsOn: ["AKB-090"],
  relatedTo: ["AKB-095"],
};
const browsedDocs = await docs.browse({ collection: "guides", depth: 0, includeHashes: true });
browsedDocs.throwOnError().data.kind satisfies "document";
const fetchedDoc = await docs.get("guides/readme.md", { version: "abc1234" });
fetchedDoc.throwOnError().data.content satisfies string | null | undefined;
const putDoc = await docs.put(documentPutInput);
putDoc.throwOnError().data.kind satisfies "document_write";
const updatedDoc = await docs.update("guides/readme.md", {
  summary: "Fresh summary",
  expectedCommit: "abc1234",
  expectedContentHash: "hash1234",
});
updatedDoc.throwOnError().data.current_commit satisfies string | null | undefined;
const deletedDoc = await docs.delete("guides/readme.md");
deletedDoc.throwOnError().data.deleted satisfies boolean | undefined;
const historyOptions: AkbDocumentHistoryOptions = { vault: "eng", limit: 20 };
const historyResult = await docs.history("guides/readme.md", historyOptions);
const historyLeaf: AkbDocumentHistoryEnvelope = historyResult.throwOnError().data;
historyLeaf.kind satisfies "document_history";
historyLeaf.uri satisfies string;
historyLeaf.history.at(0)?.author_name satisfies string | null | undefined;
const diffOptions: AkbDocumentDiffOptions = { commit: "abc1234" };
const diffResult = await docs.diff("guides/readme.md", diffOptions);
const diffLeaf: AkbDocumentDiffEnvelope = diffResult.throwOnError().data;
diffLeaf.kind satisfies "document_diff";
diffLeaf.type satisfies "added" | "deleted" | "modified" | "unknown" | "unchanged";
diffLeaf.error satisfies string | null | undefined;
const activityOptions: AkbActivityListOptions = { collection: null, author: null, since: null, limit: 20 };
const activityResult = await typedClient.vault("eng").activity.list(activityOptions);
const activityLeaf: AkbActivityEnvelope = activityResult.throwOnError().data;
activityLeaf.kind satisfies "activity";
activityLeaf.activity.at(0)?.files.at(0)?.change satisfies "added" | "deleted" | "modified" | undefined;
activityLeaf.activity.at(0)?.author_name satisfies string | null | undefined;
const recentOptions: AkbActivityRecentOptions = { vault: "eng", limit: 10 };
const recentResult = await typedClient.activity.recent(recentOptions);
recentResult.throwOnError().data.kind satisfies "recent_changes";
recentResult.throwOnError().data.changes.at(0)?.commit satisfies string | null | undefined;
recentResult.throwOnError().data.changes.at(0)?.changed_at satisfies string | null | undefined;
type HistoryOperation = AkbOperationResponse<operations["documentsHistory"]>;
type DiffOperation = AkbOperationResponse<operations["documentsDiff"]>;
type ActivityOperation = AkbOperationResponse<operations["activityList"]>;
type RecentOperation = AkbOperationResponse<operations["activityRecent"]>;
historyLeaf satisfies HistoryOperation;
diffLeaf satisfies DiffOperation;
activityLeaf satisfies ActivityOperation;
recentResult.throwOnError().data satisfies RecentOperation;
declare const activitySuccess: AkbSuccessEnvelope;
if (activitySuccess.kind === "document_history") {
  activitySuccess.history.at(0)?.message satisfies string | undefined;
} else if (activitySuccess.kind === "document_diff") {
  activitySuccess.diff satisfies string;
} else if (activitySuccess.kind === "activity") {
  activitySuccess.total satisfies number;
} else if (activitySuccess.kind === "recent_changes") {
  activitySuccess.changes.at(0)?.doc_id satisfies string | undefined;
}
// @ts-expect-error diff requires a commit.
docs.diff("guides/readme.md", {});
// @ts-expect-error history uses limit, not topK.
docs.history("guides/readme.md", { topK: 20 });
// @ts-expect-error activity list does not accept a recent-only option alias.
typedClient.activity.list({ topK: 20 });
// @ts-expect-error recent does not accept activity filters.
typedClient.activity.recent({ author: "u1" });
// @ts-expect-error collection is required when putting a document.
docs.put({ title: "Missing collection", content: "# Missing collection" });
// @ts-expect-error document status is constrained to AKB document lifecycle values.
docs.put({ collection: "guides", title: "Bad status", content: "# Bad status", status: "done" });
const storage = typedClient.vault("eng").storage;
const storageUploadOptions: AkbStorageUploadOptions = {
  description: "Logo",
  contentHash: "hash1234",
  hashAlgorithm: "sha256",
};
const uploadedFile = await storage.upload("media/logo.txt", new Blob(["hello"], { type: "text/plain" }), storageUploadOptions);
uploadedFile.throwOnError().data.kind satisfies "file";
const presignedFile = await storage.presignUpload("media/logo.txt", { mimeType: "text/plain" });
presignedFile.throwOnError().data.upload_url satisfies string | undefined;
const confirmedFile = await storage.confirm("11111111-1111-4111-8111-111111111111", { contentHash: "hash1234" });
confirmedFile.throwOnError().data.size_bytes satisfies number | undefined;
const downloadFile = await storage.download("media/logo.txt");
downloadFile.throwOnError().data.download_url satisfies string | undefined;
const downloadedBytes = await storage.download("media/logo.txt", { bytes: true });
downloadedBytes.throwOnError().data.kind satisfies "file_download";
downloadedBytes.throwOnError().data.bytes.byteLength satisfies number;
const listedFiles = await storage.list({ collection: "media", limit: 20 });
listedFiles.throwOnError().data.items?.at(0)?.name satisfies import("../src/index.js").AkbJsonValue | undefined;
const deletedFile = await storage.delete("media/logo.txt");
deletedFile.throwOnError().data.deleted satisfies boolean | undefined;
// @ts-expect-error upload body is required.
storage.upload("media/logo.txt");
// @ts-expect-error raw SQL is only exposed as a tagged template.
typedClient.vault("eng").sql("SELECT title FROM tasks");
// @ts-expect-error AkbSchema only contains the tasks table fixture.
typedClient.vault("eng").from("incidents").request("/rows");
typedClient.vault("eng").from("tasks").request("/rows");
typedClient.docs.request("/eng/readme.md");
const builderResult = await typedClient.vault("eng").from("tasks").select("title").eq("status", "todo");
builderResult.throwOnError().data.items.at(0)?.title satisfies string | undefined;
// @ts-expect-error metadata was not selected.
builderResult.throwOnError().data.items.at(0)?.metadata;
const selectedColumns = await typedClient.vault("eng").from("tasks").select("title,status");
selectedColumns.throwOnError().data.items.at(0)?.status satisfies "todo" | "done" | undefined;
// @ts-expect-error unknown columns are rejected in select strings.
typedClient.vault("eng").from("tasks").select("titel");
const jsonPathResult = await typedClient.vault("eng").from("tasks").select("metadata->>tier");
jsonPathResult.throwOnError().data.items.at(0)?.["metadata->>tier"] satisfies string | null | undefined;
const jsonPathListResult = await typedClient.vault("eng").from("tasks").select("metadata#>>{stats,count}");
jsonPathListResult.throwOnError().data.items.at(0)?.["metadata#>>{stats,count}"] satisfies string | null | undefined;
const jsonPathCastResult = await typedClient.vault("eng").from("tasks").select("metadata#>>{stats,count}::int");
jsonPathCastResult.throwOnError().data.items.at(0)?.["metadata#>>{stats,count}::int"] satisfies string | null | undefined;
const mixedJsonPathCast = await typedClient.vault("eng").from("tasks").select("title,metadata->>tier::text");
mixedJsonPathCast.throwOnError().data.items.at(0)?.title satisfies string | undefined;
mixedJsonPathCast.throwOnError().data.items.at(0)?.["metadata->>tier::text"] satisfies string | null | undefined;
const mixedJsonPathList = await typedClient.vault("eng").from("tasks").select("title,metadata#>>{stats,count}");
mixedJsonPathList.throwOnError().data.items.at(0)?.["metadata#>>{stats,count}"] satisfies string | null | undefined;
// @ts-expect-error casts must not turn the whole select string into an alias-shaped wide boundary.
typedClient.vault("eng").from("tasks").select("titel,metadata->>tier::text");
// @ts-expect-error a leading JSON path must not hide sibling column typos.
typedClient.vault("eng").from("tasks").select("metadata->>tier,titel");
const starAndJsonPath = await typedClient.vault("eng").from("tasks").select("*,metadata->>tier");
starAndJsonPath.throwOnError().data.items.at(0)?.metadata satisfies import("./fixtures/akb.types.ts").Json | null | undefined;
starAndJsonPath.throwOnError().data.items.at(0)?.["metadata->>tier"] satisfies string | null | undefined;
const wideJoinBoundary = await typedClient.vault("eng").from("tasks").select("owner:users(id)");
wideJoinBoundary.throwOnError().data.items.at(0)?.metadata satisfies import("./fixtures/akb.types.ts").Json | null | undefined;
const wideJoinWithInnerComma = await typedClient.vault("eng").from("tasks").select("owner:users(id,name)");
wideJoinWithInnerComma.throwOnError().data.items.at(0)?.metadata satisfies import("./fixtures/akb.types.ts").Json | null | undefined;
// @ts-expect-error wide join tokens must not hide sibling column typos.
typedClient.vault("eng").from("tasks").select("titel,owner:users(id)");
const dynamicColumns: string = "title,status";
const dynamicSelect = await typedClient.vault("eng").from("tasks").select(dynamicColumns);
dynamicSelect.throwOnError().data.items.at(0)?.metadata satisfies import("./fixtures/akb.types.ts").Json | null | undefined;
const writeResult = await typedClient
  .vault("eng")
  .from("tasks")
  .insert({ title: "Ship" });
writeResult.throwOnError().data?.items.at(0)?.title satisfies string | undefined;
await typedClient.vault("eng").from("tasks").select("title").insert({ title: "Draft" });
const singleWriteResult = await typedClient
  .vault("eng")
  .from("tasks")
  .upsert({ title: "Ship" }, { onConflict: "title" })
  .select("title")
  .single();
singleWriteResult.throwOnError().data.title satisfies string;
// @ts-expect-error status was not selected before single().
singleWriteResult.throwOnError().data.status;
const maybeDeleted = await typedClient.vault("eng").from("tasks").delete().eq("status", "done").select("*").maybeSingle();
maybeDeleted.throwOnError().data?.status satisfies "todo" | "done" | undefined;
// @ts-expect-error generated Update restricts status to the table enum.
typedClient.vault("eng").from("tasks").update({ status: "blocked" });

const liteClient = createLiteClient<AkbSchema>("https://akb.test", { apiKey: "service-key" }); // pragma: allowlist secret — test fixture, not a real key
liteClient satisfies AkbClient<AkbSchema>;

const typedFetch = createTypedFetch(typedClient);
const schemaResult = await typedFetch("get", "/api/v1/tables/{vault}/schema", {
  path: { vault: "eng" },
});
schemaResult.throwOnError().data.kind satisfies "vault_table_schema";
// @ts-expect-error generated path params are required.
await typedFetch("get", "/api/v1/tables/{vault}/schema");

type VaultSchemaResponse = AkbOperationResponse<operations["tablesGetVaultSchema"]>;
const vaultSchema: VaultSchemaResponse = {
  kind: "vault_table_schema",
  vault: "eng",
  tables: [],
  total: 0,
};
vaultSchema.kind satisfies "vault_table_schema";

type SearchResponse = AkbOperationResponse<operations["searchSearchDocuments"]>;
const searchEnvelope: SearchResponse = {
  kind: "search",
  query: "postgres",
  total: 1,
  returned: 1,
  total_matches: 1,
  results: [{ source_type: "document", uri: "akb://eng/doc/readme.md", vault: "eng", path: "readme.md", title: "Readme", tags: [], score: 1 }],
};
searchEnvelope.kind satisfies "search";

type RawGraphResponse = AkbOperationResponse<operations["graphNeighbors"]>;
const rawGraphResponse: RawGraphResponse = {
  kind: "graph_neighbors",
  nodes: [{ uri: "akb://eng/doc/readme.md", name: "Readme", resource_type: "doc", depth: 0 }],
  edges: [],
};
rawGraphResponse.kind satisfies "graph_neighbors" | "graph_overview";
type GraphOverviewResponse = AkbOperationResponse<operations["graphOverview"]>;
const graphOverviewResponse: GraphOverviewResponse = overviewLeaf;
graphOverviewResponse.kind satisfies "graph_overview";
type GraphHealthResponse = AkbOperationResponse<operations["graphHealth"]>;
const graphHealthResponse: GraphHealthResponse = healthLeaf;
graphHealthResponse.kind satisfies "graph_health";
type GraphRelationsResponse = AkbOperationResponse<operations["graphRelations"]>;
const graphRelationsResponse: GraphRelationsResponse = relationsLeaf;
graphRelationsResponse.kind satisfies "relations";
type GraphLinkResponse = AkbOperationResponse<operations["graphLink"]>;
const graphLinkResponse: GraphLinkResponse = linkLeaf;
graphLinkResponse.kind satisfies "relation_link";
type GraphUnlinkResponse = AkbOperationResponse<operations["graphUnlink"]>;
const graphUnlinkResponse: GraphUnlinkResponse = unlinkLeaf;
graphUnlinkResponse.kind satisfies "relation_unlink";
type GraphProvenanceResponse = AkbOperationResponse<operations["graphProvenance"]>;
const graphProvenanceResponse: GraphProvenanceResponse = provenanceLeaf;
graphProvenanceResponse.kind satisfies "provenance";

type InsertRowsResponse = AkbOperationResponse<operations["tablesInsertRows"]>;
const insertRowsNoContent: InsertRowsResponse = null;
