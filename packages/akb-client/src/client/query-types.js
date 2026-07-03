/**
 * Shared JSDoc typedefs for the query builder and its serializers.
 *
 * This module holds no runtime code; it exists so the builder, compiler, and
 * serializer modules can reference the same state shapes without importing one
 * another for types.
 *
 * @typedef {{
 *   baseUrl: string,
 *   table: string,
 *   vault: string | null,
 *   request: import("../index.js").AkbClient["request"],
 *   maxUrlBytes: number,
 * }} QueryBuilderOptions
 *
 * @typedef {{ type: "filter", column: string, operator: string, value: unknown }} FilterNode
 * @typedef {{ type: "group", op: "and" | "or", expression?: string, nodes?: QueryNode[] }} GroupNode
 * @typedef {FilterNode | GroupNode} QueryNode
 * @typedef {{ column: string, ascending: boolean }} OrderNode
 * @typedef {{
 *   type: "insert" | "update" | "upsert" | "delete",
 *   body?: unknown,
 *   onConflict?: string | null,
 *   ignoreDuplicates?: boolean,
 * }} MutationState
 * @typedef {{
 *   select: string | null,
 *   nodes: QueryNode[],
 *   order: OrderNode[],
 *   limit: number | null,
 *   offset: number | null,
 *   range: { from: number, to: number } | null,
 *   count: "exact" | "planned" | "estimated" | null,
 *   mutation: MutationState | null,
 *   all: boolean,
 *   resultMode: "rows" | "single" | "maybeSingle",
 * }} QueryState
 */

export {};
