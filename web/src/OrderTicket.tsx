import { type FormEvent, useContext, useEffect, useState } from "react";
import { api } from "./auth";
import { closesIn, type Instrument, rowState, tradingEnds } from "./board";
import { ExchangeOpen } from "./MarketHours";
import type { Book } from "./useMarketData";
import type { Order } from "./useOrders";

export type TicketPreset = { symbol: string; side: "BUY" | "SELL"; price: number; nonce: number };

type Message = { kind: "ok" | "error"; text: string } | null;

const STOPPED = new Set(["CLOSED", "HALTED", "SUSPENDED", "PENDING", "EXPIRED", "TERMINATED", "ENDED", "SETTLED"]);

/** Why the order would be rejected, checked against the contract before it is sent: its tick size, lot size,
 *  price limits and state. The exchange and orderentry check again; this only saves a round trip. */
export function problems(qty: number, price: number, inst: Instrument | undefined, state: string): string[] {
  const out: string[] = [];
  const tick = inst?.tick_size ?? 0;
  const lot = inst?.lot_size ?? 1;
  if (STOPPED.has(state)) out.push(`The contract is ${state.toLowerCase()}: it cannot be traded now.`);
  if (!Number.isInteger(qty) || qty <= 0) out.push("Quantity must be a whole number above zero.");
  else if (qty % lot) out.push(`Quantity must be a multiple of the lot size, ${lot}.`);
  if (!Number.isFinite(price)) out.push("Enter a price.");
  else {
    if (tick && Math.abs(price / tick - Math.round(price / tick)) > 1e-9) {
      out.push(`Price must be on the tick, ${tick}: for example ${(Math.round(price / tick) * tick).toFixed(2)}.`);
    }
    if (inst?.low_limit != null && price < inst.low_limit) out.push(`Price is below the limit, ${inst.low_limit}.`);
    if (inst?.high_limit != null && price > inst.high_limit) out.push(`Price is above the limit, ${inst.high_limit}.`);
  }
  return out;
}

/** Limit order ticket. Clicking a price on the price screen or in the order book fills it in. */
export default function OrderTicket({
  symbols,
  canTrade,
  preset,
  instruments,
  books,
}: {
  symbols: string[];
  canTrade: boolean;
  preset: TicketPreset | null;
  instruments: Map<string, Instrument>;
  books: Record<string, Book>;
}) {
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [qty, setQty] = useState("1");
  const [price, setPrice] = useState("");
  const [tif, setTif] = useState("DAY");
  const [message, setMessage] = useState<Message>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // The price screen's filters changed: never keep a contract the ticket no longer lists.
    if (symbols.length && !symbols.includes(symbol) && symbol !== preset?.symbol) setSymbol(symbols[0]);
  }, [symbols, symbol, preset]);

  useEffect(() => {
    if (!preset) return;
    setSymbol(preset.symbol);
    setSide(preset.side);
    setPrice(preset.price.toFixed(2));
    setMessage(null);
  }, [preset]);

  const inst = instruments.get(symbol);
  const lot = inst?.lot_size ?? 1;
  const tick = inst?.tick_size ?? 0.25;
  const exchangeOpen = useContext(ExchangeOpen);
  const state = rowState(inst, books[symbol], Date.now(), exchangeOpen);
  const ends = tradingEnds(inst);
  const found = symbol ? problems(Number(qty), price === "" ? NaN : Number(price), inst, state) : [];
  const options = symbols.includes(symbol) || !symbol ? symbols : [symbol, ...symbols];

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!symbol || found.length) {
      setMessage({ kind: "error", text: found[0] ?? "Pick a contract." });
      return;
    }
    const q = Number(qty);
    const p = Number(price);
    setBusy(true);
    try {
      const order = await api<Order>("/api/orders", {
        method: "POST",
        body: JSON.stringify({ symbol, side, qty: q, price: p, tif }),
      });
      setMessage(
        order.status === "rejected"
          ? { kind: "error", text: `Rejected: ${order.text}` }
          : { kind: "ok", text: `Sent ${side} ${q} @ ${p.toFixed(2)} (${order.cl_ord_id})` },
      );
    } catch (e) {
      setMessage({ kind: "error", text: e instanceof Error ? e.message : "Order failed" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="ticket" onSubmit={submit}>
      <h2>Order ticket</h2>
      {!canTrade && <p className="note">View only: your role can't place orders.</p>}
      <fieldset disabled={!canTrade || busy}>
        <label>
          Symbol
          <select id="ticket-symbol" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {options.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        {inst && (
          <p className="facts-line">
            Tick {tick} · lot {lot}
            {inst.low_limit != null || inst.high_limit != null
              ? ` · limits ${inst.low_limit ?? "–"} to ${inst.high_limit ?? "–"}`
              : ""}
            {ends ? ` · closes in ${closesIn(ends - Date.now())}` : ""}
          </p>
        )}
        <div className="sides">
          {(["BUY", "SELL"] as const).map((s) => (
            <button
              type="button"
              key={s}
              className={`side ${s.toLowerCase()} ${side === s ? "active" : ""}`}
              onClick={() => setSide(s)}
            >
              {s}
            </button>
          ))}
        </div>
        <div className="row">
          <label>
            Quantity
            <input id="ticket-qty" type="number" min={lot} step={lot} value={qty} onChange={(e) => setQty(e.target.value)} />
          </label>
          <label>
            Price
            <input id="ticket-price" type="number" step={tick} value={price} onChange={(e) => setPrice(e.target.value)} />
          </label>
        </div>
        <div className="quick">
          {[1, 5, 10].map((n) => (
            <button type="button" key={n} className="link" onClick={() => setQty(String(n * lot))}>
              {n * lot}
            </button>
          ))}
        </div>
        <label>
          Time in force
          <select id="ticket-tif" value={tif} onChange={(e) => setTif(e.target.value)}>
            <option value="DAY">DAY</option>
            <option value="GTC">GTC</option>
            <option value="IOC">IOC</option>
            <option value="FOK">FOK</option>
          </select>
        </label>
        {found.length > 0 && price !== "" && (
          <ul className="problems">
            {found.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        )}
        <button type="submit" className={`submit ${side.toLowerCase()}`} disabled={found.length > 0}>
          {side} {qty || 0} @ {price || "–"}
        </button>
      </fieldset>
      {message && <p className={message.kind === "ok" ? "ok" : "error"}>{message.text}</p>}
    </form>
  );
}
