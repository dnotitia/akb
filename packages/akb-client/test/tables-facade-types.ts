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
} from "../src/index.js";
import type {
  AkbTableMigrationOptions as LiteMigrationOptions,
  AkbTablesFacade as LiteTablesFacade,
} from "../src/lite.js";

const client = createClient({ baseUrl: "https://akb.test/api/v1", defaultVault: "eng" });
client.tables satisfies AkbTablesFacade;
client.tables satisfies LiteTablesFacade;

const createInput: CreateTableRequest = {
  name: "incidents",
  columns: [{ name: "state", type: "text" }],
};
const alterInput: AlterTableRequest = {
  alter_columns: [{ name: "state", set_default: "todo" }],
};
const operations = [
  { op: "add_column", table: "incidents", name: "priority", type: "text" },
  {
    op: "add_index",
    table_name: "incidents",
    index: { columns: [{ name: "priority", order: "desc" }] },
  },
] as const satisfies readonly TableMigrationOperation[];
const options: AkbTableMigrationOptions = { idempotencyKey: "migration-key" };
options satisfies LiteMigrationOptions;

const listed: AkbTableEnvelope = (await client.tables.list()).throwOnError().data;
listed.kind satisfies "table";
const vaultSchema: AkbVaultTableSchemaEnvelope = (
  await client.tables.schema()
).throwOnError().data;
vaultSchema.kind satisfies "vault_table_schema";
const tableSchema: AkbTableSchemaEnvelope = (
  await client.tables.schema("incidents")
).throwOnError().data;
tableSchema.kind satisfies "table_schema";
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
created.kind satisfies "table";
altered.kind satisfies "table";
migrated.kind satisfies "table_migration";
dropped.kind satisfies "table";

// @ts-expect-error idempotencyKey is required.
await client.tables.migrate(operations, {});
// @ts-expect-error operations must be generated migration operations.
await client.tables.migrate([{ op: "raw_sql", sql: "SELECT 1" }], options);
// @ts-expect-error create input requires generated column definitions.
await client.tables.create({ name: "bad", columns: [{ type: "text" }] });
// @ts-expect-error alter input uses generated snake_case fields.
await client.tables.alter("bad", { addColumns: [] });
