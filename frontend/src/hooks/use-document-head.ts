import { useCallback, useLayoutEffect, useMemo, useState } from "react";

import { ApiError, getDocumentHead, type DocumentHead } from "@/lib/api";

const POLL_INTERVAL = 15_000;

export interface DocumentHeadOptions {
  vault: string;
  document: string;
  /** The account and access identity that owns this editor. */
  scope: string;
  /** Saving or reloading a new base invalidates observations of the previous base. */
  baseCommit: string | null;
  enabled: boolean;
}

interface HeadFlight {
  controller: AbortController;
  promise: Promise<DocumentHead | null>;
}

interface Generation extends Omit<DocumentHeadOptions, "enabled"> {
  active: boolean;
  flight: HeadFlight | null;
}

interface Observation {
  generation: Generation;
  head: DocumentHead | null;
  checking: boolean;
  error: Error | null;
}

function isTerminalHeadError(error: unknown): error is ApiError {
  return error instanceof ApiError && [401, 403, 404].includes(error.status);
}

/** Observe only the current editor; private observations never enter a shared cache. */
export function useDocumentHead({ vault, document: documentRef, scope, baseCommit, enabled }: DocumentHeadOptions) {
  const generation = useMemo<Generation>(() => ({
    vault,
    document: documentRef,
    scope,
    baseCommit,
    active: false,
    flight: null,
  }), [vault, documentRef, scope, baseCommit]);
  const [observation, setObservation] = useState<Observation | null>(null);

  const check = useCallback((): Promise<DocumentHead | null> => {
    if (!generation.active || document.visibilityState === "hidden") return Promise.resolve(null);
    if (generation.flight) return generation.flight.promise;

    const flight: HeadFlight = {
      controller: new AbortController(),
      promise: Promise.resolve(null),
    };
    generation.flight = flight;
    setObservation(previous => ({
      generation,
      head: previous?.generation === generation ? previous.head : null,
      checking: true,
      error: previous?.generation === generation ? previous.error : null,
    }));
    const isCurrent = () => generation.active && !flight.controller.signal.aborted && generation.flight === flight;
    flight.promise = (async () => {
      try {
        const head = await getDocumentHead(generation.vault, generation.document, flight.controller.signal);
        if (!isCurrent()) return null;
        setObservation({ generation, head, checking: false, error: null });
        return head;
      } catch (caught) {
        if (!isCurrent()) return null;
        const error = caught instanceof Error ? caught : new Error("Unable to check the latest document.");
        const terminal = isTerminalHeadError(error);
        setObservation(previous => ({
          generation,
          // A failed connection does not undo a known remote change. Access
          // loss or deletion does invalidate the last private observation.
          head: !terminal && previous?.generation === generation ? previous.head : null,
          checking: false,
          // A network failure does not prove restored access. Keep the last
          // terminal failure until this generation has an authorized success.
          error: !terminal && previous?.generation === generation && isTerminalHeadError(previous.error)
            ? previous.error : error,
        }));
        return null;
      } finally {
        if (generation.flight === flight) generation.flight = null;
      }
    })();
    return flight.promise;
  }, [generation]);

  useLayoutEffect(() => {
    generation.active = enabled && !!generation.vault && !!generation.document && !!generation.scope;
    setObservation(previous => previous?.generation === generation && previous.checking ? { ...previous, checking: false } : previous);
    if (!generation.active) return;

    let timer: ReturnType<typeof setInterval> | null = null;
    const stopPolling = () => {
      if (timer !== null) clearInterval(timer);
      timer = null;
    };
    const cancelFlight = () => {
      generation.flight?.controller.abort();
      generation.flight = null;
    };
    const resume = () => {
      if (document.visibilityState === "hidden") return;
      void check();
      if (timer === null) timer = setInterval(() => { void check(); }, POLL_INTERVAL);
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        stopPolling();
        cancelFlight();
        setObservation(previous => previous?.generation === generation ? { ...previous, checking: false } : previous);
      } else {
        resume();
      }
    };
    const onOnline = () => {
      if (document.visibilityState !== "hidden") void check();
    };

    resume();
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("online", onOnline);
    return () => {
      generation.active = false;
      stopPolling();
      cancelFlight();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("online", onOnline);
    };
  }, [check, enabled, generation]);

  const current = observation?.generation === generation ? observation : null;
  return {
    head: current?.head ?? null,
    checking: enabled && (current?.checking ?? false),
    error: current?.error ?? null,
    check,
  };
}
