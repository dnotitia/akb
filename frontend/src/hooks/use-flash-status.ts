import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Transient status message that auto-clears after `durationMs`.
 * For one-off success notices ("Saved", "Copied") that should not linger.
 * Errors usually want to be persistent — use plain state for those.
 */
export function useFlashStatus(durationMs = 3000) {
  const [message, setMessage] = useState("");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const setFlash = useCallback(
    (next: string) => {
      if (timerRef.current) clearTimeout(timerRef.current);
      setMessage(next);
      timerRef.current = setTimeout(() => setMessage(""), durationMs);
    },
    [durationMs],
  );

  const clear = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setMessage("");
  }, []);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  return { message, setFlash, clear };
}
