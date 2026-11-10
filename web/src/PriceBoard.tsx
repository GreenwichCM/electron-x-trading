import { useEffect, useMemo, useRef, useState } from "react";
import { type Contract, parseSymbol } from "./symbols";
import type { Book } from "./useMarketData";
import { usePolling } from "./usePolling";

/** At most this many rows: the browser gets live prices only for the rows it shows. */
export const MAX_ROWS = 200;

type Instrument = { symbol: string; state: string; last_trade_time: string | null };

export type Filters = { market: string; hub: string; product: string; day: string; tradingOnly: boolean };
const ALL: Filters = { market: "", hub: "", product: "", day: "", tradingOnly: true };

const fmtPrice = (v: number | null | undefined) => (v == null ? "" : v.toFixed(2));
const fmtQty = (v: number | null | undefined) => (v == null ? "" : String(v));
const fmtTime = (iso: string | undefined) => (iso ? new Date(iso).toLocaleTimeString() : "");
const unique = (values: string[]) => [...new Set(values)].sort();

const trading = (i: Instrument, now: number) =>
  (!i.state || i.state === "OPEN") && (!i.last_trade_time || Date.parse(i.last_trade_time) > now);

const byScreen = (a: Contract, b: Contract) =>
  a.market.localeCompare(b.market) ||
  a.day.localeCompare(b.day) ||
  a.hub.localeCompare(b.hub) ||
  a.product.localeCompare(b.product) ||
  a.order - b.order;

function savedFilters(key: string): Filters {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? { ...ALL, ...JSON.parse(raw) } : ALL;
  } catch {
    return ALL;
  }
}

/** Every listed contract, the filters, and the contracts they select (at most MAX_ROWS). Each user's
 *  filters are remembered in this browser, so the same contracts are on screen (and get live prices
 *  first) the next time they sign in. */
export function useBoard(userId: string) {
  // Asked often, but cheap: while the list is unchanged the api answers "304 Not Modified" with no body.
  const instruments = usePolling<Instrument[]>("/api/instruments", 10000);
  const key = `board-filters:${userId}`;
  const [filters, setFiltersState] = useState<Filters>(() => savedFilters(key));
  const setFilters = (next: Filters) => {
    setFiltersState(next);
    try {
      window.localStorage.setItem(key, JSON.stringify(next));
    } catch {
      // private window or storage blocked: the filters just are not remembered
    }
  };
  return useMemo(() => {
    const now = Date.now();
    const all = (instruments ?? [])
      .filter((i) => !filters.tradingOnly || trading(i, now))
      .map((i) => parseSymbol(i.symbol) ?? { symbol: i.symbol, product: "", market: "", hub: "", day: "", hour: "", order: 0 });
    // A remembered choice that no longer exists (a delivery day gone by) counts as "all".
    const markets = unique(all.map((c) => c.market).filter(Boolean));
    const market = markets.includes(filters.market) ? filters.market : "";
    const inMarket = all.filter((c) => !market || c.market === market);
    const options = {
      market: markets,
      hub: unique(inMarket.map((c) => c.hub).filter(Boolean)),
      product: unique(inMarket.map((c) => c.product).filter(Boolean)),
      day: unique(inMarket.map((c) => c.day).filter(Boolean)),
    };
    const hub = options.hub.includes(filters.hub) ? filters.hub : "";
    const product = options.product.includes(filters.product) ? filters.product : "";
    const day = options.day.includes(filters.day) ? filters.day : "";
    const selected = inMarket
      .filter((c) => !hub || c.hub === hub)
      .filter((c) => !product || c.product === product)
      .filter((c) => !day || c.day === day)
      .sort(byScreen);
    return {
      loaded: instruments !== null,
      filters: { ...filters, market, hub, product, day },
      setFilters,
      options,
      matching: selected.length,
      rows: selected.slice(0, MAX_ROWS).map((c) => c.symbol),
    };
  }, [instruments, filters]);
}

/** A price cell that flashes green when the value rises and red when it falls. Clickable to trade. */
function PriceCell({ value, onClick, title }: { value: number | null; onClick?: () => void; title?: string }) {
  const previous = useRef(value);
  const [flash, setFlash] = useState("");
  useEffect(() => {
    const before = previous.current;
    previous.current = value;
    if (value == null || before == null || value === before) return;
    setFlash(value > before ? "up" : "down");
    const t = window.setTimeout(() => setFlash(""), 600);
    return () => window.clearTimeout(t);
  }, [value]);
  const clickable = onClick && value != null;
  return (
    <td
      className={`num ${flash} ${clickable ? "clickable" : ""}`}
      onClick={clickable ? onClick : undefined}
      title={clickable ? title : undefined}
    >
      {fmtPrice(value)}
    </td>
  );
}

function Row({
  symbol,
  book,
  onPick,
}: {
  symbol: string;
  book: Book | undefined;
  onPick: (side: "BUY" | "SELL", price: number) => void;
}) {
  const trade = book?.last_trade;
  return (
    <tr className={!book || book.stale ? "stale" : ""}>
      <td className="symbol">{symbol}</td>
      <td className="num qty">{fmtQty(book?.bid_qty)}</td>
      <PriceCell value={book?.bid ?? null} onClick={() => onPick("SELL", book!.bid!)} title="Sell at the bid" />
      <PriceCell value={book?.ask ?? null} onClick={() => onPick("BUY", book!.ask!)} title="Buy at the ask" />
      <td className="num qty">{fmtQty(book?.ask_qty)}</td>
      <td className={`num ${trade?.aggressor === "BUY" ? "buy" : trade?.aggressor === "SELL" ? "sell" : ""}`}>
        {fmtPrice(trade?.price)}
      </td>
      <td className="num qty">{fmtQty(trade?.qty)}</td>
      <td className="time">{fmtTime(book?.time)}</td>
    </tr>
  );
}

function Choice({
  label,
  value,
  options,
  all,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  all: string;
  onChange: (value: string) => void;
}) {
  return (
    <label>
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{all}</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

/** The price screen: filters over every listed contract, then live books for the ones selected. */
export default function PriceBoard({
  board,
  books,
  onPick,
}: {
  board: ReturnType<typeof useBoard>;
  books: Record<string, Book>;
  onPick: (symbol: string, side: "BUY" | "SELL", price: number) => void;
}) {
  const { filters, setFilters, options, rows, matching } = board;
  const set = (change: Partial<Filters>) => setFilters({ ...filters, ...change });
  return (
    <div className="board">
      <div className="filters">
        <Choice
          label="Market"
          value={filters.market}
          options={options.market}
          all="All markets"
          onChange={(market) => set({ market, hub: "", product: "", day: "" })}
        />
        <Choice label="Hub" value={filters.hub} options={options.hub} all="All hubs" onChange={(hub) => set({ hub })} />
        <Choice
          label="Product"
          value={filters.product}
          options={options.product}
          all="All products"
          onChange={(product) => set({ product })}
        />
        <Choice label="Delivery day" value={filters.day} options={options.day} all="All days" onChange={(day) => set({ day })} />
        <label className="check">
          <input
            type="checkbox"
            checked={filters.tradingOnly}
            onChange={(e) => set({ tradingOnly: e.target.checked })}
          />
          Trading now
        </label>
        <span className="count">
          {matching > rows.length ? `${rows.length} of ${matching}: narrow the filters to see the rest` : `${matching} contracts`}
        </span>
      </div>
      <table className="prices">
        <thead>
          <tr>
            <th>Symbol</th>
            <th className="num">Bid qty</th>
            <th className="num">Bid</th>
            <th className="num">Ask</th>
            <th className="num">Ask qty</th>
            <th className="num">Last</th>
            <th className="num">Last qty</th>
            <th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((symbol) => (
            <Row key={symbol} symbol={symbol} book={books[symbol]} onPick={(side, price) => onPick(symbol, side, price)} />
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={8} className="empty">
                {board.loaded ? "No contracts match the filters." : "Waiting for the instrument list…"}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
