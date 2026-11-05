import { useState } from "react";
import { api, type User } from "./auth";
import { LIVE, type Order } from "./useOrders";

const fmtTime = (iso: string) => new Date(iso).toLocaleTimeString();
const label = (status: string) => status.replace("_", " ");

/** Orders, newest first. Risk and management see everyone's, with a Trader column. */
export default function Blotter({ orders, user }: { orders: Order[]; user: User }) {
  const [error, setError] = useState("");
  const showTrader = user.permissions.includes("orders.view.all");
  const canTrade = user.permissions.includes("orders.trade");

  const cancel = async (o: Order) => {
    setError("");
    try {
      await api(`/api/orders/${encodeURIComponent(o.cl_ord_id)}/cancel`, { method: "POST" });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Cancel failed");
    }
  };

  return (
    <section className="blotter">
      <h2>Orders</h2>
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>Time</th>
            {showTrader && <th>Trader</th>}
            <th>Symbol</th>
            <th>Side</th>
            <th className="num">Qty</th>
            <th className="num">Price</th>
            <th className="num">Filled</th>
            <th className="num">Avg</th>
            <th>Status</th>
            <th>Note</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.cl_ord_id}>
              <td className="time">{fmtTime(o.created)}</td>
              {showTrader && <td>{o.trader}</td>}
              <td className="symbol">{o.symbol}</td>
              <td className={o.side === "BUY" ? "buy" : "sell"}>{o.side}</td>
              <td className="num">{o.qty}</td>
              <td className="num">{o.price.toFixed(2)}</td>
              <td className="num">{o.cum_qty}</td>
              <td className="num">{o.cum_qty ? o.avg_px.toFixed(2) : ""}</td>
              <td>
                <span className={`pill ${o.status}`}>{label(o.status)}</span>
              </td>
              <td className="text">{o.text}</td>
              <td>
                {canTrade && o.trader === user.id && LIVE.includes(o.status) && o.status !== "pending_cancel" && (
                  <button className="link" onClick={() => cancel(o)}>
                    Cancel
                  </button>
                )}
              </td>
            </tr>
          ))}
          {orders.length === 0 && (
            <tr>
              <td colSpan={showTrader ? 11 : 10} className="empty">
                No orders yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
