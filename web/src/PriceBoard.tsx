import { type ReactNode, useContext, useEffect, useRef, useState } from "react";
import {
  closesIn,
  type Column,
  COLUMNS,
  compareValues,
  describeStatus,
  type Instrument,
  MAX_ROWS,
  rowState,
  type SortKey,
  sortValue,
  spread,
  tradingEnds,
} from "./board";
import { ExchangeOpen } from "./MarketHours";
import type { Contract } from "./symbols";
import type { Board } from "./useBoard";
import type { Book } from "./useMarketData";

export type Working = { buy: number; sell: number }; // lots still working, per side
type Side = "BUY" | "SELL";

const fmtPrice = (v: number | null | undefined) => (v == null ? "" : v.toFixed(2));
const fmtQty = (v: number | null | undefined) => (v == null ? "" : String(v));
const fmtTime = (iso: string | undefined) => (iso ? new Date(iso).toLocaleTimeString() : "");
const signed = (n: number) => (n > 0 ? `+${n}` : String(n));

function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(t);
  }, [everyMs]);
  return now;
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
      onClick={
        clickable
          ? (e) => {
              e.stopPropagation(); // trading from a price is not selecting the row
              onClick();
            }
          : undefined
      }
      title={clickable ? title : undefined}
    >
      {fmtPrice(value)}
    </td>
  );
}

export function StateBadge({ state }: { state: string }) {
  return <span className={`state state-${state.toLowerCase().replace(" ", "-")}`}>{state}</span>;
}

type RowProps = {
  contract: Contract;
  book: Book | undefined;
  inst: Instrument | undefined;
  columns: Set<Column>;
  now: number;
  position: number;
  working: Working | undefined;
  selected: boolean;
  onPick: (side: Side, price: number) => void;
  onSelect: () => void;
};

function Row({ contract, book, inst, columns, now, position, working, selected, onPick, onSelect }: RowProps) {
  const trade = book?.last_trade;
  const state = rowState(inst, book, now, useContext(ExchangeOpen));
  const ends = tradingEnds(inst);
  const dam = book?.marks?.dam?.price;
  const settle = book?.settlement;
  const optional = (column: Column, cell: ReactNode) => (columns.has(column) ? cell : null);
  return (
    <tr className={`${state === "OPEN" ? "" : "stale"} ${selected ? "selected" : ""}`} onClick={onSelect}>
      <td className="symbol" title="Click for the order book">
        {contract.symbol}
      </td>
      {optional("state", <td><StateBadge state={state} /></td>)}
      <td className="num qty">{fmtQty(book?.bid_qty)}</td>
      <PriceCell value={book?.bid ?? null} onClick={() => onPick("SELL", book!.bid!)} title="Sell at the bid" />
      <PriceCell value={book?.ask ?? null} onClick={() => onPick("BUY", book!.ask!)} title="Buy at the ask" />
      <td className="num qty">{fmtQty(book?.ask_qty)}</td>
      {optional("spread", <td className="num qty">{fmtPrice(spread(book))}</td>)}
      {optional("mid", <td className="num qty">{fmtPrice(book?.mid)}</td>)}
      <td
        className={`num ${trade?.aggressor === "BUY" ? "buy" : trade?.aggressor === "SELL" ? "sell" : ""}`}
        title={trade?.aggressor ? `${trade.aggressor === "BUY" ? "Buyer" : "Seller"} took liquidity` : undefined}
      >
        {trade?.aggressor === "BUY" ? "▲ " : trade?.aggressor === "SELL" ? "▼ " : ""}
        {fmtPrice(trade?.price)}
      </td>
      <td className="num qty">{fmtQty(trade?.qty)}</td>
      {optional("lastTime", <td className="time">{fmtTime(trade?.time)}</td>)}
      {optional(
        "closesIn",
        <td className="num qty" title={ends ? `Trading stops ${new Date(ends).toLocaleString()}` : undefined}>
          {ends ? closesIn(ends - now) : ""}
        </td>,
      )}
      {optional("dayAhead", <td className="num qty">{contract.family === "BFUT" ? fmtPrice(dam) : ""}</td>)}
      {optional(
        "settlement",
        <td className="num qty" title={settle ? (settle.final ? "Final" : "Indicative") : undefined}>
          {settle ? `${fmtPrice(settle.price)}${settle.final ? "" : " (ind.)"}` : ""}
        </td>,
      )}
      {optional("position", <td className="num">{position ? signed(position) : ""}</td>)}
      {optional(
        "working",
        <td className="num qty">
          {working?.buy ? <span className="buy">{working.buy}</span> : null}
          {working?.buy && working?.sell ? " / " : null}
          {working?.sell ? <span className="sell">{working.sell}</span> : null}
        </td>,
      )}
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

const HOURS = Array.from({ length: 24 }, (_, i) => i + 1);

function Filters({ board }: { board: Board }) {
  const { filters: f, options, setFilters: set } = board;
  return (
    <div className="filters">
      <Choice
        label="Market"
        value={f.market}
        options={options.market}
        all="All markets"
        onChange={(market) => set({ market, hub: "", product: "", day: "" })}
      />
      <Choice label="Hub" value={f.hub} options={options.hub} all="All hubs" onChange={(hub) => set({ hub })} />
      <Choice
        label="Family"
        value={f.family}
        options={options.family}
        all="BFUT and BOPT"
        onChange={(family) => set({ family, product: "" })}
      />
      <Choice
        label="Product"
        value={f.product}
        options={options.product}
        all="All sizes"
        onChange={(product) => set({ product })}
      />
      <Choice label="Delivery day" value={f.day} options={options.day} all="All days" onChange={(day) => set({ day })} />
      <label>
        Hours ending
        <span className="pair">
          <select value={f.hourFrom} onChange={(e) => set({ hourFrom: Number(e.target.value) })}>
            {HOURS.map((h) => (
              <option key={h} value={h}>
                {String(h).padStart(2, "0")}
              </option>
            ))}
          </select>
          to
          <select value={f.hourTo} onChange={(e) => set({ hourTo: Number(e.target.value) })}>
            {HOURS.map((h) => (
              <option key={h} value={h}>
                {String(h).padStart(2, "0")}
              </option>
            ))}
          </select>
        </span>
      </label>
      <label title="Weekday peak hours in each ISO's own time: HE07-22 Central and Pacific, HE08-23 Eastern">
        Load
        <select value={f.load} onChange={(e) => set({ load: e.target.value as "" | "peak" | "offpeak" })}>
          <option value="">Peak and off-peak</option>
          <option value="peak">Peak</option>
          <option value="offpeak">Off-peak</option>
        </select>
      </label>
      <label>
        Closing within
        <select value={f.closingWithin} onChange={(e) => set({ closingWithin: Number(e.target.value) })}>
          <option value={0}>Any time</option>
          <option value={1}>1 hour</option>
          <option value={3}>3 hours</option>
          <option value={12}>12 hours</option>
          <option value={24}>24 hours</option>
        </select>
      </label>
      <label className="grow">
        Search
        <input
          type="search"
          placeholder="e.g. north 18"
          value={f.search}
          onChange={(e) => set({ search: e.target.value })}
        />
      </label>
      <div className="checks">
        <label className="check" title="Only books with a bid and an offer right now">
          <input type="checkbox" checked={f.withPrices} onChange={(e) => set({ withPrices: e.target.checked })} />
          Only with prices
        </label>
        <label className="check" title="Only contracts with a position or a working order">
          <input type="checkbox" checked={f.mine} onChange={(e) => set({ mine: e.target.checked })} />
          My contracts
        </label>
        <label className="check" title="Hide contracts that have stopped trading (expired, settled); closed ones stay">
          <input type="checkbox" checked={f.hideEnded} onChange={(e) => set({ hideEnded: e.target.checked })} />
          Hide ended
        </label>
        <button className="link" onClick={board.resetFilters}>
          Reset filters
        </button>
      </div>
    </div>
  );
}

function Toolbar({ board }: { board: Board }) {
  const [view, setView] = useState("");
  const names = Object.keys(board.views).sort();
  const toggle = (column: Column, on: boolean) =>
    board.setColumns(on ? [...board.columns, column] : board.columns.filter((c) => c !== column));
  return (
    <div className="toolbar">
      <div className="segmented">
        {(["list", "strip"] as const).map((v) => (
          <button key={v} className={board.view === v ? "active" : ""} onClick={() => board.setView(v)}>
            {v === "list" ? "List" : "Strip"}
          </button>
        ))}
      </div>
      {board.view === "list" && (
        <details className="menu">
          <summary>Columns</summary>
          <div className="menu-body">
            {(Object.keys(COLUMNS) as Column[]).map((c) => (
              <label key={c} className="check">
                <input type="checkbox" checked={board.columns.includes(c)} onChange={(e) => toggle(c, e.target.checked)} />
                {COLUMNS[c]}
              </label>
            ))}
          </div>
        </details>
      )}
      <select
        value={view}
        onChange={(e) => {
          setView(e.target.value);
          if (e.target.value) board.openView(e.target.value);
        }}
        title="Saved views: filters, columns and layout"
      >
        <option value="">{names.length ? "Saved views…" : "No saved views"}</option>
        {names.map((n) => (
          <option key={n} value={n}>
            {n}
          </option>
        ))}
      </select>
      <button
        className="link"
        onClick={() => {
          const name = window.prompt("Name for this view (filters, columns and layout):", view)?.trim();
          if (name) {
            board.saveView(name);
            setView(name);
          }
        }}
      >
        Save view
      </button>
      {view && (
        <button
          className="link"
          onClick={() => {
            board.deleteView(view);
            setView("");
          }}
        >
          Delete “{view}”
        </button>
      )}
      <span className="count">
        {board.matching > board.rows.length
          ? `${board.rows.length} of ${board.matching.toLocaleString()}: narrow the filters to see the rest`
          : `${board.matching.toLocaleString()} contracts`}
      </span>
    </div>
  );
}

type BoardProps = {
  board: Board;
  books: Record<string, Book>;
  positions: Record<string, number>;
  working: Record<string, Working>;
  selected: string;
  onPick: (symbol: string, side: Side, price: number) => void;
  onSelect: (symbol: string) => void;
};

function ListView({ board, books, positions, working, selected, onPick, onSelect }: BoardProps) {
  const now = useNow(1000);
  const columns = new Set(board.columns);
  const { key, desc } = board.sort;
  const rows =
    key === "screen"
      ? board.rows
      : [...board.rows].sort((a, b) =>
          compareValues(
            sortValue(key, a, books[a.symbol], board.instruments.get(a.symbol), positions[a.symbol] ?? 0),
            sortValue(key, b, books[b.symbol], board.instruments.get(b.symbol), positions[b.symbol] ?? 0),
            desc,
          ),
        );
  const header = (label: string, sortKey?: SortKey, num = true) => {
    const active = sortKey && key === sortKey;
    return (
      <th
        className={`${num ? "num" : ""} ${sortKey ? "sortable" : ""}`}
        onClick={
          sortKey
            ? () => board.setSort(active ? (desc ? { key: "screen", desc: false } : { key: sortKey, desc: true }) : { key: sortKey, desc: false })
            : undefined
        }
        title={sortKey ? "Sort the rows shown (click again to reverse, a third time to undo)" : undefined}
      >
        {label}
        {active ? (desc ? " ▼" : " ▲") : ""}
      </th>
    );
  };
  const optional = (column: Column, cell: ReactNode) => (columns.has(column) ? cell : null);
  const span = 7 + board.columns.length;
  return (
    <div className="board-scroll">
      <table className="prices">
        <thead>
          <tr>
            {header("Symbol", "symbol", false)}
            {optional("state", header("State", undefined, false))}
            {header("Bid qty")}
            {header("Bid", "bid")}
            {header("Ask", "ask")}
            {header("Ask qty")}
            {optional("spread", header("Spread", "spread"))}
            {optional("mid", header("Mid"))}
            {header("Last", "last")}
            {header("Last qty")}
            {optional("lastTime", header("Last at", undefined, false))}
            {optional("closesIn", header("Closes in", "closesIn"))}
            {optional("dayAhead", header("Day-ahead"))}
            {optional("settlement", header("Settlement"))}
            {optional("position", header("Position", "position"))}
            {optional("working", header("Working"))}
            {header("Updated", undefined, false)}
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <Row
              key={c.symbol}
              contract={c}
              book={books[c.symbol]}
              inst={board.instruments.get(c.symbol)}
              columns={columns}
              now={now}
              position={positions[c.symbol] ?? 0}
              working={working[c.symbol]}
              selected={c.symbol === selected}
              onPick={(side, price) => onPick(c.symbol, side, price)}
              onSelect={() => onSelect(c.symbol)}
            />
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={span} className="empty">
                {board.loaded ? "No contracts match the filters." : "Waiting for the instrument list…"}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function StripView({ board, books, positions, selected, onPick, onSelect }: BoardProps) {
  const now = useNow(5000);
  const exchangeOpen = useContext(ExchangeOpen);
  if (!board.strip) return <p className="empty">{board.stripNeeds || "Waiting for the instrument list…"}</p>;
  const { hubs, hours, cells } = board.strip;
  return (
    <div className="board-scroll">
      <table className="prices strip">
        <thead>
          <tr>
            <th>HE</th>
            {hubs.map((h) => (
              <th key={h} className="num">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {hours.map((hourRow) => (
            <tr key={hourRow.hour}>
              <td className="symbol">{hourRow.hour}</td>
              {hubs.map((hub) => {
                const symbol = cells.get(`${hub}|${hourRow.hour}`);
                if (!symbol) return <td key={hub} />;
                const book = books[symbol];
                const state = rowState(board.instruments.get(symbol), book, now, exchangeOpen);
                const position = positions[symbol] ?? 0;
                return (
                  <td
                    key={hub}
                    className={`cell ${state === "OPEN" ? "" : "stale"} ${symbol === selected ? "selected" : ""}`}
                    title={`${symbol}: ${state}${position ? `, position ${signed(position)}` : ""}`}
                    onClick={() => onSelect(symbol)}
                  >
                    <span
                      className={`bid ${book?.bid != null ? "clickable" : ""}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (book?.bid != null) onPick(symbol, "SELL", book.bid);
                      }}
                    >
                      {fmtPrice(book?.bid) || "–"}
                    </span>
                    <span className="sep">/</span>
                    <span
                      className={`ask ${book?.ask != null ? "clickable" : ""}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (book?.ask != null) onPick(symbol, "BUY", book.ask);
                      }}
                    >
                      {fmtPrice(book?.ask) || "–"}
                    </span>
                    {position ? <span className="pos">{signed(position)}</span> : null}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The price screen: filters over every listed contract, then live books for the ones shown, as a list or
 *  as an hourly strip. Clicking a price trades it; clicking elsewhere on a row opens its order book. */
export default function PriceBoard(props: BoardProps) {
  const { board } = props;
  return (
    <div className="board">
      <Filters board={board} />
      <Toolbar board={board} />
      {board.statuses.length > 0 && (
        <div className="statuses">
          {board.statuses.map((s) => (
            <span key={s.market} className={s.closed === s.open + s.closed + s.halted + s.unknown ? "closed" : ""}>
              {describeStatus(s)}
            </span>
          ))}
        </div>
      )}
      {board.view === "list" ? <ListView {...props} /> : <StripView {...props} />}
      {board.view === "list" && board.sort.key !== "screen" && (
        <p className="note">Sorting orders the {board.rows.length} rows shown; up to {MAX_ROWS} are shown.</p>
      )}
    </div>
  );
}
