import {
  createClient,
  type AkbTableEnvelope,
  type AkbTableMigrationEnvelope,
  type AkbTableMigrationOptions,
  type AkbTableSchemaEnvelope,
  type AkbTablesFacade,
  type AkbVaultTableSchemaEnvelope,
  type AlterTableRequest,
  type CreateTableRequest,
  type TableMigrationOperation,
} from "@akb/client";
import type {
  AkbTableMigrationOptions as LiteMigrationOptions,
  AkbTablesFacade as LiteTablesFacade,
} from "@akb/client/lite";

const client = createClient({ baseUrl: "https://public.example/api/v1", defaultVault: "eng" });
client.tables satisfies AkbTablesFacade;
client.tables satisfies LiteTablesFacade;

const createInput: CreateTableRequest = {
  name: "incidents",
  columns: [{ name: "state", type: "text" }],
};
const alterInput: AlterTableRequest = {
  add_columns: [{ name: "owner", type: "text" }],
};
const operations = [
  { op: "add_column", table: "incidents", name: "priority", type: "text" },
  { op: "drop_index", table: "incidents", name: "old_idx" },
] as const satisfies readonly TableMigrationOperation[];
const options: AkbTableMigrationOptions = { idempotencyKey: "artifact-key" };
options satisfies LiteMigrationOptions;

const listed: AkbTableEnvelope = (await client.tables.list()).throwOnError().data;
const vaultSchema: AkbVaultTableSchemaEnvelope = (
  await client.tables.schema()
).throwOnError().data;
const tableSchema: AkbTableSchemaEnvelope = (
  await client.tables.schema("incidents")
).throwOnError().data;
const created: AkbTableEnvelope = (
  await client.tables.create(createInput)
).throwOnError().data;
const altered: AkbTableEnvelope = (
  await client.tables.alter("incidents", alterInput)
).throwOnError().data;
const migrated: AkbTableMigrationEnvelope = (
  await client.tables.migrate(operations, options)
).throwOnError().data;
const dropped: AkbTableEnvelope = (
  await client.tables.drop("incidents")
).throwOnError().data;
listed.kind satisfies "table";
vaultSchema.kind satisfies "vault_table_schema";
tableSchema.kind satisfies "table_schema";
created.kind satisfies "table";
altered.kind satisfies "table";
migrated.kind satisfies "table_migration";
dropped.kind satisfies "table";

// @ts-expect-error idempotencyKey is required.
await client.tables.migrate(operations, {});
// @ts-expect-error generated operations reject raw SQL.
await client.tables.migrate([{ op: "raw_sql", sql: "SELECT 1" }], options);
// @ts-expect-error generated create input requires column names.
await client.tables.create({ name: "bad", columns: [{ type: "text" }] });
