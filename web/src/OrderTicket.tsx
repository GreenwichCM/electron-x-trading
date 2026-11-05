import { type FormEvent, useEffect, useState } from "react";
import { api } from "./auth";
import type { Order } from "./useOrders";

export type TicketPreset = { symbol: string; side: "BUY" | "SELL"; price: number; nonce: number };

type Message = { kind: "ok" | "error"; text: string } | null;

/** Limit order ticket. Clicking a bid or ask in the price table fills it in. */
export default function OrderTicket({
  symbols,
  canTrade,
  preset,
}: {
  symbols: string[];
  canTrade: boolean;
  preset: TicketPreset | null;
}) {
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [qty, setQty] = useState("1");
  const [price, setPrice] = useState("");
  const [tif, setTif] = useState("DAY");
  const [message, setMessage] = useState<Message>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!symbol && symbols.length) setSymbol(symbols[0]);
  }, [symbols, symbol]);

  useEffect(() => {
    if (!preset) return;
    setSymbol(preset.symbol);
    setSide(preset.side);
    setPrice(preset.price.toFixed(2));
    setMessage(null);
  }, [preset]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const q = Number(qty);
    const p = Number(price);
    if (!symbol || !Number.isInteger(q) || q <= 0 || !Number.isFinite(p) || price === "") {
      setMessage({ kind: "error", text: "Enter a symbol, a whole-number quantity and a price." });
      return;
    }
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
            {symbols.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
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
            <input id="ticket-qty" type="number" min="1" step="1" value={qty} onChange={(e) => setQty(e.target.value)} />
          </label>
          <label>
            Price
            <input
              id="ticket-price"
              type="number"
              step="0.25"
              value={price}
              onChange={(e) => setPrice(e.target.value)}
            />
          </label>
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
        <button type="submit" className={`submit ${side.toLowerCase()}`}>
          {side} {qty || 0} @ {price || "–"}
        </button>
      </fieldset>
      {message && <p className={message.kind === "ok" ? "ok" : "error"}>{message.text}</p>}
    </form>
  );
}
