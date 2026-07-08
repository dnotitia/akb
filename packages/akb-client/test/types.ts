import {
  createClient,
  createTypedFetch,
  unwrapAkbResponse,
  type AkbResult,
  type AkbSuccessEnvelope,
  type AkbClient,
  type AkbDocumentEnvelope,
  type AkbDocumentPutInput,
  type AkbDocumentWriteEnvelope,
  type AkbOperationResponse,
  type operations,
} from "../src/index.js";
import { createClient as createLiteClient } from "../src/lite.js";
import type { AkbSchema } from "./fixtures/akb.types.ts";

type TableQueryEnvelope = AkbSuccessEnvelope & {
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
// @ts-expect-error collection is required when putting a document.
docs.put({ title: "Missing collection", content: "# Missing collection" });
// @ts-expect-error document status is constrained to AKB document lifecycle values.
docs.put({ collection: "guides", title: "Bad status", content: "# Bad status", status: "done" });
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

type InsertRowsResponse = AkbOperationResponse<operations["tablesInsertRows"]>;
const insertRowsNoContent: InsertRowsResponse = null;
