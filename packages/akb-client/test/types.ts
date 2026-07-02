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

const typedClient = createClient<AkbSchema>({ baseUrl: "https://akb.test/api/v1" });
typedClient satisfies AkbClient<AkbSchema>;
typedClient.vault("eng").actingAs({ sub: "u1", app_metadata: { org_id: "o1", role: "member" } });
// @ts-expect-error org_id and role are required by the BFF X-Akb-Claims parser.
typedClient.vault("eng").actingAs({ sub: "u1", app_metadata: {} });
// @ts-expect-error AkbSchema only contains the tasks table fixture.
typedClient.vault("eng").from("incidents").request("/rows");
typedClient.vault("eng").from("tasks").request("/rows");
typedClient.docs.request("/eng/readme.md");

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
