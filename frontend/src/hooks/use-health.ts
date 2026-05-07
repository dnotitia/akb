import { useEffect, useState } from "react";

export interface HealthSnapshot {
  external_git?: { total: number; due?: number; retrying?: number; abandoned?: number };
  metadata_backfill?: { pending: number; retrying?: number; abandoned?: number };
  vector_store?: {
    reachable: boolean;
    backfill?: {
      upsert?: { pending: number; retrying?: number; abandoned?: number; indexed?: number };
      delete?: { pending: number; abandoned?: number };
    };
    bm25_vocab_size?: number;
  };
}

const ENDPOINT = "/health";
const DEFAULT_INTERVAL = 15000;

/**
 * /health is a public endpoint (no auth) per backend main.py. Failures must
 * not break the page — consumers should treat `data === null` as "unknown"
 * and simply render nothing rather than an error state.
 */
export function useHealth(enabled: boolean, intervalMs = DEFAULT_INTERVAL) {
  const [data, setData] = useState<HealthSnapshot | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const r = await fetch(ENDPOINT);
        if (!r.ok) throw new Error(`${r.status}`);
        const json = await r.json();
        if (!cancelled) {
          setData(json);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e as Error);
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [enabled, intervalMs]);

  return { data, error };
}
