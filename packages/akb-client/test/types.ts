import {
  createClient,
  unwrapAkbResponse,
  type AkbResult,
  type AkbSuccessEnvelope,
} from "../src/index.js";

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
