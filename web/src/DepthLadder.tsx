import { useContext } from "react";
import { type Instrument, rowState, spread } from "./board";
import { ExchangeOpen } from "./MarketHours";
import { StateBadge } from "./PriceBoard";
import type { Book } from "./useMarketData";

const fmtPrice = (v: number | null | undefined) => (v == null ? "" : v.toFixed(2));

/** The selected contract's order book, every level the exchange sends (up to 10 a side). Clicking a bid
 *  sells at that price, clicking an offer buys at it. */
export default function DepthLadder({
  symbol,
  book,
  inst,
  onPick,
}: {
  symbol: string;
  book: Book | undefined;
  inst: Instrument | undefined;
  onPick: (side: "BUY" | "SELL", price: number) => void;
}) {
  const exchangeOpen = useContext(ExchangeOpen);
  if (!symbol) {
    return (
      <section className="ladder">
        <h2>Order book</h2>
        <p className="note">Click a row on the price screen to see its order book.</p>
      </section>
    );
  }
  const bids = book?.bids ?? [];
  const asks = book?.asks ?? [];
  const depth = Math.max(bids.length, asks.length);
  const biggest = Math.max(1, ...bids.map(([, q]) => q), ...asks.map(([, q]) => q));
  const bar = (qty: number) => `${Math.round((100 * qty) / biggest)}%`;
  const trade = book?.last_trade;
  const state = rowState(inst, book, Date.now(), exchangeOpen);
  return (
    <section className="ladder">
      <h2>
        Order book <StateBadge state={state} />
      </h2>
      <div className="ladder-symbol">{symbol}</div>
      <table>
        <thead>
          <tr>
            <th className="num">Bid qty</th>
            <th className="num">Bid</th>
            <th className="num">Ask</th>
            <th className="num">Ask qty</th>
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: depth }, (_, i) => {
            const bid = bids[i];
            const ask = asks[i];
            return (
              <tr key={i}>
                <td className="num qty depth">
                  {bid && <span className="depth-bar bid" style={{ width: bar(bid[1]) }} />}
                  {bid?.[1] ?? ""}
                </td>
                <td
                  className={`num buy ${bid ? "clickable" : ""}`}
                  onClick={bid ? () => onPick("SELL", bid[0]) : undefined}
                  title={bid ? "Sell at this price" : undefined}
                >
                  {fmtPrice(bid?.[0])}
                </td>
                <td
                  className={`num sell ${ask ? "clickable" : ""}`}
                  onClick={ask ? () => onPick("BUY", ask[0]) : undefined}
                  title={ask ? "Buy at this price" : undefined}
                >
                  {fmtPrice(ask?.[0])}
                </td>
                <td className="num qty depth">
                  {ask && <span className="depth-bar ask" style={{ width: bar(ask[1]) }} />}
                  {ask?.[1] ?? ""}
                </td>
              </tr>
            );
          })}
          {depth === 0 && (
            <tr>
              <td colSpan={4} className="empty">
                {book && !book.stale ? "The book is empty." : "No live book yet."}
              </td>
            </tr>
          )}
        </tbody>
      </table>
      <dl className="facts">
        <dt>Spread</dt>
        <dd>{fmtPrice(spread(book)) || "–"}</dd>
        <dt>Last trade</dt>
        <dd>
          {trade?.price != null
            ? `${trade.qty} @ ${fmtPrice(trade.price)}${trade.aggressor ? ` (${trade.aggressor === "BUY" ? "buyer" : "seller"} took)` : ""}`
            : "–"}
        </dd>
        <dt>Tick / lot</dt>
        <dd>
          {inst?.tick_size ?? "–"} / {inst?.lot_size ?? "–"}
        </dd>
        <dt>Price limits</dt>
        <dd>
          {inst?.low_limit != null || inst?.high_limit != null
            ? `${fmtPrice(inst?.low_limit) || "–"} to ${fmtPrice(inst?.high_limit) || "–"}`
            : "–"}
        </dd>
      </dl>
    </section>
  );
}
