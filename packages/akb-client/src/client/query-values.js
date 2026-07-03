/**
 * Value formatting shared by the URL and AST serializers.
 *
 * These helpers only translate individual filter values into their wire form;
 * they hold no query state and depend on nothing else in the client.
 */

/**
 * @param {string} operator
 * @param {unknown} value
 * @returns {string}
 */
export function formatFilterValue(operator, value) {
  const op = baseOperator(operator);
  if (Array.isArray(value)) {
    const inner = value.map(formatScalar).join(",");
    return op === "cs" ? `{${inner}}` : `(${inner})`;
  }
  if (value && typeof value === "object") return JSON.stringify(value);
  return formatScalar(value);
}

/**
 * @param {string} operator
 * @param {unknown} value
 * @returns {unknown}
 */
export function formatAstValue(operator, value) {
  const op = baseOperator(operator);
  if ((op === "like" || op === "ilike") && typeof value === "string") {
    return value.replaceAll("*", "%");
  }
  return value;
}

/**
 * @param {string} operator
 * @returns {string}
 */
export function baseOperator(operator) {
  return operator.startsWith("not.") ? operator.slice(4) : operator;
}

/**
 * @param {unknown} value
 * @returns {string}
 */
function formatScalar(value) {
  if (value === null) return "null";
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  return String(value);
}
