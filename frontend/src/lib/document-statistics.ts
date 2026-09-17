export function getDocumentStats(content: string) {
  const normalized = content.replace(/\r\n?/g, "\n");
  const withoutTerminalNewline = normalized.endsWith("\n")
    ? normalized.slice(0, -1)
    : normalized;

  return {
    lineCount:
      normalized.length === 0 ? 0 : withoutTerminalNewline.split("\n").length,
    byteCount: new TextEncoder().encode(content).byteLength,
  };
}

export function formatLineCount(count: number) {
  return `${count} ${count === 1 ? "line" : "lines"}`;
}

export function formatByteSize(bytes: number) {
  if (bytes < 1024) return `${bytes} ${bytes === 1 ? "Byte" : "Bytes"}`;
  if (bytes < 1024 * 1024) return `${formatUnit(bytes / 1024)} KB`;
  return `${formatUnit(bytes / (1024 * 1024))} MB`;
}

function formatUnit(value: number) {
  return value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2);
}
