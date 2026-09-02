import { describe, expect, it } from "vitest";
import {
  compileTableFilter,
  parseTableQueryState,
  tableFilterOperators,
  tableOrder,
  validateTableFilter,
  writeTableQueryState,
  type TableQueryState,
} from "@/lib/table-query-state";

describe("table query state", () => {
  it("round-trips page, size, sort, repeated filters, and unrelated params", () => {
    const state: TableQueryState = {
      pageIndex: 2,
      pageSize: 25,
      sort: { column: "severity", direction: "asc" },
      filters: [
        { column: "severity", operator: "eq", value: "critical" },
        { column: "title", operator: "contains", value: "API: gateway" },
      ],
    };
    const params = writeTableQueryState(new URLSearchParams("view=compact"), state);
    expect(parseTableQueryState(params)).toEqual(state);
    expect(params.get("view")).toBe("compact");
    expect(params.get("page")).toBe("3");
  });

  it("falls back safely when URL state is malformed", () => {
    const state = parseTableQueryState(
      new URLSearchParams("page=-2&size=999&sort=bad;drop.desc&f=oops"),
    );
    expect(state).toEqual({ pageIndex: 0, pageSize: 50, sort: null, filters: [] });
  });

  it("compiles user-facing conditions to the row API grammar", () => {
    expect(
      compileTableFilter({ column: "title", operator: "contains", value: "gateway" }),
    ).toEqual({ column: "title", expression: "ilike.*gateway*" });
    expect(
      compileTableFilter({ column: "severity", operator: "in", value: "high, critical" }),
    ).toEqual({ column: "severity", expression: "in.(high,critical)" });
    expect(
      compileTableFilter({
        column: "metadata",
        operator: "json_contains",
        value: '{ "tier": "gold" }',
      }),
    ).toEqual({ column: "metadata", expression: 'cs.{"tier":"gold"}' });
    expect(
      compileTableFilter({ column: "metadata", operator: "json_contains", value: "{" }),
    ).toEqual({ column: "metadata", expression: "cs.{" });
  });

  it("uses a stable id tie-breaker for every sort", () => {
    expect(tableOrder(null)).toBe("created_at.desc,id.desc");
    expect(tableOrder({ column: "severity", direction: "asc" })).toBe(
      "severity.asc,id.asc",
    );
    expect(tableOrder({ column: "id", direction: "desc" })).toBe("id.desc");
  });

  it("offers and validates conditions based on the column type", () => {
    expect(tableFilterOperators({ name: "enabled", type: "boolean" }).map((item) => item.value))
      .toEqual(["is_true", "is_false", "is_null", "not_null"]);
    expect(tableFilterOperators({ name: "metadata", type: "jsonb" }).map((item) => item.value))
      .toEqual(["json_contains", "is_null", "not_null"]);
    expect(
      tableFilterOperators({ name: "severity", type: "text", enum: ["low", "high"] }).map(
        (item) => item.value,
      ),
    ).toEqual(["eq", "neq", "in", "is_null", "not_null"]);
    expect(
      validateTableFilter(
        { column: "score", operator: "gte", value: "not-a-number" },
        { name: "score", type: "numeric" },
      ),
    ).toBe("Enter a valid number.");
    expect(
      validateTableFilter(
        { column: "metadata", operator: "json_contains", value: "{" },
        { name: "metadata", type: "jsonb" },
      ),
    ).toBe("Enter valid JSON.");
  });
});
