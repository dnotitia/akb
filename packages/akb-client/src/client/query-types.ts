/**
 * Shared state shapes for the query builder and its serializers.
 *
 * This module holds no runtime code; it exists so the builder, compiler, and
 * serializer modules can reference the same state shapes without importing one
 * another for values.
 */

import type { AkbClient } from "../index.js";

export interface QueryBuilderOptions {
  baseUrl: string;
  table: string;
  vault: string | null;
  request: AkbClient["request"];
  maxUrlBytes: number;
}

export interface FilterNode {
  type: "filter";
  column: string;
  operator: string;
  value: unknown;
}

export interface GroupNode {
  type: "group";
  op: "and" | "or";
  expression?: string;
  nodes?: QueryNode[];
}

export type QueryNode = FilterNode | GroupNode;

export interface OrderNode {
  column: string;
  ascending: boolean;
}

export interface MutationState {
  type: "insert" | "update" | "upsert" | "delete";
  body?: unknown;
  onConflict?: string | null;
  ignoreDuplicates?: boolean;
}

export interface QueryState {
  select: string | null;
  nodes: QueryNode[];
  order: OrderNode[];
  limit: number | null;
  offset: number | null;
  range: { from: number; to: number } | null;
  count: "exact" | "planned" | "estimated" | null;
  mutation: MutationState | null;
  all: boolean;
  resultMode: "rows" | "single" | "maybeSingle";
}
