/**
 * Value formatting shared by the URL and AST serializers.
 *
 * These helpers only translate individual filter values into their wire form;
 * they hold no query state and depend on nothing else in the client.
 */

export function formatFilterValue(operator: string, value: unknown): string {
  const op = baseOperator(operator);
  if (Array.isArray(value)) {
    const inner = value.map(formatScalar).join(",");
    return op === "cs" ? `{${inner}}` : `(${inner})`;
  }
  if (value && typeof value === "object") return JSON.stringify(value);
  return formatScalar(value);
}

export function formatAstValue(operator: string, value: unknown): unknown {
  const op = baseOperator(operator);
  if ((op === "like" || op === "ilike") && typeof value === "string") {
    return value.replaceAll("*", "%");
  }
  return value;
}

export function baseOperator(operator: string): string {
  return operator.startsWith("not.") ? operator.slice(4) : operator;
}

function formatScalar(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  return String(value);
}
