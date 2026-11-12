import { createContext, useEffect, useState } from "react";
import { usePolling } from "./usePolling";

export type Hours = {
  open: boolean;
  next_change: string;
  reason: string;
  schedule: string;
  upcoming: { date: string; what: string }[]; // the next holidays and early closes
};

/** Whether ElectronX is open now, by its trading hours. A contract the exchange has given no state (an empty
 *  book carries none) shows CLOSED while this is false. True until the hours are known. */
export const ExchangeOpen = createContext(true);

const ROUTINE = ["open", "before the open", "after the close", "weekend"];

/** "4h 05m", "12m", "under a minute". */
export function until(ms: number): string {
  if (ms < 60_000) return "under a minute";
  const minutes = Math.floor(ms / 60_000);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}

function useNow(everyMs: number): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(timer);
  }, [everyMs]);
  return now;
}

/** The exchange's hours from the api, asked again as soon as it opens or closes. */
export function useMarketHours(): Hours | null {
  const [refresh, setRefresh] = useState(0);
  const hours = usePolling<Hours>("/api/market-hours", 5 * 60_000, refresh);
  const now = useNow(15_000);
  const due = hours != null && now >= Date.parse(hours.next_change);
  useEffect(() => {
    if (due) setRefresh((n) => n + 1);
  }, [due]);
  return hours;
}

const inChicago = (d: Date) =>
  d.toLocaleString([], { timeZone: "America/Chicago", weekday: "short", hour: "numeric", minute: "2-digit" });
const local = (d: Date) => d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });

/** Whether ElectronX is open now, and when that changes: in Central Time, with your own time on hover. */
export default function MarketHours({ hours, simulator }: { hours: Hours | null; simulator: boolean }) {
  const now = useNow(15_000);
  if (!hours) return null;
  const next = new Date(hours.next_change);
  const left = until(next.getTime() - now);
  const special = ROUTINE.includes(hours.reason) ? "" : ` (${hours.reason})`; // a holiday, or an early close
  const text = hours.open
    ? `Market open${special} · closes ${inChicago(next)} CT (in ${left})`
    : `Market closed${special} · opens ${inChicago(next)} CT (in ${left})`;
  const title = [
    `ElectronX trading hours: ${hours.schedule}.`,
    `${hours.open ? "Closes" : "Opens"} ${local(next)} your time.`,
    ...(hours.upcoming.length ? ["Coming up:", ...hours.upcoming.map((u) => `  ${u.date}  ${u.what}`)] : []),
    simulator ? "The simulator trades at any time." : "",
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <span className={`market-hours ${hours.open ? "open" : "closed"}`} title={title}>
      {text}
    </span>
  );
}
