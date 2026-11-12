import { useEffect, useState } from "react";
import { api } from "./auth";
import { parseSymbol } from "./symbols";

type Trade = {
  id: string;
  symbol: string;
  price: number | null;
  qty: number;
  aggressor: string;
  trade_id: string;
  time: string;
};

const MAX_ROWS = 300;

/** ERCOT NORTH 11-12 HE16 500: market, hub, delivery day, hour ending and product size. */
function short(symbol: string): string {
  const c = parseSymbol(symbol);
  if (!c) return symbol;
  const size = c.product.replace(/^BFUT/, "").replace(/^BOPT/, "opt");
  const hub = c.hub.replace(/^HB_/, "").replace(/_HUB$/, "").replace(/_/g, " ");
  return `${c.market} ${hub} ${c.day.slice(5)} HE${c.hour} ${size}`;
}

const fmtTime = (iso: string) => new Date(iso).toLocaleTimeString([], { hour12: false });

/** Every trade on the exchange as it happens, newest first, from the market data feed: price, size and
 *  who took (the aggressor). For all contracts, or the one selected on the price screen. */
export default function TimeAndSales({ selected }: { selected: string }) {
  const [mode, setMode] = useState<"all" | "contract">("all");
  const [trades, setTrades] = useState<Trade[]>([]);
  const symbol = mode === "contract" ? selected : "";

  useEffect(() => {
    setTrades([]);
    if (mode === "contract" && !selected) return;
    let stopped = false;
    let busy = false;
    let last = "";
    const load = async () => {
      if (busy) return;
      busy = true;
      const params = new URLSearchParams({ limit: "100" });
      if (symbol) params.set("symbol", symbol);
      if (last) params.set("after", last);
      try {
        const res = await api<{ trades: Trade[]; last_id: string }>(`/api/trades?${params}`);
        if (stopped) return;
        if (res.last_id) last = res.last_id;
        if (res.trades.length) {
          setTrades((prev) => {
            const seen = new Set(prev.map((t) => t.id));
            return [...res.trades.filter((t) => !seen.has(t.id)), ...prev].slice(0, MAX_ROWS);
          });
        }
      } catch {
        // the next poll tries again
      } finally {
        busy = false;
      }
    };
    load();
    const timer = window.setInterval(load, 1000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [mode, symbol, selected]);

  const all = mode === "all";
  return (
    <section className="tape">
      <h2>
        Time &amp; sales
        <span className="tape-modes">
          <button type="button" className={all ? "active" : ""} onClick={() => setMode("all")}>
            All
          </button>
          <button type="button" className={all ? "" : "active"} onClick={() => setMode("contract")}>
            This contract
          </button>
        </span>
      </h2>
      <div className="tape-scroll">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              {all && <th>Contract</th>}
              <th className="num">Price</th>
              <th className="num">Qty</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <tr
                key={t.id}
                className={t.aggressor === "BUY" ? "buy" : t.aggressor === "SELL" ? "sell" : ""}
                title={`${t.symbol}\n${t.aggressor === "BUY" ? "Buyer took the offer" : t.aggressor === "SELL" ? "Seller hit the bid" : ""}\nTrade ${t.trade_id}`}
              >
                <td>{fmtTime(t.time)}</td>
                {all && <td className="contract">{short(t.symbol)}</td>}
                <td className="num">{t.price?.toFixed(2) ?? ""}</td>
                <td className="num">{t.qty}</td>
              </tr>
            ))}
            {trades.length === 0 && (
              <tr>
                <td colSpan={all ? 4 : 3} className="empty">
                  {!all && !selected
                    ? "Click a row on the price screen to follow its trades."
                    : "No trades yet. Green: the buyer took the offer; red: the seller hit the bid."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {!all && selected && <div className="tape-symbol">{selected}</div>}
    </section>
  );
}
