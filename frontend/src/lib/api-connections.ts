import { authenticatedFetch } from "@/lib/api";

/** One AI app seen using this account over MCP. The server stores no IP and no request content. */
export interface AIConnection {
  id: string;
  client_name: string | null;
  client_version: string | null;
  auth_kind: "oauth" | "pat";
  token_id: string | null;
  token_prefix: string | null;
  token_name: string | null;
  first_seen_at: string;
  last_seen_at: string;
}

/** The server cannot list connections (an older backend, or a non-personal account). */
export class ConnectionsUnavailable extends Error {}

const text = (value: unknown) => value === null || typeof value === "string";
const instant = (value: unknown) => typeof value === "string" && !Number.isNaN(Date.parse(value));

function isConnection(value: unknown): value is AIConnection {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return typeof item.id === "string" && (item.auth_kind === "oauth" || item.auth_kind === "pat") &&
    text(item.client_name) && text(item.client_version) && text(item.token_id) &&
    text(item.token_prefix) && text(item.token_name) && instant(item.first_seen_at) && instant(item.last_seen_at);
}

export async function listConnections(signal?: AbortSignal): Promise<AIConnection[]> {
  const response = await authenticatedFetch("/api/v1/auth/connections", { signal });
  if ([404, 405, 501].includes(response.status)) throw new ConnectionsUnavailable("Connected apps are not available on this server yet.");
  if (response.status === 403) throw new ConnectionsUnavailable("Connected apps are listed for personal accounts only.");
  if (!response.ok) throw new Error("Could not load connected apps. Try again.");
  const body: unknown = await response.json().catch(() => null);
  const connections = body && typeof body === "object" ? (body as { connections?: unknown }).connections : undefined;
  if (!Array.isArray(connections) || !connections.every(isConnection)) throw new Error("Could not load connected apps. Try again.");
  return connections;
}
