/** The price screen's logic, with no display code: each contract's state, the filters, peak hours,
 *  sorting and the strip layout. */
import type { Contract } from "./symbols";
import type { Book } from "./useMarketData";

/** At most this many rows (or strip cells): the browser gets live prices only for what it shows. */
export const MAX_ROWS = 200;

export type Instrument = {
  symbol: string;
  state: string | null;
  last_trade_time: string | null;
  expiration_time: string | null;
  tick_size: number | null;
  lot_size: number | null;
  low_limit: number | null;
  high_limit: number | null;
};

export type Filters = {
  market: string;
  hub: string;
  family: string; // BFUT, BOPT or "" for both
  product: string; // with its size, e.g. BFUT500
  day: string;
  hourFrom: number; // hour ending, 1..24
  hourTo: number;
  load: "" | "peak" | "offpeak";
  closingWithin: number; // hours; 0 = any time
  withPrices: boolean; // only books with a bid and an offer
  mine: boolean; // only contracts with a position or a working order
  hideEnded: boolean; // hide contracts whose trading has ended (expired, settled)
  search: string; // words that must all appear in the symbol
};

export const DEFAULT_FILTERS: Filters = {
  market: "",
  hub: "",
  family: "",
  product: "",
  day: "",
  hourFrom: 1,
  hourTo: 24,
  load: "",
  closingWithin: 0,
  withPrices: false,
  mine: false,
  hideEnded: true,
  search: "",
};

export const COLUMNS = {
  state: "State",
  spread: "Spread",
  mid: "Mid",
  lastTime: "Last trade time",
  closesIn: "Closes in",
  dayAhead: "Day-ahead",
  settlement: "Settlement",
  position: "Position",
  working: "Working",
} as const;
export type Column = keyof typeof COLUMNS;
export const DEFAULT_COLUMNS: Column[] = ["state", "spread", "closesIn", "position", "working"];

export type SortKey = "screen" | "symbol" | "bid" | "ask" | "spread" | "last" | "closesIn" | "position";
export type Sort = { key: SortKey; desc: boolean };

/** Peak hours (hour ending, inclusive) on weekdays, in each ISO's own time: 07:00-22:00 Central and
 *  Pacific, 07:00-23:00 Eastern. Holidays are not taken out. */
export const PEAK_HOURS: Record<string, [number, number]> = {
  ERCOT: [7, 22],
  SPP: [7, 22],
  CAISO: [7, 22],
  PJM: [8, 23],
  NYISO: [8, 23],
  ISONE: [8, 23],
  MISO: [8, 23],
};

export function isPeak(c: Contract): boolean {
  const [from, to] = PEAK_HOURS[c.market] ?? [8, 23];
  return c.weekday >= 1 && c.weekday <= 5 && c.hourNum >= from && c.hourNum <= to;
}

const ENDED_STATES = new Set(["EXPIRED", "TERMINATED"]);

export const tradingEnds = (i: Instrument | undefined): number | null =>
  i?.last_trade_time ? Date.parse(i.last_trade_time) : null;

/** Trading has stopped for good: expired, or past its last trade time. */
export function hasEnded(i: Instrument | undefined, now: number): boolean {
  const ends = tradingEnds(i);
  return ENDED_STATES.has(i?.state ?? "") || (ends !== null && ends <= now);
}

/** What the row shows: OPEN, CLOSED (for now), HALTED, SUSPENDED, PENDING, EXPIRED, SETTLED, ENDED
 *  (past its last trade time), or NO DATA (no live book yet). */
export function rowState(i: Instrument | undefined, book: Book | undefined, now: number): string {
  if (book?.settlement?.final) return "SETTLED";
  const state = book?.state || i?.state || "";
  if (ENDED_STATES.has(state)) return state;
  const ends = tradingEnds(i);
  if (ends !== null && ends <= now) return "ENDED";
  if (state && state !== "OPEN") return state;
  if (!book || book.stale) return "NO DATA";
  return "OPEN";
}

export const spread = (b: Book | undefined) => (b?.bid != null && b?.ask != null ? b.ask - b.bid : null);

export function sortValue(key: SortKey, c: Contract, b: Book | undefined, i: Instrument | undefined, position: number) {
  switch (key) {
    case "symbol":
      return c.symbol;
    case "bid":
      return b?.bid ?? null;
    case "ask":
      return b?.ask ?? null;
    case "spread":
      return spread(b);
    case "last":
      return b?.last_trade?.price ?? null;
    case "closesIn":
      return tradingEnds(i);
    case "position":
      return position || null;
    default:
      return null;
  }
}

/** Rows without a value (no price, no position) always go last, whichever way the column is sorted. */
export function compareValues(a: string | number | null, b: string | number | null, desc: boolean): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  const order = typeof a === "string" ? a.localeCompare(String(b)) : a - (b as number);
  return desc ? -order : order;
}

export const byScreen = (a: Contract, b: Contract) =>
  a.market.localeCompare(b.market) ||
  a.day.localeCompare(b.day) ||
  a.hub.localeCompare(b.hub) ||
  a.product.localeCompare(b.product) ||
  a.order - b.order;

export function matchesSearch(c: Contract, search: string): boolean {
  const words = search.toUpperCase().split(/\s+/).filter(Boolean);
  return words.every((w) => c.symbol.includes(/^\d{1,2}$/.test(w) ? `-${w.padStart(2, "0")}` : w));
}

export function closesIn(ms: number): string {
  if (ms <= 0) return "closed";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export type StatusCount = { market: string; open: number; closed: number; halted: number; unknown: number };

/** Per market, how many of its contracts still trading are open, closed (for now), halted or not reported
 *  yet: so a closed market reads as closed, not as broken. */
export function marketStatus(contracts: Contract[], instruments: Map<string, Instrument>, now: number): StatusCount[] {
  const out = new Map<string, StatusCount>();
  for (const c of contracts) {
    const i = instruments.get(c.symbol);
    if (!c.market || hasEnded(i, now)) continue;
    const row = out.get(c.market) ?? { market: c.market, open: 0, closed: 0, halted: 0, unknown: 0 };
    const state = i?.state ?? "";
    if (state === "OPEN") row.open += 1;
    else if (state === "CLOSED") row.closed += 1;
    else if (state) row.halted += 1; // HALTED, SUSPENDED, PENDING
    else row.unknown += 1;
    out.set(c.market, row);
  }
  return [...out.values()].sort((a, b) => a.market.localeCompare(b.market));
}

export function describeStatus(s: StatusCount): string {
  const parts = [
    s.open && `${s.open.toLocaleString()} open`,
    s.closed && `${s.closed.toLocaleString()} closed`,
    s.halted && `${s.halted.toLocaleString()} halted or suspended`,
    s.unknown && `${s.unknown.toLocaleString()} not reported yet`,
  ].filter(Boolean);
  const total = s.open + s.closed + s.halted + s.unknown;
  if (s.closed === total) return `${s.market}: closed (all ${total.toLocaleString()} contracts)`;
  return `${s.market}: ${parts.join(" · ")}`;
}
