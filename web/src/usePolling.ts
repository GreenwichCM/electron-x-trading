import { useEffect, useState } from "react";
import { api } from "./auth";

/** GET `path` now and every `everyMs`. Returns the latest result, or null until the first one arrives.
 *  `refresh` bumps a counter to fetch again immediately (e.g. right after a change). */
export function usePolling<T>(path: string, everyMs: number, refresh = 0): T | null {
  const [data, setData] = useState<T | null>(null);
  useEffect(() => {
    let stopped = false;
    const load = () =>
      api<T>(path)
        .then((d) => !stopped && setData(d))
        .catch(() => {});
    load();
    const timer = window.setInterval(load, everyMs);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [path, everyMs, refresh]);
  return data;
}
