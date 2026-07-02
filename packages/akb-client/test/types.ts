import {
  createClient,
  createTypedFetch,
  unwrapAkbResponse,
  type AkbResult,
  type AkbSuccessEnvelope,
  type AkbClient,
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

const liteClient = createLiteClient<AkbSchema>("https://akb.test", { apiKey: "service-key" });
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

type InsertRowsResponse = AkbOperationResponse<operations["tablesInsertRows"]>;
const insertRowsNoContent: InsertRowsResponse = null;
