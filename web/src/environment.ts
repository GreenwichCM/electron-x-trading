import { useEffect, useState } from "react";

/** Which ElectronX environment this app is connected to, from the api (TRADING_ENV, ORDER_ENTRY). */
export type Environment = {
  name: "development" | "beta" | "production";
  /** false: record-only. Orders are entered on the ElectronX screen; this app cannot send any. */
  order_entry: boolean;
};

export const LABELS: Record<Environment["name"], string> = {
  development: "Simulator",
  beta: "ElectronX beta",
  production: "ElectronX PRODUCTION",
};

/** Asked once; needs no sign-in, so the login page shows it too. null until the api answers. */
export function useEnvironment(): Environment | null {
  const [env, setEnv] = useState<Environment | null>(null);
  useEffect(() => {
    fetch("/api/environment")
      .then((res) => (res.ok ? (res.json() as Promise<Environment>) : null))
      .then((e) => {
        if (!e) return;
        setEnv(e);
        document.title = `${LABELS[e.name]} · ElectronX trading`;
      })
      .catch(() => undefined);
  }, []);
  return env;
}
