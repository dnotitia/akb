import { useEffect, useState } from "react";

/** Returns `value` after it has stayed unchanged for `delay` ms.
 *  Use to gate expensive work (search calls, filters) on typing pauses. */
export function useDebounce<T>(value: T, delay: number = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}
